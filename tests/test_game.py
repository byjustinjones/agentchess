import asyncio
import io

import chess
import chess.pgn
import pytest

from agentchess.db import Database
from agentchess.events import EventBus
from agentchess.game import build_pgn, play_game
from agentchess.models import (
    GameConfig,
    GameRecord,
    GameStatus,
    MoveRequest,
    MoveResponse,
    Opening,
    PlayerKind,
    PlayerSpec,
    Termination,
    new_id,
)
from agentchess.openings import get_opening
from agentchess.players.base import GameEnd, GameStart, Player
from agentchess.players.random_player import RandomPlayer


def spec(pid: str, kind: PlayerKind = PlayerKind.RANDOM, **config) -> PlayerSpec:
    return PlayerSpec(id=pid, name=pid.upper(), kind=kind, config=config)


def new_game(**config) -> GameRecord:
    return GameRecord(id=new_id("g"), white_id="w", black_id="b", config=GameConfig(**config))


class Scripted(Player):
    """Plays a fixed list of answers (str move, MoveResponse, Exception, or 'sleep')."""

    def __init__(self, pid: str, answers=(), fallback_random: bool = False) -> None:
        super().__init__(spec(pid))
        self.answers = list(answers)
        self.requests: list[MoveRequest] = []
        self.calls: list[str] = []
        self.fallback = RandomPlayer(spec(pid, seed=1)) if fallback_random else None

    async def start_game(self, info: GameStart) -> None:
        self.calls.append("start")

    async def get_move(self, request: MoveRequest) -> MoveResponse:
        self.requests.append(request)
        if not self.answers:
            if self.fallback:
                return await self.fallback.get_move(request)
            raise AssertionError("script exhausted")
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        if a == "sleep":
            await asyncio.sleep(10)
        if isinstance(a, MoveResponse):
            return a
        return MoveResponse(move=a, comment=f"playing {a}", usage={"input_tokens": 10, "output_tokens": 5})

    async def end_game(self, info: GameEnd) -> None:
        self.calls.append(f"end:{info.result}:{info.termination}")

    async def close(self) -> None:
        self.calls.append("close")


def assert_pgn_consistent(game: GameRecord) -> chess.pgn.Game:
    pg = chess.pgn.read_game(io.StringIO(game.pgn))
    assert pg is not None and not pg.errors
    moves = [m.uci() for m in pg.mainline_moves()]
    assert moves == [m.uci for m in game.moves]
    assert pg.headers["Result"] == game.result
    return pg


async def test_random_vs_random_completes():
    for seed in range(3):
        game = new_game(max_plies=400)
        white = RandomPlayer(spec("w", seed=seed))
        black = RandomPlayer(spec("b", seed=seed + 100))
        out = await play_game(game, white, black, names={"w": "White Bot", "b": "Black Bot"})
        assert out is game
        assert out.status == GameStatus.FINISHED
        assert out.result in ("1-0", "0-1", "1/2-1/2")
        assert out.termination is not None and out.finished_at and out.started_at
        pg = assert_pgn_consistent(out)
        assert pg.headers["White"] == "White Bot" and pg.headers["Termination"] == out.termination.value
        board = chess.Board()
        for m in out.moves:
            assert board.fen() != m.fen_after
            board.push_uci(m.uci)
            assert board.fen() == m.fen_after


async def test_illegal_moves_retry_then_forfeit():
    white = Scripted("w", ["e2e5", "banana", "Nf6"])
    black = Scripted("b", [])
    bus = EventBus()
    q = bus.subscribe()
    game = await play_game(new_game(max_illegal_attempts=3), white, black, bus=bus)
    assert game.result == "0-1"
    assert game.termination == Termination.ILLEGAL_MOVES
    assert [r.attempt for r in white.requests] == [1, 2, 3]
    assert white.requests[0].previous_error is None
    assert "e2e5" in white.requests[1].previous_error
    assert "banana" in white.requests[2].previous_error
    assert len({r.request_id for r in white.requests}) == 3
    events = [q.get_nowait() for _ in range(q.qsize())]
    illegal = [e for e in events if e["type"] == "illegal_move"]
    assert [e["attempt"] for e in illegal] == [1, 2, 3]
    assert white.calls == ["start", "end:0-1:illegal_moves", "close"]
    assert black.calls == ["start", "end:0-1:illegal_moves", "close"]


async def test_illegal_then_legal_is_recorded():
    white = Scripted("w", ["e5", "e4", "Nf3"], fallback_random=False)
    black = Scripted("b", ["e5", MoveResponse(resign=True)])
    game = await play_game(new_game(), white, black)
    assert game.termination == Termination.RESIGNATION
    assert game.result == "1-0"
    first = game.moves[0]
    assert first.uci == "e2e4" and len(first.illegal_attempts) == 1
    assert first.illegal_attempts[0].move == "e5"
    assert first.usage["input_tokens"] == 20  # usage of the illegal attempt is included
    assert first.total_elapsed_s >= first.elapsed_s
    assert "illegal=1" in game.pgn
    assert_pgn_consistent(game)


async def test_timeout_forfeit():
    white = Scripted("w", ["e4"])
    black = Scripted("b", ["sleep"])
    game = await play_game(new_game(move_timeout_s=0.05), white, black)
    assert game.result == "1-0"
    assert game.termination == Termination.TIMEOUT
    assert "black" in game.termination_detail
    assert black.calls[-1] == "close"


async def test_exception_is_error_forfeit():
    white = Scripted("w", [RuntimeError("boom")])
    black = Scripted("b", [])
    game = await play_game(new_game(), white, black)
    assert game.result == "0-1"
    assert game.termination == Termination.ERROR
    assert "boom" in game.termination_detail


class BadStart(Scripted):
    async def start_game(self, info: GameStart) -> None:
        raise ConnectionError("cannot start")


class SlowStart(Scripted):
    async def start_game(self, info: GameStart) -> None:
        await asyncio.sleep(10)


class BadClose(Scripted):
    async def close(self) -> None:
        raise RuntimeError("close failed")


async def test_start_game_failures_forfeit():
    white, black = Scripted("w"), BadStart("b")
    game = await play_game(new_game(), white, black)
    assert (game.result, game.termination) == ("1-0", Termination.ERROR)
    assert "start_game" in game.termination_detail
    assert white.calls[-1] == "close" and black.calls[-1] == "close"

    white, black = SlowStart("w"), Scripted("b")
    game = await play_game(new_game(move_timeout_s=0.05), white, black)
    assert (game.result, game.termination) == ("0-1", Termination.TIMEOUT)


async def test_close_errors_do_not_prevent_other_close():
    white = BadClose("w", [MoveResponse(resign=True)])
    black = Scripted("b")
    game = await play_game(new_game(), white, black)
    assert game.termination == Termination.RESIGNATION
    assert black.calls[-1] == "close"


async def test_threefold_repetition_is_automatic_draw():
    shuffle_w = ["Nf3", "Ng1", "Nf3", "Ng1"]
    shuffle_b = ["Nf6", "Ng8", "Nf6", "Ng8"]
    game = await play_game(new_game(), Scripted("w", shuffle_w), Scripted("b", shuffle_b))
    assert game.result == "1/2-1/2"
    assert game.termination == Termination.THREEFOLD_REPETITION
    assert len(game.moves) == 8


async def test_max_plies_draw():
    game = await play_game(new_game(max_plies=10), RandomPlayer(spec("w", seed=1)), RandomPlayer(spec("b", seed=2)))
    assert game.termination == Termination.MAX_PLIES
    assert game.result == "1/2-1/2"
    assert len(game.moves) == 10


async def test_checkmate():
    white = Scripted("w", ["f3", "g4"])
    black = Scripted("b", ["e5", "Qh4#"])
    game = await play_game(new_game(), white, black)
    assert (game.result, game.termination) == ("0-1", Termination.CHECKMATE)
    assert game.pgn.strip().endswith("0-1")


async def test_opening_book_and_requests():
    game = new_game(show_legal_moves=True)
    game.opening = get_opening("ruy-lopez")
    white = Scripted("w", [MoveResponse(resign=True)])
    black = Scripted("b")
    bus = EventBus()
    q = bus.subscribe()
    await play_game(game, white, black, bus=bus, names={"b": "Opponent"})
    book = game.moves[:8]
    assert [m.uci for m in book] == game.opening.moves_uci
    assert all(m.comment == "book" and m.usage == {"book": True} and m.elapsed_s == 0 for m in book)
    req = white.requests[0]
    assert req.ply == 8 and req.move_number == 5 and req.color == "white"
    assert req.history_uci == game.opening.moves_uci
    assert req.pgn.startswith("1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6")
    assert req.opponent_name == "Opponent"
    assert "O-O" in req.legal_moves_san and "e1g1" in req.legal_moves_uci
    assert len(req.legal_moves_san) == len(req.legal_moves_uci)
    assert req.time_limit_s == game.config.move_timeout_s
    assert "8 |" in req.ascii_board
    assert not black.requests
    events = [q.get_nowait() for _ in range(q.qsize())]
    types = [e["type"] for e in events]
    assert types[0] == "game_started" and types[-1] == "game_finished"
    assert types.count("move") == 8 and "thinking" in types
    assert events[0]["black_name"] == "Opponent"
    assert "{ book }" in game.pgn and game.pgn.count("[Opening ") == 1


async def test_hidden_legal_moves():
    white = Scripted("w", [MoveResponse(resign=True)])
    await play_game(new_game(show_legal_moves=False), white, Scripted("b"))
    assert white.requests[0].legal_moves_uci == [] and white.requests[0].legal_moves_san == []


async def test_db_persistence():
    db = Database(":memory:")
    for pid in ("w", "b"):
        db.add_player(spec(pid))
    game = new_game(max_plies=12)
    game.opening = Opening(id="x", name="King's Pawn", eco="C20", moves_uci=["e2e4", "e7e5"])
    db.add_game(game)
    await play_game(game, RandomPlayer(spec("w", seed=3)), RandomPlayer(spec("b", seed=4)), db=db)
    stored = db.get_game(game.id)
    assert stored.status == GameStatus.FINISHED
    assert stored.result == game.result and stored.termination == game.termination
    assert stored.pgn == game.pgn and stored.started_at and stored.finished_at
    assert [m.uci for m in stored.moves] == [m.uci for m in game.moves]
    stats = db.move_stats()
    assert stats["w"]["moves"] + stats["b"]["moves"] == len(game.moves) - 2  # book moves excluded
    assert db.rated_results()[0]["id"] == game.id


async def test_cancellation_closes_players_and_reraises():
    white = Scripted("w", ["sleep"])
    black = Scripted("b")
    game = new_game(move_timeout_s=30)
    task = asyncio.create_task(play_game(game, white, black))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert game.status == GameStatus.RUNNING
    assert white.calls[-1] == "close" and black.calls[-1] == "close"
    assert "end:*:aborted" in white.calls


async def test_build_pgn_unfinished_and_custom_fen():
    fen = "4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"
    game = GameRecord(id="g1", white_id="w", black_id="b", initial_fen=fen)
    assert "[Result \"*\"]" in build_pgn(game)
    white = Scripted("w", ["e4", MoveResponse(resign=True)])
    black = Scripted("b", ["Kd7"])
    await play_game(game, white, black)
    pg = assert_pgn_consistent(game)
    assert pg.headers["FEN"] == fen and pg.headers["SetUp"] == "1"


def test_movetext():
    from agentchess.game import movetext
    board = chess.Board()
    sans = ["e4", "e5", "Nf3"]
    assert movetext(board, sans) == board.variation_san([board.parse_san("e4")]) + " e5 2. Nf3"
    black_first = chess.Board("4k3/8/8/8/8/8/4P3/4K3 b - - 0 7")
    assert movetext(black_first, ["Kd7", "e4", "Ke6"]) == "7... Kd7 8. e4 Ke6"
    assert movetext(board, []) == ""
