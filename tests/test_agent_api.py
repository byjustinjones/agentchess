"""External agent API: HTTP long-poll and WebSocket flows, auth failures."""
from __future__ import annotations

import random
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agentchess.server.app import Settings, create_app
from agentchess.server.registry import hash_token

GAME = {"move_timeout_s": 20, "max_illegal_attempts": 3, "max_plies": 24, "show_legal_moves": True}


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "a.db"), seed=False, bootstrap=10,
                              web_dir=str(tmp_path / "noweb")))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def agent(client):
    """(token, headers) of a registered remote player 'bot' plus a random opponent 'rnd'."""
    token = client.post("/api/players", json={"name": "Bot", "kind": "remote"}).json()["token"]
    client.post("/api/players", json={"name": "Rnd", "kind": "random"})
    return token, {"Authorization": f"Bearer {token}"}


def test_auth_failures(client, agent):
    token, headers = agent
    assert client.get("/api/agent/me").status_code == 401
    assert client.get("/api/agent/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/agent/turn", params={"wait": 0}).status_code == 401
    me = client.get("/api/agent/me", headers=headers).json()
    assert me["player"]["id"] == "bot" and me["online"] is True
    assert client.get("/api/agent/me", params={"token": token}).status_code == 200
    # a token pointing at a non-remote player is forbidden
    client.app.state.db.set_player_token("rnd", hash_token("ac_fake"))
    assert client.get("/api/agent/me", headers={"Authorization": "Bearer ac_fake"}).status_code == 403
    # deleting the player revokes the token
    client.delete("/api/players/bot")
    assert client.get("/api/agent/me", headers=headers).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/agent/ws?token=nope") as ws:
            assert ws.receive_json()["type"] == "error"
            ws.receive_json()


def test_turn_nothing_pending(client, agent):
    _, headers = agent
    t0 = time.time()
    r = client.get("/api/agent/turn", params={"wait": 0.3}, headers=headers)
    assert r.status_code == 204 and time.time() - t0 >= 0.25
    assert client.get("/api/agent/turn", params={"wait": -5}, headers=headers).status_code == 204
    assert client.get("/api/agent/games", headers=headers).json() == []
    assert client.post("/api/agent/games/nope/move", json={"move": "e2e4"}, headers=headers).status_code == 404
    online = [a for a in client.get("/api/agents").json() if a["player_id"] == "bot"]
    assert online and online[0]["online"] is True


def test_http_agent_full_game(client, agent):
    _, headers = agent
    r = client.post("/api/games", json={"white_id": "bot", "black_id": "rnd", "config": GAME})
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    # the random player may not answer for the agent
    other = client.post("/api/games", json={"white_id": "rnd", "black_id": "bot", "config": GAME}).json()["id"]

    illegal_checked = stale_checked = False
    for _ in range(400):
        r = client.get("/api/agent/turn", params={"wait": 1}, headers=headers)
        g = client.get(f"/api/games/{gid}").json()
        if r.status_code == 204:
            if g["status"] == "finished" and client.get(f"/api/games/{other}").json()["status"] == "finished":
                break
            continue
        req = r.json()
        assert req["color"] in ("white", "black") and req["legal_moves_uci"]
        state = client.get(f"/api/agent/games/{req['game_id']}", headers=headers).json()
        assert state["your_turn"] is True and state["request"]["request_id"] == req["request_id"]

        if not illegal_checked and req["game_id"] == gid:
            bad = client.post(f"/api/agent/games/{gid}/move", headers=headers,
                              json={"move": "e2e5", "request_id": req["request_id"]}).json()
            assert bad["accepted"] is True and bad["legal"] is False and bad["san"] is None
            assert bad["error"] and bad["attempts_remaining"] == 2
            retry = client.get("/api/agent/turn", params={"wait": 5}, headers=headers).json()
            while retry["game_id"] != gid:  # the other game may be first in the queue
                mv = random.choice(retry["legal_moves_uci"])
                client.post(f"/api/agent/games/{retry['game_id']}/move", headers=headers, json={"move": mv})
                retry = client.get("/api/agent/turn", params={"wait": 5}, headers=headers).json()
            assert retry["attempt"] == 2 and retry["previous_error"]
            assert retry["request_id"] != req["request_id"]
            illegal_checked = True
            req = retry

        if not stale_checked:
            r2 = client.post(f"/api/agent/games/{req['game_id']}/move", headers=headers,
                             json={"move": req["legal_moves_uci"][0], "request_id": "stale"})
            assert r2.status_code == 409
            stale_checked = True

        mv = random.choice(req["legal_moves_san"])
        res = client.post(f"/api/agent/games/{req['game_id']}/move", headers=headers,
                          json={"move": mv, "request_id": req["request_id"], "comment": "hi"}).json()
        assert res["accepted"] and res["legal"] and res["san"] == mv and res["error"] is None
    else:
        raise AssertionError("game did not finish")

    assert illegal_checked and stale_checked
    g = client.get(f"/api/games/{gid}").json()
    assert g["status"] == "finished" and g["result"] in ("1-0", "0-1", "1/2-1/2")
    assert any(m["illegal_attempts"] for m in g["moves"])
    assert any(m["comment"] == "hi" for m in g["moves"])
    # not your turn any more
    assert client.post(f"/api/agent/games/{gid}/move", json={"move": "e2e4"}, headers=headers).status_code == 409
    assert client.post(f"/api/agent/games/{gid}/resign", headers=headers).status_code == 409
    state = client.get(f"/api/agent/games/{gid}", headers=headers).json()
    assert state["status"] == "finished" and state["request"] is None and state["color"] == "white"


def test_http_agent_resign(client, agent):
    _, headers = agent
    gid = client.post("/api/games", json={"white_id": "bot", "black_id": "rnd", "config": GAME}).json()["id"]
    req = client.get("/api/agent/turn", params={"wait": 5}, headers=headers).json()
    games = client.get("/api/agent/games", headers=headers).json()
    assert games[0]["game_id"] == gid and games[0]["your_turn"] and games[0]["opponent"] == "Rnd"
    assert client.post(f"/api/agent/games/{gid}/resign", headers=headers,
                       json={"request_id": req["request_id"]}).json()["accepted"] is True
    for _ in range(50):
        g = client.get(f"/api/games/{gid}").json()
        if g["status"] == "finished":
            break
        time.sleep(0.1)
    assert g["result"] == "0-1" and g["termination"] == "resignation"


def test_websocket_agent_full_game(client, agent):
    token, _ = agent
    with client.websocket_connect(f"/api/agent/ws?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["player"]["id"] == "bot"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        ws.send_text("not json")
        assert ws.receive_json()["type"] == "error"
        gid = client.post("/api/games", json={"white_id": "rnd", "black_id": "bot", "config": GAME}).json()["id"]
        seen: set[str] = set()
        requests: set[str] = set()
        illegal_done = False
        while True:
            msg = ws.receive_json()
            seen.add(msg["type"])
            if msg["type"] == "move_request":
                req = msg["request"]
                assert req["request_id"] not in requests  # never pushed twice
                requests.add(req["request_id"])
                move = "a1a1" if not illegal_done else random.choice(req["legal_moves_uci"])
                ws.send_json({"type": "move", "game_id": req["game_id"], "move": move,
                              "request_id": req["request_id"]})
            elif msg["type"] == "move_result":
                if not illegal_done:
                    assert msg["legal"] is False and msg["attempts_remaining"] == 2
                    illegal_done = True
                else:
                    assert msg["legal"] is True and msg["san"]
            elif msg["type"] == "game_ended" and msg["game_id"] == gid:
                break
        assert {"game_started", "move_request", "move_result"} <= seen
        ws.send_json({"type": "move", "game_id": gid, "move": "e2e4"})
        err = ws.receive_json()
        assert err["type"] == "error" and err["status"] == 409
    g = client.get(f"/api/games/{gid}").json()
    assert g["status"] == "finished"
