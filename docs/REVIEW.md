# agentchess review (design + implementation)

Reviewer: senior engineer / benchmark designer. Scope: the benchmark design (validity,
statistics, fairness, reproducibility, cost) and the implementation, using the live
round robin (`t_2f81da09cfbb`: Opus, Sonnet, Fable, Haiku as remote Claude Code relays vs
random, SF skill0/d1, SF 1320, SF 1500; 2 games per pair, builtin openings, 15-minute moves,
legal moves shown) as evidence. All numbers below come from a read-only copy of that DB
(54 finished games at the time of the copy), analysed offline with Stockfish 16 at depth 10,
one thread, `nice -n 19`.

## 1. Findings, ranked

### F1 (high) — Results alone cannot rank the models: the design is under-powered
* 14 games per model give 95% CIs of ±170–235 Elo. The fit's own estimate of what it would
  take to get to ±50: about **290 more games for Opus, 245 for Sonnet, 115 for Fable, 85 for
  Haiku** (`games_for_ci50`, new).
* Bootstrap superiority (new): P(Opus > Sonnet) = 0.95, P(Sonnet > Fable) = 0.99,
  P(Fable > Haiku) = 0.98, but P(Opus > SF 1500) = 0.89 and P(Fable > SF skill0) = 0.23. So
  the *ordering* of the four models is fairly solid; their absolute Elo and their placement
  relative to the engine ladder is not.
* With 2 games per pair **no head-to-head can reach p ≤ 0.05** (best case 2–0 gives p = 0.5
  under the sign test). The crosstable is a picture, not evidence.
* Two games per pair also means each opening is played once per colour and never repeated, so
  opening luck is not averaged.

### F2 (high) — Move quality is a far more sensitive signal than game results, and it was
### only available through an ad-hoc script
Built-in engine analysis of the same 54 games (new `analysis.py`, depth 10):

| player | ACPL | blunders/100 | mistakes/100 | engine move % | missed wins | judged moves |
|---|---|---|---|---|---|---|
| Claude Opus | 51 | 3.8 | 6.6 | 43 | 2 | 290 |
| Stockfish 1500 | 49 | 1.3 | 14.1 | 32 | 0 | 313 |
| Claude Sonnet | 61 | 5.7 | 7.9 | 34 | 1 | 316 |
| Stockfish 1320 | 69 | 5.2 | 14.4 | 31 | 0 | 270 |
| SF skill 0 d1 | 74 | 4.9 | 15.3 | 29 | 2 | 365 |
| Claude Fable | 80 | 7.7 | 11.7 | 35 | 3 | 274 |
| Claude Haiku | 156 | 19.2 | 21.8 | 21 | 2 | 156 |
| Random | 244 | 30.9 | 27.7 | 12 | 1 | 94 |

This reproduces the ad-hoc `quality.py` numbers (Opus 49/3.5, Haiku 141/15.9) and adds two
diagnostics the live run needed: **engine-move agreement** (Opus plays Stockfish's first choice
more often than Stockfish 1500 does, i.e. its knowledge is good and its losses are tactical
blunders) and **missed wins** (Fable 3, Opus 2, Haiku 2: the stalemates and
insufficient-material draws you observed are now counted per player). The blunder rate has
~300 observations per model instead of 14, which is why it separates Opus/Sonnet/Fable/Haiku
cleanly where ratings overlap.

### F3 (high) — Fair play was enforced by instructions and an after-the-fact audit
The relay agents ran on a machine with Stockfish installed, with a general-purpose Bash tool,
and the audit script needed several patches for false positives. A benchmark that depends on
the subject's honesty is not reproducible by third parties. The fix is structural: give the
model no tools at all and let the harness talk to the server (`agentchess relay`, new). That
also removes three other observed problems: agents miscounting the 40-move relay limit
(the harness counts), agents chaining `turn && move` (impossible: the harness moves), and
missing token/cost data (claude's own usage report is attached to every move).

### F4 (medium) — Anchoring is approximate and one-sided
Stockfish `UCI_Elo` is calibrated at much longer time controls than 100 ms/move; at 100 ms and
depth-limited play the "1500" engine is probably weaker than 1500 (it lost to Opus 1–1 and to
Sonnet 1–1 while Opus is rated above it). The models sit between skill-0 and 1500, but there
is no anchor above 1500 and the only anchors are two points 180 Elo apart, so the scale's
*slope* is set by two engines that differ in a single UCI parameter. Not fixed in code (it is
a configuration matter); see experiments E2.

### F5 (medium) — Draws in won positions are a real skill deficit, not noise
Fable/Haiku drew totally won games (insufficient material after promoting badly, stalemates),
Opus stalemated Q+R vs K. This is the single most model-specific weakness the run exposed and
it costs half a point each time; `missed_win` now quantifies it. Do not "fix" it with
adjudication: converting is part of playing chess. Do consider a separate endgame/conversion
test set (see E3).

### F6 (medium) — Implementation issues (all fixed)
1. **Illegal attempts and usage of the forfeiting move were lost.** A 3-illegal-move forfeit
   left no trace in `moves` and no tokens in the stats; the GUI even wiped the pending illegal
   list on `game_finished`. Now stored as `games.final_attempt_json`, counted by `move_stats`,
   rendered in the game view.
2. **Crashed agents were handed games.** `is_online` had a 60 s grace; the scheduler now uses
   `is_ready` (connected / polling / seen ≤ 10 s). A crashed agent otherwise forfeits a whole
   game on the first 15-minute timeout.
3. **Tokens in `?token=` query parameters** were accepted on HTTP routes (access logs). Header only
   now; the WebSocket keeps the query form.
4. **Wrong-typed API bodies → 500.** `TournamentConfig`/`GameConfig` are now typed pydantic models
   (422), and odd `games_per_pair` with builtin openings is rejected with an explanation
   (`ValueError` in the scheduler and 400 in the API; the GUI already refused it).
5. **Oversized comments** were broadcast in full over the GUI WebSocket and remote agents could
   send unlimited ones: truncated to 8000 chars at the runner and at the agent API.
6. **Remote agents had no usage column** — added `usage` to the move body (HTTP + WS), sanitised.
7. GUI: page overflow at phone width on the leaderboard (pre-existing) fixed.

### F7 (low) — Smaller observations
* `RatingRow.performance` is a clamped ±800 formula; it is close to meaningless for 100% scorers
  (Random shows "perf 718"). Left as is but the leaderboard now favours the bootstrap quantities.
* Illegal-move rates are low for all models (0.5–1.6%) *with legal moves shown*; the
  `show_legal_moves: false` variant would measure board understanding but was not run.
* Move times: Sonnet's median 23 s / p90 58 s, Fable's max 427 s — relay chains spend a lot of
  wall-clock time waiting; the 15-minute limit was never hit but nearly (sequential 2-game play).
* `sequential_elo` is exported but unused by the GUI; the ratings-history chart it was written
  for does not exist.
* The tournament `seed` selects openings deterministically but the opening list is only 24
  lines; with 8 players and 2 games per pair, 28 pairs already wrap around the list.

## 2. What was implemented (and why)

| area | files | what |
|---|---|---|
| Post-game analysis | `agentchess/analysis.py`, `db.py` (3 tables, `save/get/delete_analysis`, `analysis_stats`, `unanalysed_game_ids`), `server/app.py` (`AnalysisService` in `Runtime`, `GET/POST /api/games/{id}/analysis`, `POST /api/tournaments/{id}/analyse`, `GET /api/analysis/status`, stats merged into `/api/ratings` and `/api/players/{id}`), `cli.py` (`agentchess analyse`, `--analysis-depth/--no-analysis`, quality columns in `ratings`/`run`), GUI (`components.js` ACPL + blunder columns, `game.js` per-move ??/?/?! badges, eval strip, engine eval per move, "Analyse" button, `game_analysed` event) | ACPL, blunder/mistake/inaccuracy rates, engine-move agreement, missed wins per player and game. Automatic for every finished game (one `nice -n 19` single-thread engine), never for running games, not reachable through the agent API. Why: F2 — the sensitive metric the benchmark needs, previously an unversioned script. |
| Statistics | `rating.py` (`rating_report`, `RatingReport.superiority`, `RatingRow.opponents/linked_to_anchor/games_for_ci50/p_above_next`, crosstable `p`), API/GUI/CLI | Honest uncertainty: P(A>B) from the bootstrap, "games needed for ±50", a ⚠ for players not linked to an anchor, sign-test p-values in the crosstable. Why: F1. |
| Fair-play relay harness | `agentchess/relay.py`, `cli.py` (`agentchess relay`), `tests/fake_claude.py` | Reusable, testable replacement for `turn.sh`/`move.sh`/PROMPT.md: `claude -p` with all tools disabled, one session per game, handover after N moves counted by the harness, usage/cost attached to moves, JSONL transcripts. The flags were verified against Claude Code 2.1.284 (`--tools "" --strict-mcp-config --permission-prompts none --output-format json --resume`). Integration note: `--bare` was dropped from the defaults because it forces `ANTHROPIC_API_KEY` auth and ignores a Claude subscription login; calls run in a private empty working directory instead, and `--bare` is opt-in. Why: F3. |
| Usage reporting for remote agents | `server/agent_api.py` (`usage` in `MoveBody` and WS `move`, `clean_usage`, `clean_comment`), docs, Connect page | Token/cost columns for external agents. |
| Open issues | `game.py` (`final_attempt`, comment cap), `models.py`, `db.py` (migration `games.final_attempt_json`, `move_stats` counts final attempts), `players/remote.py` (`is_ready`), `tournament.py` (`is_ready`, even `games_per_pair`), `server/agent_api.py` (header-only tokens), `server/app.py` (typed config models), GUI (`final_attempt` block) | F6. Schema change is a guarded `ALTER TABLE ADD COLUMN`; the live DB opens unchanged until a new process migrates it (verified on a copy). |
| Docs | `README.md`, `docs/ARCHITECTURE.md`, this file | |
| Tests | `tests/test_analysis.py`, `tests/test_relay.py` (fake `claude`, real server on a 84xx port), additions to `test_game.py`, `test_remote.py`, `test_rating.py`, `test_api.py`, `test_agent_api.py` | 151 → 170 tests, all green. |

Backward compatibility: no existing API field was removed or renamed; new fields are additive.
The only behavioural changes are the header-only token rule for HTTP agent routes (a `?token=`
user gets 401), the even-`games_per_pair` rule with builtin openings, and the stricter
readiness check before *starting* a remote agent's game (an agent must have talked to the server
within 10 s or be polling/connected; long-poll agents always satisfy this).

## 3. Recommended next, not done

1. **Tactics / conversion test mode** (same positions for every model). A `puzzles` table
   (FEN, best moves, theme, source) plus a runner that asks each player for one move per position
   and scores exact/engine-verified answers, with per-theme accuracy and a CI from the number of
   positions. Cheaper than games (one move per call), no opponent noise, directly measures the
   deficits seen here: hanging pieces, missed recaptures, mate-in-N, K+Q vs K technique,
   pawn-promotion endings. Use Lichess puzzle CSV (themes, ratings) so difficulty is calibrated.
   The `Player` interface needs no change; the runner is ~150 lines plus API/CLI/GUI.
2. **Adjudication-free but *counted* conversion metric** beyond `missed_win`: per-game "eval at
   move 40" and "result vs eval", and a per-player "conversion rate from ≥ +5".
3. **Anchor calibration**: run the engine ladder at 1 s/move for anchors (movetime affects
   UCI_Elo strength a lot) or, better, calibrate the ladder once against itself with many fast
   games and store an `effective_elo` per preset. Add a 1800 anchor above the models.
4. **Rating history / sequential Elo chart** (function exists, no consumer) — or delete it.
5. **`show_legal_moves: false` variant** as a second tournament of the same players, reported
   side by side, to measure board understanding.
6. The analysis worker analyses at most one game at a time; on an idle 4-core box a
   `--analysis-threads` option would speed up `agentchess analyse` on large DBs.
7. Relay: an `--agent-mode` that gives the model the `turn/move` scripts back as tools (using
   Claude Code's allow-list) for people who explicitly want to benchmark *agentic* play; keep the
   no-tools mode as the default because it is the only one that needs no audit.

## 4. Suggested next experiments

* **E1 — power.** Rerun the same 8 players with `games_per_pair: 8` (4 openings × 2 colours,
  224 games). Expected CI ±90–110 for the models; enough to place Opus vs SF 1500 with
  P > 0.95 if the 12–1–1 form holds. Cost: about 4× the current run.
* **E2 — anchors.** Add `sf-1800` and run the ladder engines at 1000 ms/move for one calibration
  tournament (engines only, `games_per_pair: 20`), then compare fitted vs configured Elo of the
  1320/1500 levels to estimate the 100 ms bias.
* **E3 — conversion set.** 100 positions (K+Q vs K, K+R vs K, K+P vs K, Q vs R, up a piece in a
  simple middlegame) played to the end against `random` and against SF skill 0: win rate and
  plies-to-mate per model. This isolates the stalemate/insufficient-material failure mode.
* **E4 — relay ablation.** Same model, three harness settings: `--moves-per-session 40`
  (current), `0` (never hand over) and `1` (stateless, equivalent to the built-in LLM player).
  Measures how much within-game context helps a model; the built-in `llm` player vs the relay
  with `--moves-per-session 1` also isolates the effect of Claude Code's system prompt.
* **E5 — no legal-move list.** Repeat E1 with `show_legal_moves: false` and compare illegal-move
  rates and ACPL.

## 5. Verification done for this review

* Full suite: `python -m pytest -q` → 170 passed (Stockfish-dependent tests included).
* GUI: Playwright (chromium 1194) against a server on port 8400 running a copy of the live DB
  with all 54 games analysed: leaderboard, tournament, an analysed game, an illegal-move-forfeit
  game, Connect and a player page at 1280 px dark and 390 px light; no console errors, no
  horizontal overflow at 390 px.
* `claude -p` flag compatibility checked with one real invocation (the sandbox has no Claude
  credentials, so it returned an auth error in the expected JSON envelope, which the harness
  reports as an error and retries on the next request).
* The live server, DB, `tour/` directory and agent tokens were not touched; everything ran on
  copies under `scratchpad/fable-review/`.
