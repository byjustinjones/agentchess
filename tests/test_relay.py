"""The Claude Code relay harness, driven with a fake ``claude`` against a real server."""
from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest
import uvicorn

from agentchess.relay import (
    FAIR_PLAY_ARGS,
    ClaudeRunner,
    Relay,
    RelayConfig,
    extract_move,
    format_position,
    parse_claude_json,
    usage_from_result,
)
from agentchess.server.app import Settings, create_app

FAKE = str(Path(__file__).parent / "fake_claude.py")


def free_port() -> int:
    for port in range(8450, 8500):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port in 8450-8499")


def sample_request(**kw) -> dict:
    req = {"request_id": "req_1", "game_id": "g_1", "color": "black", "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
           "initial_fen": "x", "ply": 1, "move_number": 1, "history_san": ["e4"], "history_uci": ["e2e4"],
           "pgn": "1. e4", "legal_moves_uci": ["e7e5", "c7c5"], "legal_moves_san": ["e5", "c5"],
           "opponent_name": "Stockfish 1500", "time_limit_s": 300, "attempt": 1, "previous_error": None,
           "ascii_board": "board"}
    req.update(kw)
    return req


def test_format_position_variants():
    p = format_position(sample_request(), new_session=True, takeover=False)
    assert "you play BLACK against Stockfish 1500" in p and "Legal moves (2): e5 c5" in p
    assert "teammate" not in p and p.rstrip().endswith("MOVE: <your move>")
    p2 = format_position(sample_request(attempt=2, previous_error="illegal move e4"), new_session=True, takeover=True)
    assert "teammate" in p2 and "ATTENTION" in p2 and "attempt 2" in p2
    p3 = format_position(sample_request(), new_session=False, takeover=False)
    assert "Game g_1" not in p3 and "FEN:" in p3


def test_parse_claude_json_and_usage():
    d = parse_claude_json('{"type":"result","result":"MOVE: e5","session_id":"s1","usage":{"input_tokens":10,"output_tokens":2},'
                          '"total_cost_usd":0.01,"num_turns":1,"duration_ms":5}')
    assert d["result"] == "MOVE: e5"
    u = usage_from_result(d, "opus")
    assert u == {"input_tokens": 10, "output_tokens": 2, "cost_usd": 0.01, "duration_ms": 5, "turns": 1, "model": "opus"}
    lst = parse_claude_json('[{"type":"system"},{"type":"result","result":"x","session_id":"s2"}]')
    assert lst["session_id"] == "s2"
    with pytest.raises(ValueError):
        parse_claude_json('"just a string"')


def test_extract_move_fallback():
    fen = sample_request()["fen"]
    assert extract_move(fen, "thinking...\nMOVE: c5") == "c5"
    assert extract_move(fen, "I will play e5 here") == "e7e5"
    assert extract_move(fen, "no move at all") is None


def test_command_has_fair_play_flags():
    cfg = RelayConfig(server="http://x", token="t", claude_cmd="claude", model="opus", effort="high",
                      extra_args=["--max-budget-usd", "1"])
    runner = ClaudeRunner(cfg)
    cmd = runner.command(None)
    assert cmd[:4] == ["claude", "-p", "--output-format", "json"]
    for flag in FAIR_PLAY_ARGS:
        assert flag in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--append-system-prompt" in cmd and "--model" in cmd and "--effort" in cmd
    assert cmd[-2:] == ["--max-budget-usd", "1"]
    resumed = runner.command("sess_1")
    assert "--resume" in resumed and "--append-system-prompt" not in resumed


async def test_runner_missing_binary():
    res = await ClaudeRunner(RelayConfig(server="x", token="t", claude_cmd="/nonexistent/claude")).ask("hi", None, 5)
    assert res.error and "could not start" in res.error


async def test_relay_plays_a_game_with_handovers(tmp_path, monkeypatch):
    port = free_port()
    app = create_app(Settings(db_path=str(tmp_path / "r.db"), seed=False, bootstrap=5,
                              web_dir=str(tmp_path / "noweb"), analysis_depth=0))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    state = tmp_path / "fake"
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state))
    monkeypatch.setenv("FAKE_CLAUDE_ILLEGAL_FIRST", "1")
    monkeypatch.setenv("FAKE_CLAUDE_NO_MOVE_LINE", "1")
    try:
        import httpx

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            token = (await c.post("/api/players", json={"name": "Relay Bot", "kind": "remote"})).json()["token"]
            await c.post("/api/players", json={"name": "Rnd", "kind": "random"})
            g = (await c.post("/api/games", json={"white_id": "relay-bot", "black_id": "rnd",
                                                  "config": {"move_timeout_s": 30, "max_plies": 16}})).json()
            gid = g["id"]
        cfg = RelayConfig(server=f"http://127.0.0.1:{port}", token=token,
                          claude_cmd=f"{sys.executable} {FAKE}", model="fake-model", moves_per_session=3,
                          max_games=1, poll_wait_s=2, log_dir=str(tmp_path / "logs"))
        relay = Relay(cfg)
        assert await asyncio.wait_for(relay.run(), 120) == 0

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            game = (await c.get(f"/api/games/{gid}")).json()
        assert game["status"] == "finished"
        ours = [m for m in game["moves"] if m["color"] == "white"]
        assert len(ours) == 8
        # every move carries claude's usage + the relay's session bookkeeping
        # (the runner sums usage over attempts: the retried first move and the re-asked one count twice)
        assert all(m["usage"]["input_tokens"] >= 500 and m["usage"]["model"] == "fake-model" for m in ours)
        assert all(m["usage"]["cost_usd"] >= 0.001 for m in ours) and ours[0]["usage"]["input_tokens"] == 1000
        assert ours[1]["usage"]["input_tokens"] == 500
        assert [m["usage"]["relay_session"] for m in ours] == ["1", "1", "1", "2", "2", "2", "3", "3"]
        assert [m["usage"]["session_move"] for m in ours] == ["1", "2", "3", "1", "2", "3", "1", "2"]
        assert all(m["comment"] for m in ours)
        # the illegal first answer was recorded and retried within the same session
        assert len(ours[0]["illegal_attempts"]) == 1 and "Qxh9" in ours[0]["illegal_attempts"][0]["move"]
        assert relay.stats["accepted"] == 8 and relay.stats["illegal"] == 1 and relay.stats["handovers"] == 2
        calls = [json.loads(l) for l in (state / "calls.jsonl").read_text().splitlines()]
        sessions = {c["session"] for c in calls}
        assert len(sessions) == 3                                   # 3 sessions for 8 moves at 3 per session
        assert sum(1 for c in calls if c["new"]) == 3
        assert any("Answer now with only that line" in c["prompt"] for c in calls)   # re-ask path
        assert any("teammate" in c["prompt"] for c in calls)                          # takeover prompt
        assert all("--tools" in c["args"] for c in calls)
        log = (tmp_path / "logs" / f"{gid}.jsonl").read_text()
        assert '"event": "handover"' in log and '"event": "game_over"' in log
    finally:
        server.should_exit = True
        await task


def test_command_uses_subscription_auth_by_default_and_isolated_cwd():
    import os
    from agentchess.relay import ClaudeRunner, RelayConfig
    runner = ClaudeRunner(RelayConfig(server="http://x", token="t"))
    cmd = runner.command(None)
    assert "--bare" not in cmd                      # --bare would force ANTHROPIC_API_KEY auth
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd
    assert os.path.isdir(runner.workdir) and os.listdir(runner.workdir) == []
    assert "--bare" in ClaudeRunner(RelayConfig(server="http://x", token="t", bare=True)).command("s1")
