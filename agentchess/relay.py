"""Relay harness: play a ``remote`` player with Claude Code (``claude -p``).

This reproduces the "Claude agent" setup without ad-hoc scripts and with the
fair-play controls built in, instead of promised by the model and audited later:

* The harness talks to the agentchess server (long-poll ``/api/agent/turn``,
  ``POST .../move``); the model only ever sees a position and answers a move.
* Every model call runs ``claude -p`` with **all tools disabled** (``--tools ""``),
  and no MCP servers, so it cannot run an engine, read files or execute code.
  Each call runs in a private empty working directory, so no project CLAUDE.md or
  project settings are picked up. Nothing to audit afterwards.
* Auth: the normal Claude Code login (a Claude subscription works). ``bare=True``
  (``agentchess relay --bare``) additionally skips hooks, plugins and user memory,
  but Claude Code then authenticates with ``ANTHROPIC_API_KEY`` only.
* Each game gets its own Claude Code session (``--resume``), so the model keeps
  its own earlier reasoning for that game. After ``moves_per_session`` accepted
  moves the session is dropped and a fresh one takes over from the full move
  list (the "relay"). The harness counts moves, not the model.
* Token usage and cost reported by ``claude`` are attached to every move
  (``usage``), so remote agents get token/cost columns like built-in LLM players.
* Moves are extracted from a final ``MOVE:`` line (fallback: last legal move
  mentioned). Illegal answers go back to the same session with the server's
  error, exactly like the built-in LLM player.

Run it with ``agentchess relay --server http://localhost:8000 --token ac_... --model opus``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import chess
import httpx

from agentchess.moves import extract_move_text, find_move_in_text

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are playing rated chess games on a benchmark server. You have no tools: choose every move by \
your own reasoning. Each message shows the complete current position of one game (FEN, board diagram, \
the moves so far and the legal moves). Think concretely before answering: what did the opponent's last \
move threaten, which checks, captures and threats exist for both sides, is your king safe, does your \
move hang anything. In winning positions, convert: keep pieces protected, avoid stalemate, push passed \
pawns, promote and mate. Play to win; do not resign.

Answer format: reason briefly, then end your reply with one final line of exactly this form and \
nothing after it:
MOVE: <move>
where <move> is one legal move in SAN exactly as listed (e.g. Nf3, exd5, O-O, e8=Q) or in UCI (e2e4, e7e8q).\
"""

# --bare is deliberately not in the default set: it makes Claude Code use ANTHROPIC_API_KEY
# only and never the subscription (OAuth) login. Opt in with RelayConfig.bare / --bare.
FAIR_PLAY_ARGS = ["--tools", "", "--strict-mcp-config", "--permission-prompts", "none"]


@dataclass
class RelayConfig:
    server: str
    token: str
    claude_cmd: str = "claude"
    model: Optional[str] = None
    effort: Optional[str] = None
    moves_per_session: int = 40          # 0 = never hand over
    max_think_s: float = 0.0             # 0 = the game's move time limit (minus a margin)
    log_dir: Optional[str] = None
    max_games: int = 0                   # stop after this many finished games (0 = forever)
    idle_exit_s: float = 0.0             # stop after this long without a request (0 = never)
    extra_args: list[str] = field(default_factory=list)
    poll_wait_s: float = 50.0
    system_prompt: str = SYSTEM_PROMPT
    fair_play_args: list[str] = field(default_factory=lambda: list(FAIR_PLAY_ARGS))
    reask_on_missing_move: bool = True   # ask the same session once more when no MOVE: line was given
    bare: bool = False                   # add --bare (API-key auth only; skips hooks/plugins/memory)


@dataclass
class ClaudeResult:
    text: str
    session_id: Optional[str]
    usage: dict[str, Any]
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    raw: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------- prompts
def format_position(req: dict[str, Any], *, new_session: bool, takeover: bool) -> str:
    """The user message for one move request."""
    lines: list[str] = []
    if new_session:
        lines.append(f"Game {req['game_id']}: you play {req['color'].upper()} against "
                     f"{req.get('opponent_name') or 'the opponent'}.")
        if takeover:
            lines.append("A teammate played the earlier moves of this game; you take over from the current "
                         "position. Treat the moves so far as your own and continue the plan you find best.")
        lines.append("")
    lines.append(f"Move {req['move_number']}, {req['color']} to move (ply {req['ply']}).")
    lines.append(f"FEN: {req['fen']}")
    if req.get("ascii_board"):
        lines += ["", "Board (uppercase = White, lowercase = Black, White at the bottom):", req["ascii_board"]]
    lines += ["", f"Moves so far: {req.get('pgn') or '(none)'}"]
    hist = req.get("history_san") or []
    if hist:
        lines.append(f"Opponent's last move: {hist[-1]}")
    if req.get("legal_moves_san"):
        lines.append(f"Legal moves ({len(req['legal_moves_san'])}): {' '.join(req['legal_moves_san'])}")
    if req.get("attempt", 1) > 1 or req.get("previous_error"):
        lines += ["", f"ATTENTION: your previous answer was rejected: {req.get('previous_error')}. "
                      f"This is attempt {req.get('attempt')}; answer with a legal move from the list."]
    if req.get("time_limit_s"):
        lines.append(f"Time limit for this move: {req['time_limit_s']:.0f} s.")
    lines += ["", "End your reply with the line: MOVE: <your move>"]
    return "\n".join(lines)


def parse_claude_json(raw: str) -> dict[str, Any]:
    """The ``--output-format json`` payload: a result object, or a list of messages whose
    last ``type == "result"`` entry is the result."""
    data = json.loads(raw)
    if isinstance(data, list):
        results = [d for d in data if isinstance(d, dict) and d.get("type") == "result"]
        data = results[-1] if results else (data[-1] if data and isinstance(data[-1], dict) else {})
    if not isinstance(data, dict):
        raise ValueError("unexpected claude output")
    return data


def usage_from_result(data: dict[str, Any], model: Optional[str]) -> dict[str, Any]:
    u = data.get("usage") or {}
    out: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        v = u.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = v
    cost = data.get("total_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        out["cost_usd"] = cost
    if data.get("duration_ms") is not None:
        out["duration_ms"] = data["duration_ms"]
    if data.get("num_turns") is not None:
        out["turns"] = data["num_turns"]
    if model:
        out["model"] = model
    return out


def extract_move(fen: str, text: str) -> Optional[str]:
    move = extract_move_text(text)
    if move:
        return move
    try:
        found = find_move_in_text(chess.Board(fen), text)
    except ValueError:
        return None
    return found.uci() if found else None


# ---------------------------------------------------------------- claude
class ClaudeRunner:
    def __init__(self, cfg: RelayConfig) -> None:
        self.cfg = cfg
        # One fixed, empty working directory per relay: no project CLAUDE.md/settings are
        # discovered, and --resume finds its sessions (Claude Code keys them by directory).
        self.workdir = tempfile.mkdtemp(prefix="agentchess-relay-")

    def command(self, session_id: Optional[str]) -> list[str]:
        cmd = shlex.split(self.cfg.claude_cmd) + ["-p", "--output-format", "json"]
        cmd += list(self.cfg.fair_play_args)
        if self.cfg.bare and "--bare" not in cmd:
            cmd.append("--bare")
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        if self.cfg.effort:
            cmd += ["--effort", self.cfg.effort]
        if session_id:
            cmd += ["--resume", session_id]
        elif self.cfg.system_prompt:
            cmd += ["--append-system-prompt", self.cfg.system_prompt]
        cmd += list(self.cfg.extra_args)
        return cmd

    async def ask(self, prompt: str, session_id: Optional[str], timeout_s: float) -> ClaudeResult:
        cmd = self.command(session_id)
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=self.workdir)
        except OSError as e:
            return ClaudeResult("", session_id, {}, error=f"could not start {cmd[0]}: {e}")
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ClaudeResult("", session_id, {}, error=f"claude did not answer within {timeout_s:.0f}s")
        duration = (time.monotonic() - t0) * 1000
        text_out = out.decode(errors="replace")
        if proc.returncode != 0 and not text_out.strip():
            return ClaudeResult("", session_id, {}, duration,
                                error=f"claude exited {proc.returncode}: {err.decode(errors='replace')[-500:]}")
        try:
            data = parse_claude_json(text_out)
        except ValueError:
            return ClaudeResult(text_out, session_id, {}, duration,
                                error=f"claude output is not JSON: {text_out[:200]!r}")
        text = data.get("result")
        if not isinstance(text, str):
            text = json.dumps(text) if text is not None else ""
        result = ClaudeResult(text, data.get("session_id") or session_id, usage_from_result(data, self.cfg.model),
                              duration, raw=data)
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            result.error = f"claude reported {data.get('subtype') or 'an error'}: {text[:300]}"
        return result


# ---------------------------------------------------------------- relay
@dataclass
class GameSession:
    game_id: str
    session_id: Optional[str] = None
    session_moves: int = 0     # accepted moves in the current session
    total_moves: int = 0       # accepted moves by this harness in the game
    sessions: int = 0          # sessions used so far (1 = no handover yet)


class Relay:
    def __init__(self, cfg: RelayConfig, http: Optional[httpx.AsyncClient] = None,
                 runner: Optional[ClaudeRunner] = None) -> None:
        self.cfg = cfg
        self.http = http or httpx.AsyncClient(base_url=cfg.server.rstrip("/"),
                                              headers={"Authorization": f"Bearer {cfg.token}"},
                                              timeout=httpx.Timeout(cfg.poll_wait_s + 30, connect=30))
        self._own_http = http is None
        self.runner = runner or ClaudeRunner(cfg)
        self.games: dict[str, GameSession] = {}
        self.finished: list[str] = []
        self.moves_played = 0
        self.stats = {"requests": 0, "accepted": 0, "illegal": 0, "errors": 0, "handovers": 0}
        self._log_dir = Path(cfg.log_dir) if cfg.log_dir else None
        if self._log_dir:
            self._log_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- helpers
    def _log(self, game_id: str, record: dict[str, Any]) -> None:
        if not self._log_dir:
            return
        record = {"t": time.time(), **record}
        with (self._log_dir / f"{game_id}.jsonl").open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    async def _turn(self) -> Optional[dict[str, Any]]:
        r = await self.http.get("/api/agent/turn", params={"wait": self.cfg.poll_wait_s})
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    async def _post_move(self, req: dict[str, Any], move: str, comment: str,
                         usage: dict[str, Any]) -> dict[str, Any]:
        r = await self.http.post(f"/api/agent/games/{req['game_id']}/move",
                                 json={"move": move, "request_id": req["request_id"],
                                       "comment": comment[:4000], "usage": usage})
        if r.status_code == 409:      # request expired (game ended / timed out) - nothing to do
            return {"accepted": False, "legal": False, "error": r.json().get("detail")}
        r.raise_for_status()
        return r.json()

    async def _refresh_finished(self) -> None:
        """Games we played that are no longer running count as finished."""
        if not self.games:
            return
        r = await self.http.get("/api/agent/games")
        if r.status_code != 200:
            return
        running = {g["game_id"] for g in r.json()}
        for gid in list(self.games):
            if gid not in running:
                self.finished.append(gid)
                gs = self.games.pop(gid)
                self._log(gid, {"event": "game_over", "moves": gs.total_moves, "sessions": gs.sessions})
                log.info("game %s over after %d of our moves (%d sessions)", gid, gs.total_moves, gs.sessions)

    def _timeout_for(self, req: dict[str, Any]) -> float:
        limit = float(req.get("time_limit_s") or 0)
        t = max(10.0, limit * 0.9 - 5.0) if limit else 600.0
        if self.cfg.max_think_s > 0:
            t = min(t, self.cfg.max_think_s)
        return t

    # ---------------------------------------------------------------- one move
    async def handle_request(self, req: dict[str, Any]) -> dict[str, Any]:
        gid = req["game_id"]
        gs = self.games.get(gid)
        if gs is None:
            gs = self.games[gid] = GameSession(game_id=gid)
        new_session = gs.session_id is None
        takeover = new_session and gs.total_moves > 0
        if new_session:
            gs.sessions += 1
            gs.session_moves = 0
        prompt = format_position(req, new_session=new_session, takeover=takeover)
        timeout = self._timeout_for(req)
        deadline = time.monotonic() + timeout
        res = await self.runner.ask(prompt, gs.session_id, timeout)
        if res.error and gs.session_id and "resume" in (res.error or "").lower():
            # a lost session: start over with a fresh one
            gs.session_id = None
            res = await self.runner.ask(format_position(req, new_session=True, takeover=True), None,
                                        max(5.0, deadline - time.monotonic()))
        move = extract_move(req["fen"], res.text) if not res.error else None
        if move is None and not res.error and self.cfg.reask_on_missing_move and res.session_id:
            remaining = deadline - time.monotonic()
            if remaining > 5:
                reask = "Your reply did not end with a `MOVE: <move>` line. Answer now with only that line."
                if req.get("legal_moves_san"):
                    reask += f"\nLegal moves ({len(req['legal_moves_san'])}): {' '.join(req['legal_moves_san'])}"
                again = await self.runner.ask(reask, res.session_id, remaining)
                if not again.error:
                    for k, v in again.usage.items():
                        if isinstance(v, (int, float)) and isinstance(res.usage.get(k, 0), (int, float)):
                            res.usage[k] = res.usage.get(k, 0) + v
                    res.text += "\n" + again.text
                    move = extract_move(req["fen"], again.text)
        gs.session_id = res.session_id or gs.session_id
        usage = dict(res.usage)
        # Strings on purpose: the runner sums numeric usage over illegal-move retries.
        usage["relay_session"] = str(gs.sessions)
        usage["session_move"] = str(gs.session_moves + 1)
        comment = res.text.strip() if res.text else (res.error or "")
        if res.error:
            self.stats["errors"] += 1
            log.warning("game %s ply %s: %s", gid, req.get("ply"), res.error)
        result = await self._post_move(req, move or "", comment, usage)
        self.stats["requests"] += 1
        record = {"event": "move", "ply": req.get("ply"), "attempt": req.get("attempt"), "session": gs.sessions,
                  "session_id": gs.session_id, "move": move, "legal": result.get("legal"),
                  "accepted": result.get("accepted"), "error": result.get("error") or res.error,
                  "usage": usage, "reply": res.text}
        self._log(gid, record)
        if result.get("accepted") and result.get("legal"):
            self.stats["accepted"] += 1
            self.moves_played += 1
            gs.session_moves += 1
            gs.total_moves += 1
            if self.cfg.moves_per_session > 0 and gs.session_moves >= self.cfg.moves_per_session:
                self.stats["handovers"] += 1
                self._log(gid, {"event": "handover", "after_moves": gs.total_moves, "session": gs.sessions})
                log.info("game %s: handing over after %d moves in session %d", gid, gs.session_moves, gs.sessions)
                gs.session_id = None
        elif result.get("accepted"):
            self.stats["illegal"] += 1
        return result

    # ---------------------------------------------------------------- main loop
    async def run(self) -> int:
        me = await self.http.get("/api/agent/me")
        me.raise_for_status()
        log.info("relay playing as %s via %s", me.json()["player"]["id"], self.runner.command(None)[0])
        last_request = time.monotonic()
        try:
            while True:
                req = await self._turn()
                if req is None:
                    await self._refresh_finished()
                    if self.cfg.max_games and len(self.finished) >= self.cfg.max_games:
                        break
                    if self.cfg.idle_exit_s and time.monotonic() - last_request > self.cfg.idle_exit_s:
                        log.info("no move request for %.0fs; exiting", self.cfg.idle_exit_s)
                        break
                    continue
                last_request = time.monotonic()
                try:
                    await self.handle_request(req)
                except httpx.HTTPError as e:
                    self.stats["errors"] += 1
                    log.warning("server error while answering %s: %s", req.get("game_id"), e)
                    await asyncio.sleep(2)
                await self._refresh_finished()
                if self.cfg.max_games and len(self.finished) >= self.cfg.max_games:
                    break
        finally:
            if self._own_http:
                await self.http.aclose()
        log.info("relay done: %s", self.stats)
        return 0


async def run_relay(cfg: RelayConfig) -> int:
    return await Relay(cfg).run()
