#!/usr/bin/env python3
"""Bring-your-own-agent example: a Claude-powered chess agent talking to agentchess over HTTP.

This is what an external agent looks like from the server's point of view: it polls for
move requests, thinks however it likes (here: one Claude API call per move), and posts
a move back. Swap `choose_move` for your own agent loop, tools, memory, etc.

    agentchess add-player --name "Claude Opus 5.5 (external)" --kind remote   # prints a token
    export ANTHROPIC_API_KEY=...
    python examples/llm_agent.py --server http://localhost:8000 --token ac_... [--model claude-opus-5-5]

Notes
- The server validates every move. An illegal answer gets `legal: false` plus the number of
  attempts left, and the server re-sends the position with `attempt` > 1 and `previous_error` set.
- No refusal fallbacks to other models are configured on purpose: a benchmark must measure
  exactly the model it names. A refusal is simply treated as an unparseable answer.
"""
from __future__ import annotations

import argparse
import re
import time

import anthropic
import httpx

SYSTEM = (
    "You are a strong chess player. You will be shown a chess position and must choose one move. "
    "Think about threats, captures and checks for both sides before deciding. "
    "End your reply with a single final line of the form `MOVE: <move>` using UCI (e2e4, e7e8q) "
    "or SAN (Nf3, O-O)."
)
MOVE_RE = re.compile(r"MOVE:\s*`?([A-Za-z0-9+#=\-]+)`?", re.IGNORECASE)


def build_prompt(req: dict) -> str:
    parts = [
        f"You play {req['color']} against {req['opponent_name']}.",
        f"Position (FEN): {req['fen']}",
        "Board (uppercase = white, rank 8 at the top):",
        req.get("ascii_board", ""),
        f"Moves so far: {req['pgn'] or '(none)'}",
    ]
    if req["legal_moves_san"]:
        parts.append("Legal moves: " + " ".join(req["legal_moves_san"]))
    if req.get("previous_error"):
        parts.append(f"Your previous answer was rejected ({req['previous_error']}). "
                     f"This is attempt {req['attempt']}; pick a legal move.")
    parts.append(f"You have {req['time_limit_s']:.0f} seconds. What is your move?")
    return "\n\n".join(parts)


def choose_move(client: anthropic.Anthropic, model: str, effort: str, req: dict) -> tuple[str, str]:
    """Ask Claude for a move. Returns (move_text, reasoning)."""
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        output_config={"effort": effort},
        system=SYSTEM,
        messages=[{"role": "user", "content": build_prompt(req)}],
    )
    if response.stop_reason == "refusal":
        return "", "(refused)"
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    matches = MOVE_RE.findall(text)
    return (matches[-1] if matches else ""), text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--token", required=True)
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    args = ap.parse_args()

    claude = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY (or an `ant auth login` profile)
    http = httpx.Client(base_url=args.server, headers={"Authorization": f"Bearer {args.token}"}, timeout=60)
    print("waiting for games... (start a tournament or exhibition game in the GUI)")
    while True:
        try:
            r = http.get("/api/agent/turn", params={"wait": 30})
        except httpx.TransportError as e:
            print(f"server unreachable ({e}); retrying")
            time.sleep(3)
            continue
        if r.status_code == 204:
            continue
        r.raise_for_status()
        req = r.json()
        try:
            move, reasoning = choose_move(claude, args.model, args.effort, req)
        except anthropic.APIStatusError as e:
            print(f"Claude API error {e.status_code}: {e.message}")
            move, reasoning = "", f"API error {e.status_code}"
        except anthropic.APIConnectionError:
            print("could not reach the Claude API")
            move, reasoning = "", "API connection error"
        res = http.post(f"/api/agent/games/{req['game_id']}/move",
                        json={"move": move, "request_id": req["request_id"], "comment": reasoning[-4000:]})
        if res.status_code == 409:  # the request expired (timeout) or the game ended
            print(f"{req['game_id']}: {res.json().get('detail')}")
            continue
        res.raise_for_status()
        out = res.json()
        if out["legal"]:
            print(f"{req['game_id']} ply {req['ply']}: {out['san']}")
        else:
            print(f"{req['game_id']} ply {req['ply']}: illegal {move!r} ({out['error']}); "
                  f"{out['attempts_remaining']} attempts left")


if __name__ == "__main__":
    main()
