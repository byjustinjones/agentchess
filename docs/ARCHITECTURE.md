# agentchess architecture & contracts

This document is the source of truth for module boundaries. Code in
`agentchess/models.py`, `agentchess/db.py`, `agentchess/events.py` and
`agentchess/players/base.py` already exists and is authoritative; read it.

Python ≥ 3.10, asyncio everywhere. Deps: `python-chess`, `fastapi`, `uvicorn`,
`httpx`, `pyyaml`, `anthropic` (official SDK for the Claude provider), optional
`mcp`. Tests: `pytest` + `pytest-asyncio` (asyncio_mode=auto). Stockfish lives at
`/usr/games/stockfish` in dev (also resolve `$STOCKFISH_PATH`, then `shutil.which("stockfish")`).

```
agentchess/
  models.py          (exists) dataclasses: PlayerSpec, GameConfig, MoveRequest, MoveResponse, MoveRecord, GameRecord, Tournament...
  db.py              (exists) Database: SQLite persistence
  events.py          (exists) EventBus pub/sub
  players/
    base.py          (exists) Player ABC, GameStart, GameEnd, PlayerContext
    engine.py        EnginePlayer (UCI via chess.engine), engine presets
    random_player.py RandomPlayer
    llm.py           LLMPlayer: providers "anthropic" (official SDK) and "openai" (OpenAI-compatible HTTP via httpx)
    remote.py        AgentHub + RemotePlayer (external agents)
    factory.py       create_player(spec, ctx) -> Player ; PRESETS
  moves.py           parse_move(board, text) -> chess.Move  (UCI or SAN, tolerant)
  game.py            play_game(...) runner
  openings.py        BUILTIN_OPENINGS: list[Opening]
  rating.py          compute_ratings(...) Bradley-Terry/Elo MLE + bootstrap CIs; crosstable
  tournament.py      schedule_games(...), TournamentManager
  server/app.py      FastAPI app factory create_app(settings) — REST + WS + agent API + static GUI
  server/agent_api.py  routes for external agents (HTTP long-poll + WebSocket)
  mcp_server.py      stdio MCP server bridging an MCP-capable agent to the agent API
  cli.py             `agentchess serve|run|ratings|export-pgn|mcp|add-player`
  web/               static GUI (index.html, app.js, style.css) — no build step
```

## Players

`Player` lifecycle (see `players/base.py`): `start_game → get_move* → end_game → close`.
The runner owns validation: players may return garbage; the runner re-asks.

### `players/factory.py`
```python
def create_player(spec: PlayerSpec, ctx: PlayerContext) -> Player
PRESETS: list[dict]   # ready-made PlayerSpec dicts for the GUI "add player" form, e.g.
# {"id": "sf-1500", "name": "Stockfish 1500", "kind": "engine", "config": {"uci_elo": 1500, "movetime_ms": 100}, "anchor_elo": 1500, "max_concurrent_games": 2}
def default_max_concurrency(kind: PlayerKind) -> int   # engine 2, llm 4, remote 1, random 8
```

### Engine config (`kind="engine"`)
```
path: str | None        # default: auto-detect stockfish
uci_elo: int | None     # sets UCI_LimitStrength=true, UCI_Elo=N (Stockfish range 1320..3190)
skill_level: int | None # Stockfish "Skill Level" 0..20 (for strengths below 1320)
movetime_ms: int = 100  # limit per move (chess.engine.Limit(time=...))
depth: int | None       # optional depth limit (combined with movetime)
nodes: int | None
threads: int = 1
hash_mb: int = 16
options: dict = {}      # any extra UCI options
```
One engine process per game (opened in `start_game`, quit in `close`).
Presets: `random` (no anchor), `sf-skill0-d1` (skill 0, depth 1), `sf-skill3`, `sf-skill6`,
`sf-1320`, `sf-1500`, `sf-1800`, `sf-2100`, `sf-2500`, `sf-max` (full strength, 1s/move)
— UCI_Elo levels get `anchor_elo = uci_elo`; others have no anchor.

### LLM config (`kind="llm"`)
```
provider: "anthropic" | "openai"      # openai = any OpenAI-compatible /chat/completions (OpenAI, OpenRouter, vLLM, Ollama...)
model: str                            # e.g. "claude-opus-5-5"
api_key_env: str                      # env var holding the key (default ANTHROPIC_API_KEY / OPENAI_API_KEY); keys are never stored in the DB
base_url: str | None                  # openai provider only (default https://api.openai.com/v1)
max_tokens: int = 16000
effort: str | None                    # anthropic output_config.effort ("low".."max")
temperature: float | None             # openai only (current Claude models reject sampling params)
system_prompt: str | None             # override default
extra_body: dict | None               # merged into the request body
input_cost_per_mtok / output_cost_per_mtok: float | None   # to compute usage.cost_usd
```
Stateless per move: every request contains the full position (FEN, ASCII board,
PGN so far, side to move, legal moves if `show_legal_moves`, and on retries the previous
error). The model must end its reply with a line `MOVE: <move>` (UCI or SAN).
Parsing: last `MOVE:` line; fallback: last token in the text that parses as a legal move.
`MoveResponse.comment` = model's reasoning text (trimmed), `usage` = tokens (+cost if prices configured).
Anthropic: `anthropic.AsyncAnthropic`, `messages.create` (stream via `messages.stream(...).get_final_message()`
when max_tokens > 16000), `output_config={"effort": ...}` when set, handle `stop_reason == "refusal"` →
treat as an unparseable answer (the runner retries/forfeits). **No automatic model fallbacks**: a benchmark
must measure exactly the configured model.

### Remote agents (`kind="remote"`)
External programs connect with a bearer token (issued at registration, only its sha256 is stored).

`players/remote.py`:
```python
class AgentHub:
    def __init__(self, bus: EventBus | None = None): ...
    # called by RemotePlayer
    async def request_move(self, player_id: str, req: MoveRequest) -> MoveResponse   # waits until the agent answers (runner applies timeout via cancellation)
    def notify_game_start(self, player_id: str, info: GameStart) -> None
    def notify_game_end(self, player_id: str, info: GameEnd) -> None
    # called by the agent API
    def touch(self, player_id: str) -> None                         # mark agent as seen (online)
    def is_online(self, player_id: str, within_s: float = 60) -> bool   # seen recently OR has an open websocket OR is currently long-polling
    def pending_requests(self, player_id: str) -> list[MoveRequest]
    async def wait_for_request(self, player_id: str, timeout_s: float) -> MoveRequest | None   # long-poll
    def submit(self, player_id: str, request_id: str, resp: MoveResponse) -> bool   # resolves the pending future; False if unknown/expired
    def find_request(self, player_id: str, game_id: str) -> MoveRequest | None
    async def next_notification(self, player_id: str, timeout_s: float) -> dict | None  # game_started / game_ended messages, for WS push
    def status(self) -> list[dict]   # [{player_id, online, last_seen, pending: n, connections: n}]
class RemotePlayer(Player): ...  # delegates to ctx.agent_hub
```
Only one pending request per (player, game) at a time. Illegal submitted moves are *accepted*
by the hub (it resolves the future); the runner then records the illegal attempt and issues
a new MoveRequest with `attempt+1`. To answer synchronously, the agent API validates the move
against the request FEN itself and tells the agent whether it was legal and how many attempts remain.

## Game runner — `game.py`
```python
async def play_game(
    game: GameRecord,          # status SCHEDULED; opening/initial_fen/config set
    white: Player, black: Player,
    db: Database | None = None,
    bus: EventBus | None = None,
    names: dict[str, str] | None = None,   # player_id -> display name
) -> GameRecord
```
- Starts from `game.initial_fen` (standard position) and first plays `game.opening.moves_uci`
  (if any). Book moves ARE stored as MoveRecords (`comment="book"`, `elapsed_s=0`, `usage={"book": true}`)
  and emitted as `move` events; players are only asked from the first non-book ply. Book moves are
  excluded from per-player move stats (db.move_stats filters on `usage.book`).
- For each ply: build `MoveRequest` (new `request_id` each attempt), call `player.get_move` under
  `asyncio.wait_for(..., config.move_timeout_s)`. Timeout → loss (`TIMEOUT`); exception → loss (`ERROR`);
  `resign` → loss (`RESIGNATION`); unparseable/illegal → record `MoveAttempt`, retry with
  `attempt+1` and `previous_error`, loss (`ILLEGAL_MOVES`) after `max_illegal_attempts` bad answers on one move.
- A player raising `players.base.InfrastructureError` (missing API key, provider outage after retries,
  rejected config, engine crash) ends the game as ABORTED with result `*` — unrated, never a forfeit.
- Game end: checkmate, stalemate, insufficient material, **automatic** claim of threefold
  repetition and fifty-move rule (`board.can_claim_draw()` style — use `is_repetition(3)` / `is_fifty_moves()`),
  and `max_plies` → draw (`MAX_PLIES`). Also fivefold/75-move via `board.outcome()`.
- Persists: `db.update_game` at start (RUNNING, started_at) and end; `db.add_move` after every ply.
  Final PGN (python-chess `chess.pgn.Game` with headers Event/Site/Date/White/Black/Result/Termination/Opening,
  move comments `{time=1.2s illegal=1}`) stored in `game.pgn`.
- Always calls `start_game`/`end_game`/`close` on both players (close in `finally`).
- Publishes events (below). If cancelled (asyncio.CancelledError) the game is left for the
  caller to mark ABORTED/rescheduled — re-raise after closing players.

## Openings — `openings.py`
`BUILTIN_OPENINGS: list[Opening]` — ~24 well-known, roughly balanced 4–8 ply lines (ids like
`"ruy-lopez"`, name, ECO, `moves_uci`), all verified legal by a test.

## Ratings — `rating.py`
```python
@dataclass
class RatingRow:
    player_id: str; name: str; kind: str
    elo: float; ci_low: float; ci_high: float   # 95% bootstrap interval
    anchored: bool                               # rating fixed by anchor_elo
    games: int; wins: int; draws: int; losses: int; score: float  # score = points/games
    performance: float | None                    # simple performance rating vs opponents' final elo
    rank: int
def compute_ratings(results: list[dict], players: list[PlayerSpec], *, use_anchors: bool = True,
                    base: float = 1500.0, prior_sd: float = 400.0, bootstrap: int = 300, seed: int = 0) -> list[RatingRow]
def crosstable(results: list[dict], player_ids: list[str]) -> dict   # {"players":[ids], "cells": {a: {b: {"w":..,"d":..,"l":..,"score":..,"games":..}}}}
def expected_score(ra: float, rb: float) -> float
```
Model: Bradley–Terry on the Elo scale (P(A beats B) = 1/(1+10^((Rb-Ra)/400))), draws count as half a
win for each side. MAP estimate with a Gaussian prior N(center, prior_sd²) per player (keeps 100%/0%
scorers finite and handles disconnected graphs). Anchored players (anchor_elo set, use_anchors) are
fixed; the prior center is the mean of anchors if any else `base`; without anchors, ratings are shifted
so their mean is `base`. Solve with Newton or MM iterations to convergence. CIs from bootstrap
resampling of games (anchored players have zero-width CI). Only players with ≥1 rated game are
returned, sorted by elo desc. Pure Python (no numpy requirement).

## Tournaments — `tournament.py`
```python
def schedule_games(t: Tournament, players: dict[str, PlayerSpec]) -> list[GameRecord]
```
- Pairs: round robin = all pairs; gauntlet = candidate × everyone else. Rounds via Berger/circle method
  so each round every player plays at most once; each pair plays `games_per_pair` games with colours
  alternating; with openings, game pairs k=2i,2i+1 share opening i (colours reversed) chosen
  deterministically from BUILTIN_OPENINGS by `seed`. Game `round` = cycle index·(rounds) + round.
  Returned in play order (all of round 1, then round 2 …).
```python
class TournamentManager:
    def __init__(self, db: Database, bus: EventBus, ctx: PlayerContext): ...
    def create(self, name: str, config: TournamentConfig) -> Tournament     # validates players, persists tournament + all scheduled games (status PENDING)
    async def start(self, tournament_id: str) -> None     # PENDING/PAUSED -> RUNNING; spawns a background runner task
    async def pause(self, tournament_id: str) -> None     # stop launching new games; running games finish
    async def cancel(self, tournament_id: str) -> None    # cancel running games (-> ABORTED), remaining scheduled -> ABORTED
    async def resume_all(self) -> None                    # on startup: db.recover_interrupted_games(); restart RUNNING tournaments
    async def shutdown(self) -> None                      # cancel runner tasks; running tournament games reset to SCHEDULED
    async def play_single(self, white_id: str, black_id: str, config: GameConfig, opening_id: str | None = None) -> GameRecord  # ad-hoc exhibition, runs in background, returns the scheduled record immediately
    def running_game_ids(self) -> list[str]
```
Scheduler loop per tournament: repeatedly pick the earliest SCHEDULED game (by seq) whose players each
have spare capacity (global per-player count across all tournaments < `max_concurrent_games`) and,
if `wait_for_remote`, whose remote players are online; keep ≤ `config.concurrency` running. Finished
when no SCHEDULED/RUNNING games remain → status FINISHED + event. Robust: one failing game never kills
the loop; a player-construction error forfeits that game (`ERROR`).

## Events (EventBus → GUI WebSocket `/ws`)
All events are dicts with `type`:
- `{"type":"game_started","game": GameRecord.to_dict(include_moves=False), "white_name":..., "black_name":...}`
- `{"type":"move","game_id":..,"ply":..,"uci":..,"san":..,"fen":..,"color":..,"elapsed_s":..,"illegal_attempts":n,"comment":..}`
- `{"type":"illegal_move","game_id":..,"player_id":..,"move":..,"error":..,"attempt":n}`
- `{"type":"thinking","game_id":..,"player_id":..,"ply":..}`  (a player was asked for a move)
- `{"type":"game_finished","game": GameRecord.to_dict(include_moves=False)}`
- `{"type":"tournament_updated","tournament": Tournament.to_dict(), "progress": {...}}`
- `{"type":"agent_status","player_id":..,"online":bool}`

## REST API (server/app.py) — JSON
All under `/api`. Errors: HTTP 4xx with `{"detail": "..."}`.

| Method & path | Body / query | Response |
|---|---|---|
| GET `/api/health` | | `{"ok":true,"version":..,"stockfish":path or null}` |
| GET `/api/players` | `?include_inactive=bool` | `[PlayerSpec.to_dict() + {"online": bool|null}]` |
| GET `/api/players/presets` | | `[preset dicts]` |
| POST `/api/players` | `{id?, name, kind, config, anchor_elo?, max_concurrent_games?}` (id slugified from name if absent) | `{player, token?}` — token only for remote players, shown once |
| GET `/api/players/{id}` | | player + `{"stats": {...}, "rating": RatingRow or null}` |
| PATCH `/api/players/{id}` | partial fields | player |
| DELETE `/api/players/{id}` | | `{"deleted": bool, "deactivated": bool}` |
| POST `/api/players/{id}/token` | | `{"token": ...}` (rotate remote token) |
| GET `/api/tournaments` | | `[Tournament.to_dict() + {"progress": {...}}]` |
| POST `/api/tournaments` | `{name, config: TournamentConfig dict, start?: bool}` | tournament + progress |
| GET `/api/tournaments/{id}` | | tournament + progress + `{"standings": [RatingRow], "crosstable": {...}, "players":[PlayerSpec]}` |
| POST `/api/tournaments/{id}/start` / `pause` / `cancel` | | tournament |
| POST `/api/tournaments/{id}/retry-aborted` | | tournament — reschedules ABORTED games (infra failures, cancel) and runs them |
| DELETE `/api/tournaments/{id}` | | `{"deleted": true}` (not while running) |
| GET `/api/games` | `?tournament_id&status&player_id&limit&offset&order=seq|recent` | `{"games":[GameRecord.to_dict(False) + white_name/black_name], "total": n}` |
| POST `/api/games` | `{white_id, black_id, config?: GameConfig, opening_id?}` | game (ad-hoc exhibition) |
| GET `/api/games/{id}` | | GameRecord.to_dict(True) + white_name/black_name |
| GET `/api/games/{id}/pgn` | | `text/plain` PGN (built on the fly if unfinished) |
| GET `/api/pgn` | `?tournament_id` | all finished games as one PGN file |
| GET `/api/ratings` | `?tournament_id&anchors=true|false` | `{"ratings":[RatingRow], "stats": {player_id: move_stats + termination_stats}, "games": n}` |
| GET `/api/live` | | `{"games":[running GameRecord.to_dict(True) + names]}` |
| GET `/api/openings` | | `[Opening]` |
| GET `/api/agents` | | `AgentHub.status()` |
| WS `/ws` | | stream of events above; on connect sends `{"type":"hello"}` |

## Agent API (server/agent_api.py) — for external agents
Auth: `Authorization: Bearer <token>` (or `?token=` for WS).

| Method & path | Notes |
|---|---|
| GET `/api/agent/me` | `{player, online}` |
| GET `/api/agent/turn?wait=30` | long-poll ≤ 60 s. 200 → `MoveRequest` JSON (oldest pending); 204 → nothing yet. Marks agent online. |
| GET `/api/agent/games` | agent's running games `[{game_id, color, opponent, fen, your_turn}]` |
| GET `/api/agent/games/{game_id}` | game state incl. pending `request` if it is the agent's turn |
| POST `/api/agent/games/{game_id}/move` | `{"move": "e2e4", "request_id"?: ..., "comment"?: ...}` → `{"accepted": bool, "legal": bool, "error": str|null, "attempts_remaining": int, "san": str|null}`; 409 if not your turn |
| POST `/api/agent/games/{game_id}/resign` | |
| WS `/api/agent/ws?token=...` | server → `{"type":"move_request","request": MoveRequest}`, `{"type":"game_started",...}`, `{"type":"game_ended",...}`, `{"type":"move_result",...}`; client → `{"type":"move","game_id":..,"move":..,"request_id"?:..,"comment"?:..}`, `{"type":"resign","game_id":..}`, `{"type":"ping"}` |

`MoveRequest` JSON = `dataclasses.asdict(MoveRequest)` (see models.py).
