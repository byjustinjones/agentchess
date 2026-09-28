"""Player interface. Every participant (engine, LLM, remote agent, random) implements this."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from agentchess.models import MoveRequest, MoveResponse, PlayerSpec


@dataclass
class GameStart:
    game_id: str
    color: str            # "white" | "black"
    opponent_id: str
    opponent_name: str
    initial_fen: str
    tournament_id: Optional[str] = None


@dataclass
class GameEnd:
    game_id: str
    result: str           # "1-0" | "0-1" | "1/2-1/2" | "*"
    termination: str
    detail: Optional[str] = None


class Player(ABC):
    """One Player instance is created per (player, game) by ``create_player``.

    Lifecycle: ``start_game`` -> ``get_move`` (repeatedly) -> ``end_game`` -> ``close``.
    ``close`` is always called, even if the game errors. ``get_move`` may be
    cancelled (asyncio.CancelledError) when the move timeout expires.
    """

    def __init__(self, spec: PlayerSpec) -> None:
        self.spec = spec

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def name(self) -> str:
        return self.spec.name

    async def start_game(self, info: GameStart) -> None:
        return None

    @abstractmethod
    async def get_move(self, request: MoveRequest) -> MoveResponse:
        """Return a move for ``request``. Illegal answers are allowed; the runner
        validates and re-asks with ``attempt+1`` and ``previous_error`` set."""

    async def end_game(self, info: GameEnd) -> None:
        return None

    async def close(self) -> None:
        return None


class PlayerContext:
    """Shared services a player factory may need (injected by the server / CLI)."""

    def __init__(self, agent_hub: Any = None, settings: Optional[dict[str, Any]] = None) -> None:
        self.agent_hub = agent_hub       # agentchess.players.remote.AgentHub (for REMOTE players)
        self.settings = settings or {}
