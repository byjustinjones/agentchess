"""In-process async pub/sub used to stream live updates to the GUI.

Events are plain JSON-serializable dicts with a ``type`` key. See
docs/ARCHITECTURE.md ("Events") for the catalogue.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, max_queue: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_queue = max_queue

    def subscribe(self) -> "asyncio.Queue[dict[str, Any]]":
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[dict[str, Any]]") -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        """Non-blocking; slow subscribers drop their oldest events."""
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover
                log.warning("dropping event for slow subscriber")


class NullBus(EventBus):
    def publish(self, event: dict[str, Any]) -> None:  # noqa: D401
        return
