#!/usr/bin/env python3
"""Minimal external agent: HTTP long-poll, plays uniformly random legal moves.

    agentchess add-player --name "My random agent" --kind remote     # prints a token
    python examples/random_agent.py --server http://localhost:8000 --token ac_...

Protocol:
    GET  /api/agent/turn?wait=30            -> 200 MoveRequest JSON | 204 nothing yet
    POST /api/agent/games/{game_id}/move    {"move": "e2e4", "request_id": ...}
"""
from __future__ import annotations

import argparse
import random
import time

import chess
import httpx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--token", required=True)
    ap.add_argument("--wait", type=float, default=30.0, help="long-poll seconds (max 60)")
    args = ap.parse_args()

    client = httpx.Client(base_url=args.server, headers={"Authorization": f"Bearer {args.token}"},
                          timeout=args.wait + 15)
    me = client.get("/api/agent/me").raise_for_status().json()
    print(f"connected as {me['player']['name']} ({me['player']['id']})")
    while True:
        try:
            r = client.get("/api/agent/turn", params={"wait": args.wait})
        except httpx.TransportError as e:
            print(f"server unreachable ({e}); retrying in 3 s")
            time.sleep(3)
            continue
        if r.status_code == 204:
            continue
        r.raise_for_status()
        req = r.json()
        # legal_moves_uci is empty when the server hides legal moves; compute them locally then.
        moves = req["legal_moves_uci"] or [m.uci() for m in chess.Board(req["fen"]).legal_moves]
        move = random.choice(moves)
        res = client.post(f"/api/agent/games/{req['game_id']}/move",
                          json={"move": move, "request_id": req["request_id"], "comment": "random"})
        if res.status_code == 409:  # request expired (e.g. timeout) - just poll again
            continue
        res.raise_for_status()
        print(f"{req['game_id']} ply {req['ply']}: {res.json().get('san') or move}")


if __name__ == "__main__":
    main()
