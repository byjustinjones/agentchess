"""Player construction and ready-made player presets."""
from __future__ import annotations

import copy
from typing import Any, Optional

from agentchess.models import PlayerKind, PlayerSpec
from agentchess.players.base import Player, PlayerContext

_DEFAULT_CONCURRENCY = {
    PlayerKind.ENGINE: 2,
    PlayerKind.LLM: 4,
    PlayerKind.REMOTE: 1,
    PlayerKind.RANDOM: 8,
}


def default_max_concurrency(kind: PlayerKind | str) -> int:
    """Sensible default for ``PlayerSpec.max_concurrent_games`` per player kind."""
    return _DEFAULT_CONCURRENCY.get(PlayerKind(kind), 1)


def create_player(spec: PlayerSpec, ctx: PlayerContext) -> Player:
    """Instantiate a fresh Player for one game. Raises ValueError on bad specs."""
    kind = PlayerKind(spec.kind)
    if kind == PlayerKind.RANDOM:
        from agentchess.players.random_player import RandomPlayer
        return RandomPlayer(spec)
    if kind == PlayerKind.ENGINE:
        from agentchess.players.engine import EnginePlayer
        return EnginePlayer(spec, default_path=ctx.settings.get("stockfish_path"))
    if kind == PlayerKind.LLM:
        from agentchess.players.llm import LLMPlayer
        return LLMPlayer(spec)
    if kind == PlayerKind.REMOTE:
        if ctx.agent_hub is None:
            raise ValueError(f"remote player {spec.id!r} needs an AgentHub (PlayerContext.agent_hub is not set)")
        from agentchess.players.remote import RemotePlayer
        return RemotePlayer(spec, ctx.agent_hub)
    raise ValueError(f"unknown player kind {spec.kind!r}")  # pragma: no cover


def _engine(id: str, name: str, config: dict[str, Any], anchor: Optional[float] = None,
            concurrency: int = 2) -> dict[str, Any]:
    return {"id": id, "name": name, "kind": PlayerKind.ENGINE.value, "config": config,
            "anchor_elo": anchor, "max_concurrent_games": concurrency}


# Ready-made PlayerSpec dicts for the GUI "add player" form / CLI.
# Stockfish's UCI_Elo floor is 1320; weaker levels use "Skill Level" (not anchored,
# since Skill Level has no calibrated Elo).
PRESETS: list[dict[str, Any]] = [
    {"id": "random", "name": "Random mover", "kind": PlayerKind.RANDOM.value, "config": {},
     "anchor_elo": None, "max_concurrent_games": 8},
    _engine("sf-skill0-d1", "Stockfish skill 0 (depth 1)", {"skill_level": 0, "depth": 1, "movetime_ms": 50}),
    _engine("sf-skill3", "Stockfish skill 3", {"skill_level": 3, "movetime_ms": 100}),
    _engine("sf-skill6", "Stockfish skill 6", {"skill_level": 6, "movetime_ms": 100}),
    _engine("sf-1320", "Stockfish 1320", {"uci_elo": 1320, "movetime_ms": 100}, anchor=1320),
    _engine("sf-1500", "Stockfish 1500", {"uci_elo": 1500, "movetime_ms": 100}, anchor=1500),
    _engine("sf-1800", "Stockfish 1800", {"uci_elo": 1800, "movetime_ms": 100}, anchor=1800),
    _engine("sf-2100", "Stockfish 2100", {"uci_elo": 2100, "movetime_ms": 100}, anchor=2100),
    _engine("sf-2500", "Stockfish 2500", {"uci_elo": 2500, "movetime_ms": 100}, anchor=2500),
    _engine("sf-max", "Stockfish (full strength, 1s/move)", {"movetime_ms": 1000, "hash_mb": 64}, concurrency=1),
]


def get_preset(preset_id: str) -> Optional[dict[str, Any]]:
    """A deep copy of the preset dict with this id, or None."""
    for p in PRESETS:
        if p["id"] == preset_id:
            return copy.deepcopy(p)
    return None


def preset_spec(preset_id: str) -> PlayerSpec:
    """Build a PlayerSpec from a preset id (ValueError if unknown)."""
    p = get_preset(preset_id)
    if p is None:
        raise ValueError(f"unknown preset {preset_id!r}")
    return PlayerSpec(id=p["id"], name=p["name"], kind=PlayerKind(p["kind"]), config=p["config"],
                      anchor_elo=p["anchor_elo"], max_concurrent_games=p["max_concurrent_games"])
