"""Post-game engine analysis: the engine-agnostic core, storage/aggregation, API and auto-analysis."""
from __future__ import annotations

import time

import chess
import pytest
from fastapi.testclient import TestClient

from agentchess.analysis import (
    BLUNDER_CP,
    CLAMP_CP,
    AnalysisService,
    EngineAnalyser,
    analyse_moves,
    classify,
    store,
)
from agentchess.db import Database
from agentchess.models import GameRecord, GameStatus, MoveRecord, PlayerKind, PlayerSpec
from agentchess.players.engine import find_stockfish
from agentchess.server.app import Settings, create_app

STOCKFISH = find_stockfish()
needs_stockfish = pytest.mark.skipif(STOCKFISH is None, reason="stockfish not installed")


def make_game(uci_moves: list[str], result: str = "1-0", book: int = 0, gid: str = "g1") -> GameRecord:
    board = chess.Board()
    moves = []
    for i, u in enumerate(uci_moves):
        mv = chess.Move.from_uci(u)
        san = board.san(mv)
        board.push(mv)
        moves.append(MoveRecord(ply=i, color="white" if i % 2 == 0 else "black", uci=u, san=san,
                                fen_after=board.fen(), elapsed_s=0.1, total_elapsed_s=0.1,
                                comment="book" if i < book else None, usage={"book": True} if i < book else {}))
    return GameRecord(id=gid, white_id="w", black_id="b", status=GameStatus.FINISHED, result=result, moves=moves)


def table_evaluator(table: dict[str, tuple[int, str | None]]):
    """Evaluator answering from {fen-prefix: (cp for side to move, best uci)}; unknown -> (0, None)."""
    calls: list[str] = []

    async def evaluate(board: chess.Board):
        calls.append(board.fen())
        key = " ".join(board.fen().split()[:2])
        cp, best = table.get(key, (0, None))
        return cp, (chess.Move.from_uci(best) if best else None)

    evaluate.calls = calls  # type: ignore[attr-defined]
    return evaluate


def test_classify_thresholds():
    assert classify(0) is None and classify(49) is None
    assert classify(50) == "inaccuracy" and classify(99) == "inaccuracy"
    assert classify(100) == "mistake" and classify(299) == "mistake"
    assert classify(BLUNDER_CP) == "blunder"


async def test_analyse_moves_losses_and_summaries():
    # 1. e4 e5 2. Qh5?? (say the table calls it a blunder) Nc6 3. Bc4 Nf6?? 4. Qxf7#
    game = make_game(["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"], book=2)
    b = chess.Board()
    fens = []
    for u in ["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6"]:
        b.push_uci(u)
        fens.append(" ".join(b.fen().split()[:2]))
    table = {
        fens[1]: (30, "g1f3"),      # white to move after 1...e5: +0.3, best Nf3
        fens[2]: (20, "b8c6"),      # black to move after Qh5: -(-0.2) -> white's Qh5 lost 50 -> inaccuracy
        fens[3]: (10, "f1c4"),      # white to move: +0.1
        fens[4]: (-40, "g7g6"),     # black to move after Bc4: black is -0.4 (white +0.4: Bc4 gained)
        fens[5]: (1000, "h5f7"),    # white mates: black's Nf6 lost 1000-40 -> blunder
    }
    ev = table_evaluator(table)
    moves, players = await analyse_moves(game, ev)
    # book plies are skipped, checkmate needs no engine call; each position analysed once
    assert [m.ply for m in moves] == [2, 3, 4, 5, 6]
    assert len(ev.calls) == 5
    by_ply = {m.ply: m for m in moves}
    assert by_ply[2].eval_before == 30 and by_ply[2].eval_after == -20 and by_ply[2].loss == 50
    assert by_ply[2].best_uci == "g1f3" and by_ply[2].judged
    assert by_ply[5].loss == 1000 - 40 and by_ply[5].player_id == "b"
    assert by_ply[6].eval_after == CLAMP_CP and by_ply[6].loss == 0 and by_ply[6].best_uci == "h5f7"
    w, bl = players["w"], players["b"]
    # the mating move is played in an already decided position (+10 -> mate) and is not judged
    assert w.moves == 2 and w.inaccuracies == 1 and w.blunders == 0 and w.best_moves == 1 and w.won
    assert bl.moves == 2 and bl.blunders == 1 and not bl.won and not bl.missed_win
    assert w.acpl == pytest.approx((50 + 0) / 2)


async def test_missed_win_and_decided_positions_not_judged():
    game = make_game(["e2e4", "e7e5", "g1f3", "b8c6"], result="1/2-1/2")
    # white is completely winning throughout (+9) and draws: missed win; the decided positions
    # are not judged for either side.
    ev = table_evaluator({})

    async def evaluate(board: chess.Board):
        return (900 if board.turn == chess.WHITE else -900), None

    moves, players = await analyse_moves(game, evaluate)
    assert all(not m.judged for m in moves)
    assert players["w"].moves == 0 and players["w"].acpl is None
    assert players["w"].missed_win is True and players["b"].missed_win is False
    _ = ev


def test_db_roundtrip_and_stats(tmp_path):
    db = Database(str(tmp_path / "a.db"))
    for pid in ("w", "b"):
        db.add_player(PlayerSpec(id=pid, name=pid, kind=PlayerKind.RANDOM))
    game = make_game(["e2e4", "e7e5"], gid="g1")
    db.add_game(game)
    db.update_game(game)
    assert db.unanalysed_game_ids() == ["g1"]
    moves = [{"ply": 0, "player_id": "w", "eval_before": 30, "eval_after": 20, "loss": 10, "best_uci": "d2d4",
              "judged": True},
             {"ply": 1, "player_id": "b", "eval_before": -20, "eval_after": -400, "loss": 380, "best_uci": "e7e5",
              "judged": True}]
    players = {"w": {"moves": 1, "total_loss": 10, "blunders": 0, "mistakes": 0, "inaccuracies": 0,
                     "best_moves": 0, "max_eval": 400, "won": True, "missed_win": False},
               "b": {"moves": 1, "total_loss": 380, "blunders": 1, "mistakes": 0, "inaccuracies": 0,
                     "best_moves": 0, "max_eval": -20, "won": False, "missed_win": False}}
    db.save_analysis("g1", "TestEngine", 7, moves, players)
    assert db.unanalysed_game_ids() == []
    a = db.get_analysis("g1")
    assert a["engine"] == "TestEngine" and a["depth"] == 7
    assert [m["loss"] for m in a["moves"]] == [10, 380] and a["moves"][0]["judged"] is True
    assert a["players"]["b"]["acpl"] == 380 and a["players"]["w"]["won"] is True
    st = db.analysis_stats()
    assert st["b"]["blunders_per_100"] == 100.0 and st["w"]["acpl"] == 10 and st["w"]["analysed_games"] == 1
    # saving again replaces
    db.save_analysis("g1", "TestEngine", 9, moves[:1], {"w": players["w"]})
    assert db.get_analysis("g1")["depth"] == 9 and len(db.get_analysis("g1")["moves"]) == 1
    assert "b" not in db.analysis_stats()
    db.delete_analysis("g1")
    assert db.get_analysis("g1") is None and db.unanalysed_game_ids() == ["g1"]
    db.close()


@needs_stockfish
async def test_engine_analyser_scholars_mate():
    game = make_game(["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"])
    analyser = EngineAnalyser(STOCKFISH, depth=8)
    try:
        res = await analyser.analyse(game)
    finally:
        await analyser.close()
    assert res.engine.lower().startswith("stockfish") and res.depth == 8
    last = {m.ply: m for m in res.moves}
    assert last[5].loss >= BLUNDER_CP and last[5].player_id == "b"     # Nf6?? allows mate
    assert res.players["b"].blunders >= 1 and res.players["w"].won
    assert last[6].eval_after == CLAMP_CP                                # mate delivered
    with pytest.raises(ValueError):
        game.status = GameStatus.RUNNING
        await analyser.analyse(game)


@needs_stockfish
def test_api_analysis_endpoints_and_auto_analysis(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "t.db"), seed=False, bootstrap=10,
                              web_dir=str(tmp_path / "noweb"), analysis_depth=6))
    with TestClient(app) as client:
        assert client.get("/api/analysis/status").json()["available"] is True
        for n in ("A", "B"):
            client.post("/api/players", json={"name": n, "kind": "random"})
        r = client.post("/api/games", json={"white_id": "a", "black_id": "b",
                                            "config": {"move_timeout_s": 5, "max_plies": 20}})
        gid = r.json()["id"]
        assert client.get(f"/api/games/{gid}/analysis").status_code == 404
        assert client.post(f"/api/games/{gid}/analysis").status_code in (409, 200)  # running -> 409

        def analysed():
            r = client.get(f"/api/games/{gid}/analysis")
            return r.json() if r.status_code == 200 else None

        deadline = time.time() + 60
        while time.time() < deadline and analysed() is None:
            time.sleep(0.2)
        a = analysed()
        assert a is not None, "auto-analysis did not run"
        assert a["depth"] == 6 and set(a["players"]) == {"a", "b"} and len(a["moves"]) > 0
        stats = client.get("/api/ratings").json()["stats"]
        assert stats["a"]["analysed_games"] == 1 and "acpl" in stats["a"]
        # explicit re-analysis works and returns the stored form
        again = client.post(f"/api/games/{gid}/analysis").json()
        assert again["game_id"] == gid and again["depth"] == 6
        assert client.get(f"/api/players/a").json()["stats"]["analysed_games"] == 1
        assert client.post("/api/games/nope/analysis").status_code == 404


def test_api_analysis_disabled(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "t.db"), seed=False, bootstrap=10,
                              web_dir=str(tmp_path / "noweb"), analysis_depth=0))
    with TestClient(app) as client:
        assert client.get("/api/analysis/status").json()["available"] is False
        client.post("/api/players", json={"name": "A", "kind": "random"})
        client.post("/api/players", json={"name": "B", "kind": "random"})
        t = client.post("/api/tournaments", json={"name": "x", "config": {"player_ids": ["a", "b"], "openings": "none",
                                                                          "games_per_pair": 1}}).json()
        assert client.post(f"/api/tournaments/{t['id']}/analyse").status_code == 503


async def test_service_enqueue_dedup(tmp_path):
    db = Database(":memory:")
    svc = AnalysisService(db, None, depth=12)
    assert not svc.available and not svc.enqueue("g")
    svc2 = AnalysisService(db, "/nonexistent/engine", depth=12)
    assert svc2.available
    assert svc2.enqueue("g") and not svc2.enqueue("g") and svc2.pending() == 1
    _ = store
