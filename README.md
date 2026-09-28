# agentchess

A chess benchmark for LLM agents. Agents play each other and a ladder of
calibrated chess engines in resumable round-robin tournaments. Ratings are fit on
the Elo scale with confidence intervals. A web GUI shows live games, standings,
crosstables and every move an agent made, including its reasoning, the illegal
moves it tried, the time it took and the tokens it used.

- **Players.** Built-in LLM players (Claude through the official Anthropic SDK, or any
  OpenAI-compatible endpoint such as OpenAI, OpenRouter, vLLM or Ollama), Stockfish at fixed
  strengths, a random mover, and **external agents** that connect over HTTP long-polling,
  WebSocket or MCP.
- **Tournaments.** Round robin (Berger tables) or gauntlet format. Colours are balanced,
  and each opening from a built-in suite is played once with each colour. Per-tournament and
  per-player concurrency limits apply. Games involving an offline agent wait instead of
  being forfeited. Everything is stored in SQLite, so a crash or restart resumes where it stopped.
- **Ratings.** A Bradley–Terry maximum-a-posteriori fit on the Elo scale, anchored to Stockfish
  `UCI_Elo` levels, with 95% bootstrap confidence intervals, W/D/L, performance, and
  illegal-move and forfeit rates.
- **Rules for LLMs.** A configurable per-move timeout and illegal-move budget (a player that exceeds either forfeits),
  automatic threefold and fifty-move draws, and a ply cap. Legal-move hints can be turned on or off.
  Infrastructure failures (a missing API key, a provider outage, an engine crash) abort the game unrated
  instead of scoring it as a loss. **Retry aborted** replays those games later.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp,dev]"
# Stockfish: apt install stockfish | brew install stockfish  (or set STOCKFISH_PATH)

agentchess serve            # http://127.0.0.1:8000
```

On first start the server registers a random mover and a Stockfish ladder. In the GUI:

1. **Players → Add player** registers an LLM (for example provider `anthropic`, model
   `claude-opus-5-5`, key read from `ANTHROPIC_API_KEY`) or an external agent. An external
   agent is given a token, which is shown once.
2. **Tournaments → New tournament** lets you pick participants and a format, then starts the tournament.
3. Follow games under **Live**. **Leaderboard** shows ratings with confidence intervals.

Headless runs (CI, batch benchmarking):

```bash
agentchess run examples/tournament.yaml            # engines + random, no API keys needed
agentchess run examples/llm_benchmark.yaml --serve # LLMs vs the engine ladder, with GUI
agentchess ratings
agentchess export-pgn > games.pgn
```

## Connecting your own agent

Register a player of kind `remote` (in the GUI, or with `agentchess add-player --name my-bot --kind remote`)
and keep the token. Then pick one of three ways to connect.

**HTTP long-poll.** This works from any language, or from an agent that only has `curl`:

```bash
# wait up to 30 s for a move request (204 = nothing yet)
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8000/api/agent/turn?wait=30"
# answer (UCI "e2e4" or SAN "Nf3")
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"move":"e4","comment":"king pawn"}' http://localhost:8000/api/agent/games/$GAME_ID/move
```

If a move is illegal, the reply says so and tells you how many attempts remain. The next
`turn` then carries `attempt` and `previous_error`.

**WebSocket.** Connect to `ws://localhost:8000/api/agent/ws?token=...`. The server pushes
`move_request` messages and the agent sends `{"type":"move","game_id":...,"move":...}`.
See `examples/ws_agent.py`.

**MCP.** This works with agents such as Claude Code. The bridge exposes the tools `wait_for_turn`, `make_move`, `get_game`, `list_games`
and `resign`:

```bash
claude mcp add agentchess -- agentchess mcp --server http://localhost:8000 --token $TOKEN
```

Reference clients are in `examples/`: `random_agent.py`, `llm_agent.py` and `ws_agent.py`.

## How ratings work

Each finished game is a Bradley–Terry observation on the Elo scale:
`P(A beats B) = 1 / (1 + 10^((R_B − R_A)/400))`, where a draw counts as half a win for each side.
Ratings are the maximum-a-posteriori estimate under a weak Gaussian prior, which keeps a 100% or 0%
scorer finite and handles players who never met. Engines configured with `UCI_Elo` are **anchors**:
their ratings are fixed, which ties the scale to Stockfish's calibration (roughly CCRL
blitz). Confidence intervals come from bootstrap resampling of games. Unlike incremental Elo, the fit does not depend on game order.

Tips for a meaningful benchmark:

- Include anchors on both sides of the agents' expected strength. Most LLMs belong somewhere between
  `random`, `sf-skill0-d1` and `sf-1500`.
- Stockfish's `UCI_Elo` is calibrated at longer time controls than the default 100 ms per move,
  so treat the absolute numbers as approximate. Comparisons *between agents* in the same
  tournament are robust.
- Use `games_per_pair` of 4 or more with the opening suite. Deterministic players otherwise repeat identical games.
- `show_legal_moves: false` makes the benchmark measure board understanding and not only move selection.

## Project layout

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for module contracts, the REST API and the event
catalogue.

```
agentchess/
  models.py db.py events.py          core types, SQLite storage, pub/sub
  players/ engine llm remote random  participants
  game.py moves.py openings.py       game runner, move parsing, opening suite
  rating.py tournament.py            Elo fitting, scheduling, tournament manager
  server/ app.py agent_api.py        FastAPI REST + WebSocket + agent API
  mcp_server.py cli.py               MCP bridge, command line
  web/                               GUI (static, no build step)
```

Run the tests with `pytest -q`.
