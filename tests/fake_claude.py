#!/usr/bin/env python3
"""A stand-in for ``claude -p --output-format json`` used by the relay tests.

It reads the prompt from stdin, answers with the first (or, when told to be
naughty, an illegal) move from the "Legal moves" line and prints the JSON
envelope Claude Code prints. Sessions: ``--resume ID`` keeps the id, otherwise
a fresh one is minted. Env: ``FAKE_CLAUDE_ILLEGAL_FIRST=1`` answers the very
first request of each process family with an illegal move once;
``FAKE_CLAUDE_NO_MOVE_LINE=1`` omits the MOVE: line on the first call of a
session (the relay must re-ask); ``FAKE_CLAUDE_STATE`` is a directory for
cross-process state.
"""
import json
import os
import re
import sys
import uuid
from pathlib import Path

args = sys.argv[1:]
prompt = sys.stdin.read()
session = None
if "--resume" in args:
    session = args[args.index("--resume") + 1]
new_session = session is None
session = session or f"sess_{uuid.uuid4().hex[:8]}"
state = Path(os.environ.get("FAKE_CLAUDE_STATE", "."))
state.mkdir(parents=True, exist_ok=True)
(state / "calls.jsonl").open("a").write(json.dumps({"args": args, "session": session, "new": new_session,
                                                     "prompt": prompt}) + "\n")

m = re.search(r"Legal moves \(\d+\): (.*)", prompt)
legal = m.group(1).split() if m else []
if not legal:
    text = "I cannot see any legal moves.\nMOVE: e4"
else:
    flag = state / "illegal_done"
    if os.environ.get("FAKE_CLAUDE_ILLEGAL_FIRST") and not flag.exists():
        flag.write_text("1")
        text = "Let me try something bold.\nMOVE: Qxh9"
    elif os.environ.get("FAKE_CLAUDE_NO_MOVE_LINE") and new_session:
        text = "I think the best plan is development."
    elif "Answer now with only that line" in prompt:
        text = f"MOVE: {legal[0]}"
    else:
        text = f"The position looks fine. I'll play the first legal move.\nMOVE: {legal[0]}"

print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session,
    "total_cost_usd": 0.001, "duration_ms": 12, "num_turns": 1,
    "usage": {"input_tokens": 500, "output_tokens": 40, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
}))
