"""Agent API: lets external programs play as ``remote`` players.

Transport options (same semantics):
- HTTP long-poll: ``GET /api/agent/turn`` then ``POST /api/agent/games/{id}/move``.
- WebSocket ``/api/agent/ws?token=...``: move requests are pushed, moves sent back.

Auth is a bearer token issued when the remote player was registered; only its
sha256 is stored (``Database.get_player_by_token_hash``).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
from typing import Any, Optional

import chess
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from agentchess.db import Database
from agentchess.models import GameRecord, GameStatus, MoveRequest, MoveResponse, PlayerKind, PlayerSpec
from agentchess.server.registry import hash_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

MAX_WAIT_S = 60.0


class AgentAPIError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class MoveBody(BaseModel):
    move: str
    request_id: Optional[str] = None
    comment: Optional[str] = None
    # Optional self-reported resource usage for this move, e.g.
    # {"input_tokens": 1200, "output_tokens": 800, "cost_usd": 0.02, "model": "..."}; it is
    # stored with the move and summed into the leaderboard's token/cost columns.
    usage: Optional[dict[str, Any]] = None


class ResignBody(BaseModel):
    request_id: Optional[str] = None


MAX_USAGE_KEYS = 16
USAGE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_AGENT_COMMENT_CHARS = 8000


def clean_usage(usage: Any) -> dict[str, Any]:
    """Keep a remote agent's usage report small and well-typed: snake_case keys, finite
    non-negative numbers or short strings; anything else is dropped."""
    if not isinstance(usage, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in usage.items():
        if len(out) >= MAX_USAGE_KEYS or not isinstance(k, str) or not USAGE_KEY_RE.match(k):
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            if math.isfinite(v) and v >= 0:
                out[k] = v
        elif isinstance(v, str) and len(v) <= 64:
            out[k] = v
    return out


def clean_comment(comment: Any) -> Optional[str]:
    if comment is None:
        return None
    s = comment if isinstance(comment, str) else str(comment)
    return s[:MAX_AGENT_COMMENT_CHARS]


# ----------------------------------------------------------------------- auth
def resolve_token(db: Database, token: Optional[str]) -> PlayerSpec:
    """Token -> remote player, or AgentAPIError(401/403)."""
    if not token:
        raise AgentAPIError(401, "missing bearer token")
    player = db.get_player_by_token_hash(hash_token(token.strip()))
    if player is None:
        raise AgentAPIError(401, "invalid token")
    if player.kind != PlayerKind.REMOTE:
        raise AgentAPIError(403, "token does not belong to a remote player")
    if not player.active:
        raise AgentAPIError(403, "player is inactive")
    return player


def _bearer(request: Request) -> Optional[str]:
    """HTTP routes take the token from the Authorization header only: a ``?token=`` query
    parameter would end up in access logs and proxies (the WebSocket route still accepts
    it because browsers cannot set headers on WebSocket connections)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


async def current_agent(request: Request) -> PlayerSpec:
    try:
        player = resolve_token(request.app.state.db, _bearer(request))
    except AgentAPIError as e:
        headers = {"WWW-Authenticate": "Bearer"} if e.status == 401 else None
        raise HTTPException(e.status, e.detail, headers=headers) from None
    request.app.state.hub.touch(player.id)
    return player


def _http(fn):
    """Run ``fn`` translating AgentAPIError into HTTPException."""
    try:
        return fn()
    except AgentAPIError as e:
        raise HTTPException(e.status, e.detail) from None


# -------------------------------------------------------------------- helpers
def current_fen(g: GameRecord) -> str:
    return g.moves[-1].fen_after if g.moves else g.initial_fen


def _player_game(db: Database, player: PlayerSpec, game_id: str, include_moves: bool = True) -> GameRecord:
    g = db.get_game(game_id, include_moves=include_moves)
    if g is None:
        raise AgentAPIError(404, f"game {game_id} not found")
    if player.id not in (g.white_id, g.black_id):
        raise AgentAPIError(403, "you are not playing in this game")
    return g


def _name(db: Database, pid: str) -> str:
    p = db.get_player(pid)
    return p.name if p else pid


def game_summary(db: Database, hub: Any, player: PlayerSpec, g: GameRecord) -> dict[str, Any]:
    white = player.id == g.white_id
    opp = g.black_id if white else g.white_id
    return {
        "game_id": g.id,
        "tournament_id": g.tournament_id,
        "color": "white" if white else "black",
        "opponent": _name(db, opp),
        "opponent_id": opp,
        "fen": current_fen(g),
        "ply": len(g.moves),
        "status": g.status.value,
        "your_turn": hub.find_request(player.id, g.id) is not None,
    }


def check_move(req: MoveRequest, move: str, max_attempts: int) -> dict[str, Any]:
    """Validate ``move`` against the request position (the runner re-validates authoritatively)."""
    from agentchess.moves import parse_move

    board = chess.Board(req.fen)
    san: Optional[str] = None
    error: Optional[str] = None
    try:
        mv = parse_move(board, move)
        if mv is None or mv not in board.legal_moves:
            raise ValueError(f"illegal move {move!r}")
        san = board.san(mv)
    except Exception as e:  # parse_move raises on unparseable/illegal input
        error = str(e) or f"illegal move {move!r}"
    legal = error is None
    remaining = max(0, max_attempts - req.attempt) if not legal else max(0, max_attempts - req.attempt + 1)
    if not legal and remaining == 0:
        error = f"{error} (no attempts left: game forfeited)"
    return {"legal": legal, "san": san, "error": error, "attempts_remaining": remaining}


def submit_move(state: Any, player: PlayerSpec, game_id: str, move: str,
                request_id: Optional[str] = None, comment: Optional[str] = None,
                usage: Any = None) -> dict[str, Any]:
    """Shared by HTTP and WebSocket: validate + hand the move to the hub."""
    db: Database = state.db
    hub = state.hub
    g = _player_game(db, player, game_id, include_moves=False)
    req = hub.find_request(player.id, game_id)
    if req is None:
        raise AgentAPIError(409, "not your turn (no pending move request for this game)")
    if request_id and request_id != req.request_id:
        raise AgentAPIError(409, f"stale request_id {request_id}; current is {req.request_id}")
    move = (move or "").strip()
    result = check_move(req, move, g.config.max_illegal_attempts)
    accepted = hub.submit(player.id, req.request_id,
                          MoveResponse(move=move, comment=clean_comment(comment), usage=clean_usage(usage)))
    if not accepted and result["error"] is None:
        result["error"] = "move request expired"
    return {"accepted": bool(accepted), "game_id": game_id, "request_id": req.request_id,
            "attempt": req.attempt, **result}


def submit_resign(state: Any, player: PlayerSpec, game_id: str, request_id: Optional[str] = None) -> dict[str, Any]:
    hub = state.hub
    _player_game(state.db, player, game_id, include_moves=False)
    req = hub.find_request(player.id, game_id)
    if req is None:
        raise AgentAPIError(409, "you can only resign when it is your turn")
    if request_id and request_id != req.request_id:
        raise AgentAPIError(409, f"stale request_id {request_id}; current is {req.request_id}")
    accepted = hub.submit(player.id, req.request_id, MoveResponse(resign=True))
    return {"accepted": bool(accepted), "game_id": game_id, "request_id": req.request_id}


async def next_request(hub: Any, player_id: str, wait: float) -> Optional[MoveRequest]:
    pending = hub.pending_requests(player_id)
    if pending:
        return pending[0]
    if wait <= 0:
        return None
    return await hub.wait_for_request(player_id, wait)


# ------------------------------------------------------------------ HTTP API
@router.get("/me")
async def me(request: Request, player: PlayerSpec = Depends(current_agent)) -> dict[str, Any]:
    return {"player": player.to_dict(), "online": request.app.state.hub.is_online(player.id)}


@router.get("/turn")
async def turn(request: Request, wait: float = Query(30.0), player: PlayerSpec = Depends(current_agent)):
    hub = request.app.state.hub
    req = await next_request(hub, player.id, min(max(wait, 0.0), MAX_WAIT_S))
    hub.touch(player.id)
    if req is None:
        return Response(status_code=204)
    return req.to_dict()


@router.get("/games")
async def my_games(request: Request, player: PlayerSpec = Depends(current_agent)) -> list[dict[str, Any]]:
    db: Database = request.app.state.db
    games = db.list_games(player_id=player.id, status=GameStatus.RUNNING, limit=1000)
    out = []
    for g in games:
        g.moves = db.get_moves(g.id)
        out.append(game_summary(db, request.app.state.hub, player, g))
    return out


@router.get("/games/{game_id}")
async def my_game(game_id: str, request: Request, player: PlayerSpec = Depends(current_agent)) -> dict[str, Any]:
    db: Database = request.app.state.db
    hub = request.app.state.hub
    g = _http(lambda: _player_game(db, player, game_id))
    req = hub.find_request(player.id, game_id)
    return {
        **g.to_dict(include_moves=True),
        "white_name": _name(db, g.white_id),
        "black_name": _name(db, g.black_id),
        **{k: v for k, v in game_summary(db, hub, player, g).items() if k not in ("status",)},
        "request": req.to_dict() if req else None,
    }


@router.post("/games/{game_id}/move")
async def post_move(game_id: str, body: MoveBody, request: Request,
                    player: PlayerSpec = Depends(current_agent)) -> dict[str, Any]:
    return _http(lambda: submit_move(request.app.state, player, game_id, body.move, body.request_id, body.comment,
                                     body.usage))


@router.post("/games/{game_id}/resign")
async def post_resign(game_id: str, request: Request, body: Optional[ResignBody] = None,
                      player: PlayerSpec = Depends(current_agent)) -> dict[str, Any]:
    rid = body.request_id if body else None
    return _http(lambda: submit_resign(request.app.state, player, game_id, rid))


# ------------------------------------------------------------------ WebSocket
@router.websocket("/ws")
async def agent_ws(ws: WebSocket, token: Optional[str] = Query(None)) -> None:
    state = ws.app.state
    await ws.accept()
    bearer = token
    auth = ws.headers.get("authorization", "")
    if not bearer and auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
    try:
        player = resolve_token(state.db, bearer)
    except AgentAPIError as e:
        await ws.send_json({"type": "error", "status": e.status, "detail": e.detail})
        await ws.close(code=4401 if e.status == 401 else 4403)
        return

    hub = state.hub
    pid = player.id
    lock = asyncio.Lock()

    async def send(msg: dict[str, Any]) -> None:
        async with lock:
            await ws.send_json(msg)

    async def push_requests() -> None:
        sent: set[str] = set()
        while True:
            req = await hub.wait_for_request(pid, 30.0, exclude=sent)
            sent &= {r.request_id for r in hub.pending_requests(pid)}
            if req is not None and req.request_id not in sent:
                sent.add(req.request_id)
                await send({"type": "move_request", "request": req.to_dict()})

    async def push_notifications() -> None:
        while True:
            note = await hub.next_notification(pid, 30.0)
            if note is not None:
                await send(note)

    async def receive() -> None:
        while True:
            raw = await ws.receive_text()
            hub.touch(pid)
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = None
            if not isinstance(msg, dict):
                await send({"type": "error", "detail": "messages must be JSON objects"})
                continue
            mtype = msg.get("type")
            try:
                if mtype == "ping":
                    await send({"type": "pong"})
                elif mtype == "move":
                    res = submit_move(state, player, str(msg.get("game_id", "")), str(msg.get("move", "")),
                                      msg.get("request_id"), msg.get("comment"), msg.get("usage"))
                    await send({"type": "move_result", **res})
                elif mtype == "resign":
                    res = submit_resign(state, player, str(msg.get("game_id", "")), msg.get("request_id"))
                    await send({"type": "resign_result", **res})
                else:
                    await send({"type": "error", "detail": f"unknown message type {mtype!r}"})
            except AgentAPIError as e:
                await send({"type": "error", "status": e.status, "detail": e.detail,
                            "game_id": msg.get("game_id"), "for": mtype})

    _hub_call(hub, "connect", pid)
    await send({"type": "hello", "player": player.to_dict()})
    tasks = [asyncio.create_task(c) for c in (push_requests(), push_notifications(), receive())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
                log.warning("agent websocket for %s ended: %r", pid, exc)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        _hub_call(hub, "disconnect", pid)
        with contextlib.suppress(Exception):
            await ws.close()


def _hub_call(hub: Any, method: str, player_id: str) -> None:
    fn = getattr(hub, method, None)
    if fn is not None:
        fn(player_id)
