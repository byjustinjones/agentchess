"""Post-game move-quality analysis with a UCI engine (normally Stockfish).

For every non-book ply of a *finished* game the engine evaluates the position
before the move (which also yields its first choice) and after it; the
difference, from the mover's point of view, is that move's centipawn loss.

Per player and game this gives the average centipawn loss (ACPL), blunder /
mistake / inaccuracy counts, the share of moves matching the engine's first
choice, and whether a clearly winning position (>= ``WIN_CP``) was not
converted into a win ("missed win"). Positions that are already decided (both
evaluations beyond ``DECIDED_CP``) are not judged: once a game is lost, the size
of further losses says little about playing strength.

Everything here is *post-game* only. Engines run with a single thread at low
priority (``nice``) so a running benchmark is not disturbed, and analysis is
never exposed to players through the agent API.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import chess
import chess.engine

from agentchess.models import GameRecord, GameStatus, score_of

log = logging.getLogger(__name__)

DEFAULT_DEPTH = 12
CLAMP_CP = 1000          # mate scores and huge evals are clamped to +-CLAMP_CP
DECIDED_CP = 600         # both evals beyond this: position already decided, move not judged
WIN_CP = 500             # reaching this eval and not winning counts as a missed win
INACCURACY_CP = 50
MISTAKE_CP = 100
BLUNDER_CP = 300


@dataclass
class MoveEval:
    ply: int
    player_id: str
    eval_before: int          # cp, mover's POV, before the move (clamped)
    eval_after: int           # cp, mover's POV, after the move (clamped)
    loss: int
    best_uci: Optional[str]
    judged: bool

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class PlayerSummary:
    moves: int = 0
    total_loss: float = 0.0
    blunders: int = 0
    mistakes: int = 0
    inaccuracies: int = 0
    best_moves: int = 0
    max_eval: int = -CLAMP_CP
    won: bool = False
    missed_win: bool = False

    @property
    def acpl(self) -> Optional[float]:
        return self.total_loss / self.moves if self.moves else None

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "acpl": self.acpl}


@dataclass
class GameAnalysis:
    game_id: str
    engine: str
    depth: int
    moves: list[MoveEval] = field(default_factory=list)
    players: dict[str, PlayerSummary] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"game_id": self.game_id, "engine": self.engine, "depth": self.depth,
                "moves": [m.to_dict() for m in self.moves],
                "players": {pid: s.to_dict() for pid, s in self.players.items()}}


def classify(loss: int) -> Optional[str]:
    if loss >= BLUNDER_CP:
        return "blunder"
    if loss >= MISTAKE_CP:
        return "mistake"
    if loss >= INACCURACY_CP:
        return "inaccuracy"
    return None


def _cp(score: chess.engine.PovScore, pov: chess.Color) -> int:
    v = score.pov(pov).score(mate_score=CLAMP_CP * 10)
    return max(-CLAMP_CP, min(CLAMP_CP, int(v if v is not None else 0)))


Evaluator = Callable[[chess.Board], Awaitable[tuple[int, Optional[chess.Move]]]]
"""Async ``board -> (cp from the side to move's POV, engine's first choice)``."""


def _is_book(m: Any) -> bool:
    usage = getattr(m, "usage", None) or {}
    return bool(usage.get("book")) or getattr(m, "comment", None) == "book"


async def analyse_moves(game: GameRecord, evaluate: Evaluator) -> tuple[list[MoveEval], dict[str, PlayerSummary]]:
    """Judge every move of ``game`` with ``evaluate`` (engine-agnostic core, unit-testable)."""
    board = chess.Board(game.initial_fen)
    ids = {"white": game.white_id, "black": game.black_id}
    players = {pid: PlayerSummary() for pid in (game.white_id, game.black_id)}
    for pid, white in ((game.white_id, True), (game.black_id, False)):
        players[pid].won = score_of(game.result, white) == 1.0
    evals: list[MoveEval] = []
    # Evaluate lazily so each position is analysed once (after-move eval of ply n is the
    # before-move eval of ply n+1, negated).
    cached: Optional[tuple[int, Optional[chess.Move]]] = None
    for rec in game.moves:
        try:
            move = chess.Move.from_uci(rec.uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        pid = ids[rec.color] if rec.color in ids else game.white_id
        if _is_book(rec):
            board.push(move)
            cached = None
            continue
        if cached is None:
            cached = await evaluate(board)
        before, best = cached
        board.push(move)
        if board.is_checkmate():
            after, cached = CLAMP_CP, None
        elif board.is_game_over():
            after, cached = 0, None
        else:
            opp_cp, opp_best = await evaluate(board)
            after = -opp_cp
            cached = (opp_cp, opp_best)
        loss = max(0, before - after)
        judged = not (abs(before) >= DECIDED_CP and abs(after) >= DECIDED_CP)
        best_uci = best.uci() if best else None
        s = players[pid]
        s.max_eval = max(s.max_eval, before, after)
        if judged:
            s.moves += 1
            s.total_loss += loss
            kind = classify(loss)
            if kind == "blunder":
                s.blunders += 1
            elif kind == "mistake":
                s.mistakes += 1
            elif kind == "inaccuracy":
                s.inaccuracies += 1
            if best_uci == move.uci():
                s.best_moves += 1
        evals.append(MoveEval(ply=rec.ply, player_id=pid, eval_before=before, eval_after=after, loss=loss,
                              best_uci=best_uci, judged=judged))
    for s in players.values():
        s.missed_win = s.max_eval >= WIN_CP and not s.won
    return evals, players


class EngineAnalyser:
    """Owns one low-priority, single-threaded engine process; ``analyse`` games with it."""

    def __init__(self, path: str, depth: int = DEFAULT_DEPTH, nice: bool = True, hash_mb: int = 32) -> None:
        self.path = path
        self.depth = depth
        self.nice = nice
        self.hash_mb = hash_mb
        self._engine: Optional[chess.engine.UciProtocol] = None
        self._transport: Optional[asyncio.SubprocessTransport] = None
        self._lock = asyncio.Lock()

    @property
    def engine_name(self) -> str:
        if self._engine is not None:
            return str(self._engine.id.get("name") or self.path)
        return self.path

    async def _ensure(self) -> chess.engine.UciProtocol:
        if self._engine is not None:
            return self._engine
        cmd: list[str] = [self.path]
        if self.nice and shutil.which("nice"):
            cmd = ["nice", "-n", "19", self.path]
        self._transport, self._engine = await chess.engine.popen_uci(cmd)
        opts: dict[str, Any] = {}
        for name, value in (("Threads", 1), ("Hash", self.hash_mb)):
            if name in self._engine.options:
                opts[name] = value
        if opts:
            await self._engine.configure(opts)
        return self._engine

    async def evaluate(self, board: chess.Board) -> tuple[int, Optional[chess.Move]]:
        engine = await self._ensure()
        info = await engine.analyse(board, chess.engine.Limit(depth=self.depth))
        score = info.get("score")
        pv = info.get("pv") or []
        return (_cp(score, board.turn) if score is not None else 0), (pv[0] if pv else None)

    async def analyse(self, game: GameRecord) -> GameAnalysis:
        if game.status != GameStatus.FINISHED:
            raise ValueError("only finished games are analysed")
        async with self._lock:
            try:
                moves, players = await analyse_moves(game, self.evaluate)
            except (chess.engine.EngineError, chess.engine.EngineTerminatedError):
                await self.close()
                raise
            return GameAnalysis(game_id=game.id, engine=self.engine_name, depth=self.depth,
                                moves=moves, players=players)

    async def close(self) -> None:
        engine, transport = self._engine, self._transport
        self._engine = self._transport = None
        if engine is not None:
            try:
                await asyncio.wait_for(engine.quit(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass


def store(db: Any, result: GameAnalysis) -> None:
    db.save_analysis(result.game_id, result.engine, result.depth,
                     [m.to_dict() for m in result.moves],
                     {pid: s.to_dict() for pid, s in result.players.items()})


class AnalysisService:
    """Background worker: analyses finished games one at a time and stores the result.

    ``enqueue`` is safe to call from anywhere on the event loop; ``analyse_now`` runs
    a game immediately (still serialised on the engine) and returns the stored result.
    """

    def __init__(self, db: Any, engine_path: Optional[str], depth: int = DEFAULT_DEPTH,
                 bus: Any = None, nice: bool = True) -> None:
        self.db = db
        self.engine_path = engine_path
        self.depth = depth
        self.bus = bus
        self.nice = nice
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._task: Optional[asyncio.Task] = None
        self._analyser: Optional[EngineAnalyser] = None

    @property
    def available(self) -> bool:
        return bool(self.engine_path) and self.depth > 0

    def start(self) -> None:
        if self.available and self._task is None:
            self._task = asyncio.create_task(self._worker(), name="analysis-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._analyser is not None:
            await self._analyser.close()
            self._analyser = None

    def enqueue(self, game_id: str) -> bool:
        if not self.available or game_id in self._queued:
            return False
        self._queued.add(game_id)
        self._queue.put_nowait(game_id)
        return True

    def enqueue_missing(self, tournament_id: Optional[str] = None) -> int:
        return sum(self.enqueue(gid) for gid in self.db.unanalysed_game_ids(tournament_id))

    def pending(self) -> int:
        return len(self._queued)

    def _get_analyser(self) -> EngineAnalyser:
        if self._analyser is None:
            self._analyser = EngineAnalyser(self.engine_path or "", depth=self.depth, nice=self.nice)
        return self._analyser

    async def analyse_now(self, game_id: str) -> Optional[dict[str, Any]]:
        game = self.db.get_game(game_id)
        if game is None or game.status != GameStatus.FINISHED:
            return None
        result = await self._get_analyser().analyse(game)
        store(self.db, result)
        self._queued.discard(game_id)
        if self.bus is not None:
            self.bus.publish({"type": "game_analysed", "game_id": game_id,
                              "players": {pid: s.to_dict() for pid, s in result.players.items()}})
        return self.db.get_analysis(game_id)

    async def _worker(self) -> None:
        while True:
            game_id = await self._queue.get()
            try:
                if self.db.get_analysis(game_id) is None:
                    await self.analyse_now(game_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the worker die
                log.exception("analysis of game %s failed", game_id)
                if self._analyser is not None:
                    await self._analyser.close()
                    self._analyser = None
            finally:
                self._queued.discard(game_id)
