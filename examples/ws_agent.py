#!/usr/bin/env python3
"""WebSocket protocol demo: move requests are pushed; the agent answers with random legal moves.

    python examples/ws_agent.py --server ws://localhost:8000 --token ac_...

Server -> client: hello, move_request {request}, game_started, game_ended, move_result, pong, error
Client -> server: {"type":"move","game_id","move","request_id"?,"comment"?}, {"type":"resign","game_id"},
                  {"type":"ping"}
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random

import chess
import websockets


async def run(server: str, token: str) -> None:
    url = f"{server.rstrip('/')}/api/agent/ws?token={token}"
    async for ws in websockets.connect(url, ping_interval=20):  # reconnects automatically
        try:
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "hello":
                    print(f"connected as {msg['player']['name']}")
                elif kind == "move_request":
                    req = msg["request"]
                    moves = req["legal_moves_uci"] or [m.uci() for m in chess.Board(req["fen"]).legal_moves]
                    await ws.send(json.dumps({"type": "move", "game_id": req["game_id"],
                                              "request_id": req["request_id"], "move": random.choice(moves)}))
                elif kind == "move_result":
                    print(f"{msg['game_id']}: {msg.get('san') or msg.get('error')}")
                elif kind in ("game_started", "game_ended"):
                    print(kind, json.dumps({k: v for k, v in msg.items() if k != "type"}))
                elif kind == "error":
                    print("error:", msg.get("detail"))
        except websockets.ConnectionClosed:
            print("connection closed; reconnecting")
            continue


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="ws://127.0.0.1:8000")
    ap.add_argument("--token", required=True)
    args = ap.parse_args()
    asyncio.run(run(args.server, args.token))


if __name__ == "__main__":
    main()
