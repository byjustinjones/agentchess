"""REST API + GUI event WebSocket tests (FastAPI TestClient, temp DB, no seeding)."""
from __future__ import annotations

import time

import chess.pgn
import io
import pytest
from fastapi.testclient import TestClient

from agentchess.server.app import Settings, create_app
from agentchess.server.registry import hash_token, seed_default_players, slugify

FAST_GAME = {"move_timeout_s": 10, "max_illegal_attempts": 3, "max_plies": 30, "show_legal_moves": True}


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "t.db"), seed=False, bootstrap=20,
                              web_dir=str(tmp_path / "noweb")))
    with TestClient(app) as c:
        yield c


def add(client, name, kind="random", **kw):
    r = client.post("/api/players", json={"name": name, "kind": kind, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def wait_for(fn, timeout=30.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError("timed out")


def test_health_and_placeholder(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "agentchess" in client.get("/").text
    assert client.get("/api/openings").json()
    assert isinstance(client.get("/api/players/presets").json(), list)
    assert client.get("/api/agents").json() == []


def test_slugify():
    assert slugify("Claude Opus 5.5!") == "claude-opus-5-5"
    assert slugify("  ") == "player"


def test_players_crud_and_validation(client):
    a = add(client, "Rand Bot")
    assert a["player"]["id"] == "rand-bot" and "token" not in a
    b = add(client, "Rand Bot")
    assert b["player"]["id"] == "rand-bot-2"
    remote = add(client, "Ext Agent", kind="remote")
    token = remote["token"]
    assert token.startswith("ac_") and remote["player"]["online"] is False

    players = client.get("/api/players").json()
    assert {p["id"] for p in players} == {"rand-bot", "rand-bot-2", "ext-agent"}
    assert next(p for p in players if p["id"] == "rand-bot")["online"] is None

    # validation
    assert client.post("/api/players", json={"name": "x", "kind": "wizard"}).status_code == 400
    assert client.post("/api/players", json={"kind": "random"}).status_code == 400
    assert client.post("/api/players", json={"name": "llm", "kind": "llm", "config": {}}).status_code == 400
    assert client.post("/api/players", json={"name": "sf", "kind": "engine",
                                             "config": {"skill_level": 99}}).status_code == 400
    assert client.post("/api/players", json={"name": "sf", "kind": "engine",
                                             "config": {"uci_elo": 500}}).status_code == 400
    assert client.post("/api/players", json={"name": "x", "kind": "random",
                                             "max_concurrent_games": 0}).status_code == 400
    assert client.post("/api/players", json={"preset": "nope"}).status_code == 400
    r = client.post("/api/players", json={"preset": "random"})
    assert r.status_code == 201 and r.json()["player"]["id"] == "random"

    # get / patch / token / delete
    d = client.get("/api/players/rand-bot").json()
    assert d["stats"]["games"] == 0 and d["rating"] is None
    assert client.get("/api/players/nope").status_code == 404
    p = client.patch("/api/players/rand-bot", json={"name": "Renamed", "anchor_elo": 800}).json()
    assert p["name"] == "Renamed" and p["anchor_elo"] == 800
    p = client.patch("/api/players/rand-bot", json={"anchor_elo": None}).json()
    assert p["anchor_elo"] is None and p["name"] == "Renamed"
    assert client.patch("/api/players/nope", json={"name": "x"}).status_code == 404
    assert client.post("/api/players/rand-bot/token").status_code == 400
    new_tok = client.post("/api/players/ext-agent/token").json()["token"]
    assert new_tok != token
    assert client.get("/api/agent/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.get("/api/agent/me", headers={"Authorization": f"Bearer {new_tok}"}).status_code == 200
    assert client.delete("/api/players/rand-bot-2").json() == {"deleted": True, "deactivated": False}
    assert client.delete("/api/players/rand-bot-2").status_code == 404


def test_tournament_flow(client):
    for n in ("Alpha", "Beta", "Gamma"):
        add(client, n)
    cfg = {"player_ids": ["alpha", "beta", "gamma"], "games_per_pair": 2, "openings": "builtin",
           "concurrency": 3, "game": FAST_GAME}
    # validation
    assert client.post("/api/tournaments", json={"name": "bad", "config": {"player_ids": ["alpha"]}}).status_code == 400
    assert client.post("/api/tournaments", json={"name": "bad",
                                                 "config": {**cfg, "player_ids": ["alpha", "zzz"]}}).status_code == 400
    assert client.post("/api/tournaments", json={"name": "bad", "config": {**cfg, "format": "swiss"}}).status_code == 400
    assert client.post("/api/tournaments", json={"name": "bad", "config": {**cfg, "format": "gauntlet"}}).status_code == 400

    r = client.post("/api/tournaments", json={"name": "Mini", "config": cfg})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["status"] == "pending" and t["progress"]["total"] == 6
    tid = t["id"]
    assert client.get("/api/tournaments").json()[0]["id"] == tid
    assert client.post(f"/api/tournaments/{tid}/start").json()["status"] == "running"

    def finished():
        d = client.get(f"/api/tournaments/{tid}").json()
        return d if d["status"] == "finished" else None

    d = wait_for(finished, timeout=60)
    assert d["progress"]["finished"] == 6
    assert len(d["standings"]) == 3 and {s["player_id"] for s in d["standings"]} == {"alpha", "beta", "gamma"}
    assert d["crosstable"]["players"] == ["alpha", "beta", "gamma"]
    assert {p["id"] for p in d["players"]} == {"alpha", "beta", "gamma"}
    assert client.post(f"/api/tournaments/{tid}/start").status_code == 409

    games = client.get("/api/games", params={"tournament_id": tid}).json()
    assert games["total"] == 6 and len(games["games"]) == 6
    g0 = games["games"][0]
    assert g0["white_name"] in ("Alpha", "Beta", "Gamma") and "moves" not in g0
    assert client.get("/api/games", params={"status": "finished", "limit": 2}).json()["total"] == 6
    assert len(client.get("/api/games", params={"limit": 2, "order": "recent"}).json()["games"]) == 2
    assert client.get("/api/games", params={"status": "bogus"}).status_code == 400
    assert client.get("/api/games", params={"player_id": "alpha"}).json()["total"] == 4

    full = client.get(f"/api/games/{g0['id']}").json()
    assert full["moves"] and full["black_name"]
    pgn = client.get(f"/api/games/{g0['id']}/pgn")
    assert pgn.status_code == 200 and pgn.headers["content-type"].startswith("text/plain")
    parsed = chess.pgn.read_game(io.StringIO(pgn.text))
    assert parsed is not None and parsed.headers["Result"] == full["result"]

    allpgn = client.get("/api/pgn", params={"tournament_id": tid}).text
    assert allpgn.count("[Event ") == 6
    assert client.get("/api/pgn", params={"tournament_id": "nope"}).status_code == 404

    ratings = client.get("/api/ratings", params={"tournament_id": tid}).json()
    assert ratings["games"] == 6 and len(ratings["ratings"]) == 3
    assert set(ratings["stats"]) == {"alpha", "beta", "gamma"}
    assert ratings["stats"]["alpha"]["moves"] > 0
    again = client.get("/api/ratings", params={"tournament_id": tid}).json()
    assert again == ratings  # cached
    assert client.get("/api/ratings", params={"anchors": "false"}).json()["games"] == 6
    assert client.get("/api/players/alpha").json()["rating"]["games"] == 4

    assert client.get("/api/live").json() == {"games": []}
    assert client.delete(f"/api/tournaments/{tid}").json() == {"deleted": True}
    assert client.get(f"/api/tournaments/{tid}").status_code == 404
    # players with games are only deactivated
    assert client.delete("/api/players/alpha").json() == {"deleted": True, "deactivated": False}


def test_tournament_actions_and_errors(client):
    for n in ("P1", "P2"):
        add(client, n)
    t = client.post("/api/tournaments", json={"name": "slow", "config": {
        "player_ids": ["p1", "p2"], "games_per_pair": 2, "openings": "none", "game": FAST_GAME}}).json()
    tid = t["id"]
    assert client.post(f"/api/tournaments/{tid}/pause").json()["status"] == "paused"
    assert client.post(f"/api/tournaments/{tid}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/tournaments/{tid}/start").status_code == 409
    for action in ("start", "pause", "cancel"):
        assert client.post(f"/api/tournaments/nope/{action}").status_code == 404
    assert client.delete("/api/tournaments/nope").status_code == 404
    assert client.get("/api/games/nope").status_code == 404
    assert client.get("/api/games/nope/pgn").status_code == 404


def test_adhoc_game_and_ws_events(client):
    add(client, "W")
    add(client, "B")
    assert client.post("/api/games", json={"white_id": "w", "black_id": "w"}).status_code == 400
    assert client.post("/api/games", json={"white_id": "w", "black_id": "zz"}).status_code == 400
    assert client.post("/api/games", json={"white_id": "w", "black_id": "b",
                                           "opening_id": "nope"}).status_code == 400
    assert client.post("/api/games", json={"white_id": "w", "black_id": "b",
                                           "config": {"max_plies": 0}}).status_code == 400
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        r = client.post("/api/games", json={"white_id": "w", "black_id": "b", "config": FAST_GAME,
                                            "opening_id": client.get("/api/openings").json()[0]["id"]})
        assert r.status_code == 201, r.text
        gid = r.json()["id"]
        seen = set()
        while True:
            ev = ws.receive_json()
            seen.add(ev["type"])
            if ev["type"] == "game_finished" and ev["game"]["id"] == gid:
                break
        assert {"game_started", "move"} <= seen
    g = client.get(f"/api/games/{gid}").json()
    assert g["status"] == "finished" and g["opening"] is not None


def test_seed_default_players(tmp_path):
    from agentchess.db import Database

    db = Database(str(tmp_path / "s.db"))
    added = seed_default_players(db)
    assert added and added[0].id == "random"
    assert seed_default_players(db) == []  # only on an empty DB
    db.close()


def test_token_hash_is_sha256():
    assert len(hash_token("ac_x")) == 64


def test_load_dotenv(tmp_path, monkeypatch):
    from agentchess.cli import load_dotenv
    env = tmp_path / ".env"
    env.write_text("# comment\nAGENTCHESS_T1=abc\nexport AGENTCHESS_T2='q v'\nAGENTCHESS_T3=keep\n")
    env.chmod(0o600)
    monkeypatch.delenv("AGENTCHESS_T1", raising=False)
    monkeypatch.delenv("AGENTCHESS_T2", raising=False)
    monkeypatch.setenv("AGENTCHESS_T3", "existing")
    assert load_dotenv(env) == ["AGENTCHESS_T1", "AGENTCHESS_T2"]
    import os
    assert os.environ["AGENTCHESS_T1"] == "abc" and os.environ["AGENTCHESS_T2"] == "q v"
    assert os.environ["AGENTCHESS_T3"] == "existing"
    monkeypatch.delenv("AGENTCHESS_T1"); monkeypatch.delenv("AGENTCHESS_T2")
