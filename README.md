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
  illegal-move and forfeit rates. The same bootstrap gives pairwise "P(A is stronger than B)",
  the leaderboard says how many more games a ±50 Elo interval would take, and crosstable cells
  carry a sign-test p-value.
- **Move quality.** Every finished game is analysed with Stockfish afterwards (low priority, one
  thread): average centipawn loss, blunder/mistake/inaccuracy rates, engine-move agreement and
  missed wins per player, shown on the leaderboard, in every game view and by `agentchess analyse`.
  This is far more sensitive than results alone: a 56-game round robin separates models by ACPL
  that ratings cannot separate.
- **Claude Code relay.** `agentchess relay` plays a remote player with `claude -p`: all tools
  disabled (fair play by construction), one session per game with a fresh session every N moves,
  token usage and cost attached to each move.
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

API keys: copy `.env.example` to `.env` (git-ignored), fill in the keys you need and run
`chmod 600 .env`. agentchess loads it automatically, and configs refer to keys only by variable
name (`api_key_env`), so keys never appear in YAML or the database.

No local machine (tablet, Chromebook)? Open the repo in **GitHub Codespaces**
(Code → Codespaces → Create codespace on this branch). `.devcontainer/` installs Python,
Stockfish and agentchess; run `agentchess serve --host 0.0.0.0` and the GUI opens in the
browser through the forwarded port. Add API keys as Codespaces secrets, not in files.

Headless runs (CI, batch benchmarking):

```bash
agentchess run examples/tournament.yaml            # engines + random, no API keys needed
agentchess run examples/llm_benchmark.yaml --serve # LLMs vs the engine ladder, with GUI
agentchess ratings                                 # table incl. P(>next), games needed, ACPL, blunders
agentchess analyse --tournament t_...              # (re)run engine analysis, print move-quality table
agentchess export-pgn > games.pgn
```

Post-game analysis runs automatically in `serve` and `run` (`--analysis-depth 12`, `--no-analysis`
to turn it off). It only ever looks at finished games and is not reachable through the agent API.

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
`turn` then carries `attempt` and `previous_error`. The move body may include
`"usage": {"input_tokens": .., "output_tokens": .., "cost_usd": .., "model": ".."}` so the
leaderboard's token and cost columns work for external agents too. Tokens go in the
`Authorization` header only (a `?token=` query parameter is accepted just for the WebSocket).

**WebSocket.** Connect to `ws://localhost:8000/api/agent/ws?token=...`. The server pushes
`move_request` messages and the agent sends `{"type":"move","game_id":...,"move":...}`.
See `examples/ws_agent.py`.

**MCP.** This works with agents such as Claude Code. The bridge exposes the tools `wait_for_turn`, `make_move`, `get_game`, `list_games`
and `resign`:

```bash
claude mcp add agentchess -- agentchess mcp --server http://localhost:8000 --token $TOKEN
```

Reference clients are in `examples/`: `random_agent.py`, `llm_agent.py` and `ws_agent.py`.

**Claude Code relay (reproducible agent benchmark).** To measure Claude Code itself rather than a
raw API model, run the bundled harness for a remote player:

```bash
agentchess relay --server http://localhost:8000 --token $TOKEN --model opus --effort high \
    --moves-per-session 40 --log-dir relay-logs/
```

It long-polls for your turn, hands each position to `claude -p` with **all tools disabled**
(`--tools "" --strict-mcp-config`, in an empty working directory: no engine, no files, no code, nothing to audit; works with a Claude subscription login, and `--bare` switches to API-key auth),
keeps one Claude Code session per game so the model retains its own reasoning, and starts a fresh
session after `--moves-per-session` accepted moves (the harness counts, not the model). Illegal
answers go back to the same session with the server's error, and every move carries the tokens
and cost `claude` reported. One process plays every concurrent game of the player; run one
process per model.

## How ratings work

Each finished game is a Bradley–Terry observation on the Elo scale:
`P(A beats B) = 1 / (1 + 10^((R_B − R_A)/400))`, where a draw counts as half a win for each side.
Ratings are the maximum-a-posteriori estimate under a weak Gaussian prior, which keeps a 100% or 0%
scorer finite and handles players who never met. Engines configured with `UCI_Elo` are **anchors**:
their ratings are fixed, which ties the scale to Stockfish's calibration (roughly CCRL
blitz). Confidence intervals come from bootstrap resampling of games. Unlike incremental Elo, the fit does not depend on game order.

The bootstrap replicates also give, for every pair, the probability that one player's rating is
really above the other's (`superiority` in the API, "P(>next)" on the leaderboard) and an
estimate of how many more games would shrink a player's interval to ±50 Elo (hover the CI).
A ⚠ marks a player with no chain of games to an anchored engine: its rating is only relative to
the players it met. Crosstable cells show a two-sided sign-test p-value of wins vs losses.
With 2 games per pair nothing head-to-head can reach p ≤ 0.05; use ACPL/blunder rates
(engine analysis) for a sensitive comparison and more games for rating claims.

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
  rating.py tournament.py            Elo fitting (+ superiority, games needed), scheduling, manager
  analysis.py                        post-game engine analysis (ACPL, blunders, missed wins)
  relay.py                           Claude Code relay harness (agentchess relay)
  server/ app.py agent_api.py        FastAPI REST + WebSocket + agent API
  mcp_server.py cli.py               MCP bridge, command line
  web/                               GUI (static, no build step)
```

See [docs/REVIEW.md](docs/REVIEW.md) for the benchmark review (findings, what the live tournament
data showed, and recommended next experiments).

Run the tests with `pytest -q`.
