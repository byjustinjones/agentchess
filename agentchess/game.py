"""Game runner: plays one game between two Players, enforcing the rules.

The runner owns validation and adjudication. Players may return anything; bad
answers are recorded as illegal attempts and the player is asked again (with
``attempt+1`` and ``previous_error``) until ``max_illegal_attempts`` is reached.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Optional

import chess
import chess.pgn

from agentchess.db import MAX_COMMENT_CHARS, Database
from agentchess.events import EventBus
from agentchess.models import (
    GameRecord,
    GameStatus,
    MoveAttempt,
    MoveRecord,
    MoveRequest,
    MoveResponse,
    Termination,
    new_id,
    now,
)
from agentchess.moves import parse_move, render_ascii
from agentchess.players.base import GameEnd, GameStart, InfrastructureError, Player

log = logging.getLogger(__name__)

LIFECYCLE_TIMEOUT_S = 30.0   # end_game / close
_COLOR = {chess.WHITE: "white", chess.BLACK: "black"}


class _GameOver(Exception):
    def __init__(self, result: str, termination: Termination, detail: Optional[str] = None) -> None:
        super().__init__(detail or termination.value)
        self.result = result
        self.termination = termination
        self.detail = detail


def _loss(color: chess.Color) -> str:
    return "0-1" if color == chess.WHITE else "1-0"


def _forfeit(color: chess.Color, termination: Termination, detail: str) -> _GameOver:
    return _GameOver(_loss(color), termination, f"{_COLOR[color]}: {detail}")


def _raise_if_infrastructure(color: chess.Color, exc: BaseException) -> None:
    """Infrastructure failures abort the game (unrated) instead of forfeiting it."""
    if isinstance(exc, InfrastructureError):
        raise _GameOver("*", Termination.ABORTED, f"{_COLOR[color]}: infrastructure failure: {exc}")


# --------------------------------------------------------------- adjudication
def adjudicate(board: chess.Board) -> Optional[tuple[str, Termination]]:
    """Return (result, termination) if the game is over by the rules, else None.

    Threefold repetition and the fifty-move rule are applied automatically
    (no claim needed); fivefold / 75-move rules are covered by them.
    """
    if board.is_checkmate():
        return _loss(board.turn), Termination.CHECKMATE
    if board.is_stalemate():
        return "1/2-1/2", Termination.STALEMATE
    if board.is_insufficient_material():
        return "1/2-1/2", Termination.INSUFFICIENT_MATERIAL
    if board.is_repetition(3):
        return "1/2-1/2", Termination.THREEFOLD_REPETITION
    if board.is_fifty_moves():
        return "1/2-1/2", Termination.FIFTY_MOVES
    outcome = board.outcome()
    if outcome is not None:  # pragma: no cover - all standard cases handled above
        return outcome.result(), Termination.ABORTED
    return None


# ------------------------------------------------------------------------ PGN
def movetext(start: chess.Board, sans: list[str]) -> str:
    """PGN movetext ("1. e4 e5 2. Nf3") for SAN moves played from ``start``."""
    parts: list[str] = []
    number, white = start.fullmove_number, start.turn == chess.WHITE
    for i, san in enumerate(sans):
        if white:
            parts.append(f"{number}. {san}")
        else:
            parts.append(f"{number}... {san}" if i == 0 else san)
            number += 1
        white = not white
    return " ".join(parts)


def _pgn_date(ts: Optional[float]) -> str:
    return time.strftime("%Y.%m.%d", time.gmtime(ts)) if ts else "????.??.??"


def build_pgn(game: GameRecord, names: Optional[dict[str, str]] = None) -> str:
    """PGN text for ``game`` (finished or not; unfinished games get result ``*``)."""
    names = names or {}
    pg = chess.pgn.Game()
    h = pg.headers
    h["Event"] = f"agentchess {game.tournament_id}" if game.tournament_id else "agentchess exhibition"
    h["Site"] = "agentchess"
    h["Date"] = _pgn_date(game.started_at or game.created_at)
    h["Round"] = str(game.round) if game.round else "-"
    h["White"] = names.get(game.white_id, game.white_id)
    h["Black"] = names.get(game.black_id, game.black_id)
    h["Result"] = game.result if game.status == GameStatus.FINISHED and game.result else "*"
    h["GameId"] = game.id
    if game.termination:
        h["Termination"] = game.termination.value
    if game.termination_detail:
        h["TerminationDetail"] = game.termination_detail
    if game.opening:
        h["Opening"] = game.opening.name
        if game.opening.eco:
            h["ECO"] = game.opening.eco
    board = chess.Board(game.initial_fen)
    if game.initial_fen != chess.STARTING_FEN:
        pg.setup(board)
    node: chess.pgn.GameNode = pg
    for m in game.moves:
        try:
            move = chess.Move.from_uci(m.uci)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        board.push(move)
        node = node.add_variation(move)
        if m.usage.get("book"):
            node.comment = "book"
        else:
            parts = [f"time={m.elapsed_s:.1f}s"]
            if m.illegal_attempts:
                parts.append(f"illegal={len(m.illegal_attempts)}")
            node.comment = " ".join(parts)
    h["PlyCount"] = str(len(board.move_stack))
    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=True)
    return pg.accept(exporter)


# ---------------------------------------------------------------------- runner
class _Runner:
    def __init__(self, game: GameRecord, white: Player, black: Player, db: Optional[Database],
                 bus: Optional[EventBus], names: Optional[dict[str, str]]) -> None:
        self.game = game
        self.players = {chess.WHITE: white, chess.BLACK: black}
        self.ids = {chess.WHITE: game.white_id, chess.BLACK: game.black_id}
        self.db = db
        self.bus = bus
        self.names = {game.white_id: white.name, game.black_id: black.name, **(names or {})}
        self.config = game.config
        self.board = chess.Board(game.initial_fen)
        self.start_board = chess.Board(game.initial_fen)

    # ------------------------------------------------------------- utilities
    def publish(self, event: dict[str, Any]) -> None:
        if self.bus is not None:
            self.bus.publish(event)

    def name(self, color: chess.Color) -> str:
        pid = self.ids[color]
        return self.names.get(pid, pid)

    async def _call(self, aw: Awaitable[Any], timeout: Optional[float]) -> tuple[str, Any]:
        """Run a player coroutine: ("ok", value) | ("timeout", None) | ("error", exc).

        Only the player's own exceptions become "error"; cancellation of the
        runner itself propagates (after cancelling the player's task)."""
        try:
            task = asyncio.ensure_future(aw)
        except TypeError as e:  # player method is not a coroutine function
            return "error", e
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout if timeout and timeout > 0 else None)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.wait({task}, timeout=5)
            raise
        if not done:
            task.cancel()
            await asyncio.wait({task}, timeout=5)
            if task.done() and not task.cancelled():
                task.exception()  # mark retrieved
            return "timeout", None
        if task.cancelled():
            return "error", RuntimeError("player call was cancelled")
        exc = task.exception()
        if exc is not None:
            return "error", exc
        return "ok", task.result()

    # ------------------------------------------------------------ move flow
    def _request(self, attempt: int, previous_error: Optional[str]) -> MoveRequest:
        b = self.board
        show = self.config.show_legal_moves
        legal = list(b.legal_moves) if show else []
        opponent = self.ids[not b.turn]
        return MoveRequest(
            request_id=new_id("req"),
            game_id=self.game.id,
            color=_COLOR[b.turn],
            fen=b.fen(),
            initial_fen=self.game.initial_fen,
            ply=len(b.move_stack),
            move_number=b.fullmove_number,
            history_san=[m.san for m in self.game.moves],
            history_uci=[m.uci for m in self.game.moves],
            pgn=movetext(self.start_board, [m.san for m in self.game.moves]),
            legal_moves_uci=[m.uci() for m in legal],
            legal_moves_san=[b.san(m) for m in legal],
            opponent_name=self.names.get(opponent, opponent),
            time_limit_s=self.config.move_timeout_s,
            attempt=attempt,
            previous_error=previous_error,
            ascii_board=render_ascii(b),
        )

    def _record(self, move: chess.Move, color: chess.Color, elapsed: float, total: float,
                attempts: list[MoveAttempt], comment: Optional[str], usage: dict[str, Any]) -> MoveRecord:
        san = self.board.san(move)
        ply = len(self.board.move_stack)
        self.board.push(move)
        rec = MoveRecord(ply=ply, color=_COLOR[color], uci=move.uci(), san=san, fen_after=self.board.fen(),
                         elapsed_s=round(elapsed, 3), total_elapsed_s=round(total, 3),
                         illegal_attempts=attempts, comment=comment, usage=usage)
        self.game.moves.append(rec)
        if self.db is not None:
            self.db.add_move(self.game.id, self.ids[color], rec)
        self.publish({
            "type": "move", "game_id": self.game.id, "ply": ply, "uci": rec.uci, "san": san,
            "fen": rec.fen_after, "color": rec.color, "player_id": self.ids[color],
            "elapsed_s": rec.elapsed_s, "total_elapsed_s": rec.total_elapsed_s,
            "illegal_attempts": len(attempts), "comment": comment, "usage": usage,
        })
        return rec

    def play_book(self) -> None:
        opening = self.game.opening
        if not opening or not opening.moves_uci:
            return
        for uci in opening.moves_uci:
            if adjudicate(self.board):
                break
            try:
                move = chess.Move.from_uci(uci)
            except ValueError:
                move = None
            if move is None or move not in self.board.legal_moves:
                log.warning("game %s: opening %s has illegal move %s at ply %d; book stopped",
                            self.game.id, opening.id, uci, len(self.board.move_stack))
                break
            self._record(move, self.board.turn, 0.0, 0.0, [], "book", {"book": True})

    async def play_move(self) -> None:
        color = self.board.turn
        pid = self.ids[color]
        attempts: list[MoveAttempt] = []
        usage_total: dict[str, Any] = {}
        t_start = time.monotonic()
        try:
            await self._play_move(color, attempts, usage_total)
        except _GameOver:
            # The game ended during this move (forfeit, timeout, resignation, error, abort):
            # keep what the player did on it so stats and the GUI don't lose it.
            self.game.final_attempt = {
                "player_id": pid, "color": _COLOR[color], "ply": len(self.board.move_stack),
                "attempt": len(attempts) + 1, "illegal_attempts": [a.__dict__ for a in attempts],
                "usage": dict(usage_total), "elapsed_s": round(time.monotonic() - t_start, 3),
            }
            raise

    async def _play_move(self, color: chess.Color, attempts: list[MoveAttempt],
                         usage_total: dict[str, Any]) -> None:
        player = self.players[color]
        pid = self.ids[color]
        previous_error: Optional[str] = None
        t_start = time.monotonic()
        while True:
            req = self._request(len(attempts) + 1, previous_error)
            self.publish({"type": "thinking", "game_id": self.game.id, "player_id": pid,
                          "ply": req.ply, "attempt": req.attempt})
            t0 = time.monotonic()
            status, value = await self._call(player.get_move(req), self.config.move_timeout_s)
            elapsed = time.monotonic() - t0
            if status == "timeout":
                raise _forfeit(color, Termination.TIMEOUT,
                               f"no move within {self.config.move_timeout_s:g}s (ply {req.ply})")
            if status == "error":
                _raise_if_infrastructure(color, value)
                raise _forfeit(color, Termination.ERROR, f"{type(value).__name__}: {value}")
            resp = value
            if not isinstance(resp, MoveResponse):
                error = f"player returned {type(resp).__name__} instead of a move"
                move_text = ""
            else:
                _add_usage(usage_total, resp.usage)
                if resp.resign:
                    raise _forfeit(color, Termination.RESIGNATION, "resigned")
                move_text = resp.move if isinstance(resp.move, str) else str(resp.move or "")
                try:
                    move = parse_move(self.board, move_text)
                except ValueError as e:
                    error = str(e)
                else:
                    # Untrusted (e.g. a remote agent's WS JSON): a non-string comment must not
                    # crash persistence and turn the game into an unrated ABORTED one.
                    comment = resp.comment if resp.comment is None or isinstance(resp.comment, str) \
                        else str(resp.comment)
                    if comment is not None and len(comment) > MAX_COMMENT_CHARS:
                        comment = comment[:MAX_COMMENT_CHARS]   # same limit as storage; keeps WS events small
                    self._record(move, color, elapsed, time.monotonic() - t_start, attempts,
                                 comment, usage_total)
                    return
            attempts.append(MoveAttempt(move=move_text[:200], error=error, elapsed_s=round(elapsed, 3)))
            self.publish({"type": "illegal_move", "game_id": self.game.id, "player_id": pid,
                          "move": move_text[:200], "error": error, "attempt": len(attempts)})
            if len(attempts) >= max(1, self.config.max_illegal_attempts):
                raise _forfeit(color, Termination.ILLEGAL_MOVES,
                               f"{len(attempts)} illegal/unparseable answers at ply {req.ply} (last: {error})")
            previous_error = error

    # ---------------------------------------------------------------- main
    async def start_players(self) -> None:
        for color in (chess.WHITE, chess.BLACK):
            info = GameStart(game_id=self.game.id, color=_COLOR[color], opponent_id=self.ids[not color],
                             opponent_name=self.name(not color), initial_fen=self.game.initial_fen,
                             tournament_id=self.game.tournament_id)
            status, value = await self._call(self.players[color].start_game(info), self.config.move_timeout_s)
            if status == "timeout":
                raise _forfeit(color, Termination.TIMEOUT, "start_game timed out")
            if status == "error":
                _raise_if_infrastructure(color, value)
                raise _forfeit(color, Termination.ERROR, f"start_game failed: {type(value).__name__}: {value}")

    async def play(self) -> _GameOver:
        try:
            await self.start_players()
            self.play_book()
            while True:
                over = adjudicate(self.board)
                if over:
                    return _GameOver(over[0], over[1])
                max_plies = self.config.max_plies
                if max_plies and max_plies > 0 and len(self.board.move_stack) >= max_plies:
                    return _GameOver("1/2-1/2", Termination.MAX_PLIES, f"draw adjudicated after {max_plies} plies")
                await self.play_move()
        except _GameOver as g:
            return g

    async def end_players(self, info: GameEnd) -> None:
        for player in self.players.values():
            status, value = await self._call(player.end_game(info), LIFECYCLE_TIMEOUT_S)
            if status != "ok":
                log.warning("game %s: %s.end_game %s: %s", self.game.id, player.id, status, value)

    async def close_players(self) -> None:
        for player in self.players.values():
            try:
                status, value = await self._call(player.close(), LIFECYCLE_TIMEOUT_S)
            except asyncio.CancelledError:
                continue  # still close the other player; the caller re-raises the original cancel
            if status != "ok":
                log.warning("game %s: %s.close %s: %s", self.game.id, player.id, status, value)


def _add_usage(total: dict[str, Any], usage: Optional[dict[str, Any]]) -> None:
    """Accumulate numeric usage over attempts (tokens/cost of illegal answers count too)."""
    if not isinstance(usage, dict):
        return
    for k, v in usage.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and isinstance(total.get(k, 0), (int, float)):
            total[k] = total.get(k, 0) + v
        else:
            total[k] = v


async def play_game(
    game: GameRecord,
    white: Player,
    black: Player,
    db: Optional[Database] = None,
    bus: Optional[EventBus] = None,
    names: Optional[dict[str, str]] = None,
) -> GameRecord:
    """Play ``game`` to completion and return it (status FINISHED, result/termination/pgn set).

    With ``db``, the game (and both players) must already be stored (``db.add_game``).

    Persists to ``db`` and publishes events to ``bus`` as it goes. If the task is
    cancelled, both players are closed and ``CancelledError`` is re-raised with the
    game left RUNNING for the caller to abort or reschedule.
    """
    runner = _Runner(game, white, black, db, bus, names)
    game.moves = []
    game.status = GameStatus.RUNNING
    game.started_at = now()
    game.finished_at = None
    game.result = game.termination = game.termination_detail = game.pgn = None
    game.final_attempt = None
    if db is not None:
        db.clear_moves(game.id)
        db.update_game(game)
    runner.publish({"type": "game_started", "game": game.to_dict(include_moves=False),
                    "white_name": runner.name(chess.WHITE), "black_name": runner.name(chess.BLACK)})
    try:
        try:
            over = await runner.play()
        except asyncio.CancelledError:
            try:
                await runner.end_players(GameEnd(game.id, "*", Termination.ABORTED.value, "game cancelled"))
            except asyncio.CancelledError:
                pass
            raise
        game.status = GameStatus.ABORTED if over.termination == Termination.ABORTED else GameStatus.FINISHED
        game.result = over.result
        game.termination = over.termination
        game.termination_detail = over.detail
        game.finished_at = now()
        game.pgn = build_pgn(game, runner.names)
        if db is not None:
            db.update_game(game)
        runner.publish({"type": "game_finished", "game": game.to_dict(include_moves=False),
                        "white_name": runner.name(chess.WHITE), "black_name": runner.name(chess.BLACK)})
        await runner.end_players(GameEnd(game.id, over.result, over.termination.value, over.detail))
        return game
    finally:
        await runner.close_players()
