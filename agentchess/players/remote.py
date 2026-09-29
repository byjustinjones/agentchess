"""Remote agents: external programs that play over the agent API (HTTP long-poll,
WebSocket or the MCP bridge).

``AgentHub`` is the rendezvous point between the game runner (via
``RemotePlayer.get_move`` → ``request_move``) and the agent API routes
(``wait_for_request`` / ``submit`` / ``next_notification``). It is purely
in-memory and single-event-loop.
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import time
from typing import Any, Collection, Optional

from agentchess.events import EventBus
from agentchess.models import MoveRequest, MoveResponse, PlayerSpec
from agentchess.players.base import GameEnd, GameStart, Player

_Pending = tuple[MoveRequest, "asyncio.Future[MoveResponse]"]

READY_WINDOW_S = 10.0   # an agent seen this recently may be handed a new game (see AgentHub.is_ready)


class AgentHub:
    def __init__(self, bus: Optional[EventBus] = None, max_notifications: int = 100) -> None:
        self.bus = bus
        self._pending: dict[str, dict[str, _Pending]] = {}      # player_id -> {request_id: (req, future)}
        self._notifications: dict[str, collections.deque[dict[str, Any]]] = {}
        self._max_notifications = max_notifications
        self._events: dict[str, asyncio.Event] = {}             # per-player "something changed" broadcast
        self._last_seen: dict[str, float] = {}
        self._polling: dict[str, int] = {}
        self._connections: dict[str, int] = {}
        self._published_online: dict[str, bool] = {}

    # ---------------------------------------------------------------- wakeups
    def _event(self, player_id: str) -> asyncio.Event:
        ev = self._events.get(player_id)
        if ev is None:
            ev = self._events[player_id] = asyncio.Event()
        return ev

    def _wake(self, player_id: str) -> None:
        """Wake every waiter of this player; later waiters get a fresh event."""
        ev = self._events.pop(player_id, None)
        if ev is not None:
            ev.set()

    async def _wait_change(self, player_id: str, ev: asyncio.Event, timeout_s: float) -> None:
        if timeout_s <= 0:
            return
        try:
            await asyncio.wait_for(ev.wait(), timeout_s)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------ runner-facing API
    async def request_move(self, player_id: str, req: MoveRequest) -> MoveResponse:
        """Queue ``req`` for the agent and wait for its answer (cancel to give up)."""
        pending = self._pending.setdefault(player_id, {})
        for rid, (other, fut) in list(pending.items()):   # one pending request per (player, game)
            if other.game_id == req.game_id:
                pending.pop(rid, None)
                if not fut.done():
                    fut.set_exception(RuntimeError(f"request {rid} superseded by {req.request_id}"))
        fut: asyncio.Future[MoveResponse] = asyncio.get_running_loop().create_future()
        pending[req.request_id] = (req, fut)
        self._wake(player_id)
        try:
            return await fut
        finally:
            entry = pending.get(req.request_id)
            if entry is not None and entry[1] is fut:
                pending.pop(req.request_id, None)
            if not pending:
                self._pending.pop(player_id, None)

    def notify_game_start(self, player_id: str, info: GameStart) -> None:
        self._push_notification(player_id, {"type": "game_started", **dataclasses.asdict(info)})

    def notify_game_end(self, player_id: str, info: GameEnd) -> None:
        for rid, (req, fut) in list(self._pending.get(player_id, {}).items()):
            if req.game_id == info.game_id and not fut.done():
                fut.set_exception(RuntimeError(f"game {info.game_id} ended"))
        self._push_notification(player_id, {"type": "game_ended", **dataclasses.asdict(info)})

    def _push_notification(self, player_id: str, msg: dict[str, Any]) -> None:
        q = self._notifications.get(player_id)
        if q is None:
            q = self._notifications[player_id] = collections.deque(maxlen=self._max_notifications)
        q.append(msg)
        self._wake(player_id)

    # --------------------------------------------------------- agent-facing API
    def touch(self, player_id: str) -> None:
        self._last_seen[player_id] = time.time()
        self._check_status(player_id)

    def connect(self, player_id: str) -> None:
        """A WebSocket for this agent opened (call ``disconnect`` when it closes)."""
        self._connections[player_id] = self._connections.get(player_id, 0) + 1
        self.touch(player_id)

    def disconnect(self, player_id: str) -> None:
        n = self._connections.get(player_id, 0) - 1
        if n > 0:
            self._connections[player_id] = n
        else:
            self._connections.pop(player_id, None)
        self.touch(player_id)

    def is_online(self, player_id: str, within_s: float = 60) -> bool:
        if self._connections.get(player_id, 0) > 0 or self._polling.get(player_id, 0) > 0:
            return True
        seen = self._last_seen.get(player_id)
        return seen is not None and time.time() - seen <= within_s

    def is_ready(self, player_id: str, within_s: float = READY_WINDOW_S) -> bool:
        """Stricter than :meth:`is_online`, for *starting* a game: the agent must be waiting
        right now (open WebSocket or an in-flight long-poll) or have talked to us within
        the last few seconds. A crashed agent otherwise stays "online" for a minute and
        gets handed a game it will forfeit on time."""
        return self.is_online(player_id, within_s=within_s)

    def pending_requests(self, player_id: str) -> list[MoveRequest]:
        """Unanswered requests for this agent, oldest first."""
        return [req for req, fut in self._pending.get(player_id, {}).values() if not fut.done()]

    async def wait_for_request(self, player_id: str, timeout_s: float,
                               exclude: Collection[str] = ()) -> Optional[MoveRequest]:
        """Long-poll: the oldest pending request (skipping request ids in ``exclude``),
        waiting up to ``timeout_s`` for one to appear. Marks the agent online meanwhile."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_s)
        self._polling[player_id] = self._polling.get(player_id, 0) + 1
        self.touch(player_id)
        try:
            while True:
                ev = self._event(player_id)
                for req in self.pending_requests(player_id):
                    if req.request_id not in exclude:
                        return req
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                await self._wait_change(player_id, ev, remaining)
        finally:
            n = self._polling.get(player_id, 1) - 1
            if n > 0:
                self._polling[player_id] = n
            else:
                self._polling.pop(player_id, None)
            self.touch(player_id)

    def submit(self, player_id: str, request_id: str, resp: MoveResponse) -> bool:
        """Answer a pending request. Returns False if it is unknown or already expired/answered."""
        self.touch(player_id)
        entry = self._pending.get(player_id, {}).get(request_id)
        if entry is None or entry[1].done():
            return False
        entry[1].set_result(resp)
        self._pending[player_id].pop(request_id, None)
        self._wake(player_id)
        return True

    def find_request(self, player_id: str, game_id: str) -> Optional[MoveRequest]:
        for req in self.pending_requests(player_id):
            if req.game_id == game_id:
                return req
        return None

    async def next_notification(self, player_id: str, timeout_s: float) -> Optional[dict[str, Any]]:
        """Next ``game_started`` / ``game_ended`` message for this agent (None on timeout)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_s)
        while True:
            ev = self._event(player_id)
            q = self._notifications.get(player_id)
            if q:
                return q.popleft()
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            await self._wait_change(player_id, ev, remaining)

    # ----------------------------------------------------------------- status
    def _check_status(self, player_id: str) -> None:
        online = self.is_online(player_id)
        if self._published_online.get(player_id) != online:
            self._published_online[player_id] = online
            if self.bus is not None:
                self.bus.publish({"type": "agent_status", "player_id": player_id, "online": online})

    def sweep(self) -> None:
        """Re-evaluate online status of all known agents (publishes offline transitions
        caused by inactivity). Call periodically, e.g. every ~15 s."""
        for pid in self._known_ids():
            self._check_status(pid)

    def _known_ids(self) -> list[str]:
        ids = set(self._last_seen) | set(self._pending) | set(self._connections) | set(self._polling)
        return sorted(ids)

    def status(self) -> list[dict[str, Any]]:
        self.sweep()
        return [
            {
                "player_id": pid,
                "online": self.is_online(pid),
                "last_seen": self._last_seen.get(pid),
                "pending": len(self.pending_requests(pid)),
                "connections": self._connections.get(pid, 0),
                "polling": self._polling.get(pid, 0),
            }
            for pid in self._known_ids()
        ]


class RemotePlayer(Player):
    """A player whose moves come from an external agent through an ``AgentHub``."""

    def __init__(self, spec: PlayerSpec, hub: AgentHub) -> None:
        super().__init__(spec)
        self.hub = hub

    async def start_game(self, info: GameStart) -> None:
        self.hub.notify_game_start(self.id, info)

    async def get_move(self, request: MoveRequest) -> MoveResponse:
        return await self.hub.request_move(self.id, request)

    async def end_game(self, info: GameEnd) -> None:
        self.hub.notify_game_end(self.id, info)
