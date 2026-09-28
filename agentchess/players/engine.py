"""EnginePlayer: a UCI engine (normally Stockfish) at a configured strength.

One engine process per game: it is started in ``start_game`` (or lazily on the
first ``get_move``) and quit in ``close``.

Strength notes: Stockfish's ``UCI_Elo`` range is 1320..3190 (Stockfish 16);
requested values outside the engine's advertised range are clamped to it and a
warning is logged. For strengths below ~1320 use ``skill_level`` (0..20),
optionally combined with a shallow ``depth``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any, Optional

import chess
import chess.engine

from agentchess.models import MoveRequest, MoveResponse, PlayerSpec
from agentchess.players.base import GameStart, Player

log = logging.getLogger(__name__)

STOCKFISH_CANDIDATES = ("/usr/games/stockfish", "/usr/local/bin/stockfish", "/opt/homebrew/bin/stockfish")
STOCKFISH_ELO_MIN = 1320
STOCKFISH_ELO_MAX = 3190


def find_stockfish() -> Optional[str]:
    """Locate a Stockfish binary: $STOCKFISH_PATH, PATH, then common install locations."""
    env = os.environ.get("STOCKFISH_PATH")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    found = shutil.which("stockfish")
    if found:
        return found
    for path in STOCKFISH_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _clamp(value: int, lo: Optional[int], hi: Optional[int]) -> int:
    if lo is not None:
        value = max(value, lo)
    if hi is not None:
        value = min(value, hi)
    return value


class EnginePlayer(Player):
    """Config keys (see docs/ARCHITECTURE.md): path, uci_elo, skill_level, movetime_ms,
    depth, nodes, threads, hash_mb, options."""

    def __init__(self, spec: PlayerSpec, default_path: Optional[str] = None) -> None:
        super().__init__(spec)
        cfg = spec.config or {}
        self.path: Optional[str] = cfg.get("path") or default_path
        self.uci_elo: Optional[int] = int(cfg["uci_elo"]) if cfg.get("uci_elo") is not None else None
        self.skill_level: Optional[int] = int(cfg["skill_level"]) if cfg.get("skill_level") is not None else None
        movetime = cfg.get("movetime_ms", 100)
        self.movetime_ms: Optional[float] = float(movetime) if movetime else None
        self.depth: Optional[int] = int(cfg["depth"]) if cfg.get("depth") else None
        self.nodes: Optional[int] = int(cfg["nodes"]) if cfg.get("nodes") else None
        self.threads: int = int(cfg.get("threads") or 1)
        self.hash_mb: int = int(cfg.get("hash_mb") or 16)
        self.extra_options: dict[str, Any] = dict(cfg.get("options") or {})
        self.effective_elo: Optional[int] = None   # UCI_Elo actually sent (after clamping)
        self._engine: Optional[chess.engine.UciProtocol] = None
        self._transport: Optional[asyncio.SubprocessTransport] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ helpers
    def limit(self) -> chess.engine.Limit:
        time_s = self.movetime_ms / 1000.0 if self.movetime_ms else None
        if time_s is None and self.depth is None and self.nodes is None:
            time_s = 0.1
        return chess.engine.Limit(time=time_s, depth=self.depth, nodes=self.nodes)

    def _build_options(self, engine: chess.engine.UciProtocol) -> dict[str, Any]:
        available = engine.options
        opts: dict[str, Any] = {}

        def put(name: str, value: Any) -> None:
            if name in available:
                opt = available[name]
                if isinstance(value, int) and not isinstance(value, bool) and opt.type == "spin":
                    clamped = _clamp(value, opt.min, opt.max)
                    if clamped != value:
                        log.warning("%s: %s=%s out of range [%s, %s]; clamped to %s",
                                    self.id, name, value, opt.min, opt.max, clamped)
                    value = clamped
                opts[name] = value
            else:
                log.warning("%s: engine has no option %r; ignored", self.id, name)

        put("Threads", self.threads)
        put("Hash", self.hash_mb)
        if self.skill_level is not None:
            put("Skill Level", self.skill_level)
        if self.uci_elo is not None:
            put("UCI_LimitStrength", True)
            put("UCI_Elo", self.uci_elo)
            self.effective_elo = opts.get("UCI_Elo")
        # Explicit user options are passed through verbatim (unknown ones make configure() fail loudly).
        opts.update(self.extra_options)
        # python-chess manages these itself.
        for managed in ("UCI_Chess960", "Ponder", "MultiPV", "UCI_Variant"):
            opts.pop(managed, None)
        return opts

    async def _ensure_engine(self) -> chess.engine.UciProtocol:
        async with self._lock:
            if self._engine is not None:
                return self._engine
            path = self.path or find_stockfish()
            if not path:
                raise RuntimeError("Stockfish not found: install it, set $STOCKFISH_PATH, or set config.path")
            try:
                transport, engine = await chess.engine.popen_uci(path)
            except (OSError, chess.engine.EngineError) as e:
                raise RuntimeError(f"could not start engine {path!r}: {e}") from e
            self._transport, self._engine = transport, engine
            try:
                await engine.configure(self._build_options(engine))
            except Exception:
                await self._shutdown()
                raise
            return engine

    async def _shutdown(self) -> None:
        engine, transport = self._engine, self._transport
        self._engine, self._transport = None, None
        if engine is not None:
            try:
                await asyncio.wait_for(engine.quit(), timeout=5)
            except Exception:  # noqa: BLE001 - engine may already be dead
                pass
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------------- lifecycle
    async def start_game(self, info: GameStart) -> None:
        await self._ensure_engine()

    async def get_move(self, request: MoveRequest) -> MoveResponse:
        engine = await self._ensure_engine()
        board = _board_from_request(request)
        try:
            result = await engine.play(board, self.limit(), game=request.game_id,
                                       info=chess.engine.INFO_BASIC | chess.engine.INFO_SCORE)
        except (chess.engine.EngineTerminatedError, chess.engine.EngineError) as e:
            await self._shutdown()
            raise RuntimeError(f"engine failure: {e}") from e
        if result.resigned:
            return MoveResponse(resign=True, comment="engine resigned")
        if result.move is None:
            raise RuntimeError("engine returned no move")
        return MoveResponse(move=result.move.uci(), comment=_describe(result.info, board.turn),
                            usage=_usage(result.info))

    async def close(self) -> None:
        await self._shutdown()


def _board_from_request(req: MoveRequest) -> chess.Board:
    """Rebuild the game with its move stack (so the engine sees repetitions); fall back to the FEN."""
    try:
        board = chess.Board(req.initial_fen)
        for uci in req.history_uci:
            board.push_uci(uci)
        if board.fen() == req.fen:
            return board
    except ValueError:
        pass
    return chess.Board(req.fen)


def _describe(info: dict[str, Any], turn: chess.Color) -> Optional[str]:
    parts = []
    if "depth" in info:
        parts.append(f"depth {info['depth']}")
    score = info.get("score")
    if score is not None:
        pov = score.pov(turn)
        mate = pov.mate()
        parts.append(f"mate {mate}" if mate is not None else f"eval {pov.score() / 100:+.2f}")
    if "nodes" in info:
        parts.append(f"{info['nodes']} nodes")
    return ", ".join(parts) or None


def _usage(info: dict[str, Any]) -> dict[str, Any]:
    return {k: info[k] for k in ("depth", "nodes") if k in info}
