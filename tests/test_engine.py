import chess
import pytest

from agentchess.game import play_game
from agentchess.models import GameConfig, GameRecord, GameStatus, MoveRequest, PlayerKind, PlayerSpec, Termination
from agentchess.players.base import PlayerContext
from agentchess.players.engine import EnginePlayer, find_stockfish
from agentchess.players.factory import PRESETS, create_player, default_max_concurrency, preset_spec
from agentchess.players.random_player import RandomPlayer
from agentchess.players.remote import AgentHub, RemotePlayer

STOCKFISH = find_stockfish()
needs_stockfish = pytest.mark.skipif(STOCKFISH is None, reason="stockfish not installed")


def engine_spec(**config) -> PlayerSpec:
    return PlayerSpec(id="sf", name="Stockfish", kind=PlayerKind.ENGINE, config=config)


def request(board: chess.Board) -> MoveRequest:
    return MoveRequest(request_id="r", game_id="g", color="white" if board.turn else "black", fen=board.fen(),
                       initial_fen=chess.STARTING_FEN, ply=0, move_number=board.fullmove_number, history_san=[],
                       history_uci=[m.uci() for m in board.move_stack], pgn="", legal_moves_uci=[],
                       legal_moves_san=[], opponent_name="x", time_limit_s=5)


@needs_stockfish
async def test_stockfish_beats_random():
    game = GameRecord(id="g-sf", white_id="sf", black_id="rnd", config=GameConfig(max_plies=300, move_timeout_s=10))
    sf = EnginePlayer(engine_spec(movetime_ms=10))
    rnd = RandomPlayer(PlayerSpec(id="rnd", name="Random", kind=PlayerKind.RANDOM, config={"seed": 7}))
    await play_game(game, sf, rnd)
    assert game.status == GameStatus.FINISHED
    assert game.result == "1-0", (game.termination, game.pgn)
    assert game.termination == Termination.CHECKMATE
    assert sf._engine is None  # closed


@needs_stockfish
async def test_engine_lazy_start_and_mate_in_one():
    player = EnginePlayer(engine_spec(movetime_ms=50))
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")
    try:
        resp = await player.get_move(request(board))  # no start_game: engine starts lazily
        assert resp.move == "a1a8"
        assert "mate" in (resp.comment or "")
    finally:
        await player.close()
    await player.close()  # idempotent


@needs_stockfish
async def test_uci_elo_is_clamped():
    player = EnginePlayer(engine_spec(uci_elo=800, movetime_ms=10))
    try:
        await player._ensure_engine()
        assert player.effective_elo == 1320
    finally:
        await player.close()


@needs_stockfish
async def test_engine_crash_raises():
    player = EnginePlayer(engine_spec(movetime_ms=10))
    await player._ensure_engine()
    player._transport.kill()
    with pytest.raises(RuntimeError):
        await player.get_move(request(chess.Board()))
    await player.close()


async def test_missing_engine_raises():
    player = EnginePlayer(engine_spec(path="/nonexistent/stockfish"))
    with pytest.raises(RuntimeError):
        await player.get_move(request(chess.Board()))


def test_factory_and_presets():
    ctx = PlayerContext()
    ids = [p["id"] for p in PRESETS]
    for expected in ("random", "sf-skill0-d1", "sf-skill3", "sf-skill6", "sf-1320", "sf-1500",
                     "sf-1800", "sf-2100", "sf-2500", "sf-max"):
        assert expected in ids
    assert len(ids) == len(set(ids))
    for p in PRESETS:
        spec = preset_spec(p["id"])
        assert isinstance(create_player(spec, ctx), (EnginePlayer, RandomPlayer))
        elo = p["config"].get("uci_elo")
        assert p["anchor_elo"] == elo
    assert default_max_concurrency(PlayerKind.ENGINE) == 2
    assert default_max_concurrency("random") == 8
    assert default_max_concurrency(PlayerKind.LLM) == 4
    assert default_max_concurrency(PlayerKind.REMOTE) == 1

    remote = PlayerSpec(id="agent", name="Agent", kind=PlayerKind.REMOTE)
    with pytest.raises(ValueError):
        create_player(remote, ctx)
    player = create_player(remote, PlayerContext(agent_hub=AgentHub()))
    assert isinstance(player, RemotePlayer)
    with pytest.raises(ValueError):
        create_player(PlayerSpec(id="x", name="x", kind=PlayerKind.LLM, config={"provider": "nope"}), ctx)
