"""Player registration helpers shared by the HTTP API and the CLI.

Slug ids, token issuing/hashing, kind-specific config validation, default
player seeding and stockfish discovery live here so ``app.py`` and ``cli.py``
behave identically.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import secrets
import shutil
from typing import Any, Optional

from agentchess.db import Database
from agentchess.models import PlayerKind, PlayerSpec

TOKEN_PREFIX = "ac_"
STOCKFISH_ELO_RANGE = (1320, 3190)
_FALLBACK_CONCURRENCY = {"engine": 2, "llm": 4, "remote": 1, "random": 8}
DEFAULT_SEED_PRESETS = ["random", "sf-skill0-d1", "sf-skill3", "sf-1320", "sf-1500", "sf-1800"]


class RegistryError(ValueError):
    """Invalid player definition (maps to HTTP 400)."""


# --------------------------------------------------------------------- tokens
def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ ids/slugs
def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:48].strip("-") or "player"


def unique_player_id(db: Database, base: str) -> str:
    slug = slugify(base)
    if db.get_player(slug) is None:
        return slug
    i = 2
    while db.get_player(f"{slug}-{i}") is not None:
        i += 1
    return f"{slug}-{i}"


# ----------------------------------------------------------------- stockfish
def find_stockfish() -> Optional[str]:
    """$STOCKFISH_PATH, then PATH, then /usr/games/stockfish."""
    env = os.environ.get("STOCKFISH_PATH")
    if env and os.path.exists(env):
        return env
    found = shutil.which("stockfish")
    if found:
        return found
    for p in ("/usr/games/stockfish", "/usr/local/bin/stockfish", "/opt/homebrew/bin/stockfish"):
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------- validation
def parse_kind(kind: Any) -> PlayerKind:
    try:
        return PlayerKind(kind.value if isinstance(kind, PlayerKind) else str(kind).lower())
    except ValueError:
        raise RegistryError(f"invalid kind {kind!r}; expected one of {[k.value for k in PlayerKind]}") from None


def default_concurrency(kind: PlayerKind) -> int:
    try:
        from agentchess.players.factory import default_max_concurrency

        return int(default_max_concurrency(kind))
    except Exception:  # factory not available / signature mismatch
        return _FALLBACK_CONCURRENCY[kind.value]


def _int_opt(cfg: dict[str, Any], key: str, lo: int, hi: Optional[int] = None) -> None:
    v = cfg.get(key)
    if v is None:
        return
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
        raise RegistryError(f"config.{key} must be an integer")
    if v < lo or (hi is not None and v > hi):
        rng = f"{lo}..{hi}" if hi is not None else f">= {lo}"
        raise RegistryError(f"config.{key} must be in {rng}")


def validate_config(kind: PlayerKind, config: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Sanity-check kind-specific config; returns a cleaned copy."""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise RegistryError("config must be an object")
    cfg = dict(config)
    if kind == PlayerKind.ENGINE:
        path = cfg.get("path")
        if path:
            if not (os.path.exists(path) or shutil.which(path)):
                raise RegistryError(f"engine path not found: {path}")
        elif find_stockfish() is None:
            raise RegistryError("stockfish not found (install it or set config.path / $STOCKFISH_PATH)")
        if not path:  # Stockfish-specific ranges
            _int_opt(cfg, "uci_elo", *STOCKFISH_ELO_RANGE)
        else:
            _int_opt(cfg, "uci_elo", 1)
        _int_opt(cfg, "skill_level", 0, 20)
        _int_opt(cfg, "movetime_ms", 1, 600_000)
        _int_opt(cfg, "depth", 1, 100)
        _int_opt(cfg, "nodes", 1)
        _int_opt(cfg, "threads", 1, 256)
        _int_opt(cfg, "hash_mb", 1, 65_536)
        if "options" in cfg and not isinstance(cfg["options"], dict):
            raise RegistryError("config.options must be an object")
    elif kind == PlayerKind.LLM:
        provider = cfg.setdefault("provider", "anthropic")
        if provider not in ("anthropic", "openai"):
            raise RegistryError("config.provider must be 'anthropic' or 'openai'")
        if not isinstance(cfg.get("model"), str) or not cfg["model"].strip():
            raise RegistryError("config.model is required for llm players")
        if "api_key" in cfg:
            raise RegistryError("do not store API keys; use config.api_key_env (name of an environment variable)")
        _int_opt(cfg, "max_tokens", 1, 128_000)
        if cfg.get("effort") is not None and cfg["effort"] not in ("low", "medium", "high", "xhigh", "max"):
            raise RegistryError("config.effort must be one of low, medium, high, xhigh, max")
    return cfg


# -------------------------------------------------------------- registration
def build_spec(
    db: Database,
    *,
    name: str,
    kind: Any,
    config: Optional[dict[str, Any]] = None,
    id: Optional[str] = None,
    anchor_elo: Optional[float] = None,
    max_concurrent_games: Optional[int] = None,
) -> PlayerSpec:
    """Validate a new player definition and return an unsaved PlayerSpec with a unique id."""
    if not name or not str(name).strip():
        raise RegistryError("name is required")
    k = parse_kind(kind)
    cfg = validate_config(k, config)
    mcg = max_concurrent_games if max_concurrent_games is not None else default_concurrency(k)
    if int(mcg) < 1:
        raise RegistryError("max_concurrent_games must be >= 1")
    return PlayerSpec(
        id=unique_player_id(db, id or name),
        name=str(name).strip(),
        kind=k,
        config=cfg,
        anchor_elo=float(anchor_elo) if anchor_elo is not None else None,
        max_concurrent_games=int(mcg),
    )


def register_player(db: Database, **fields: Any) -> tuple[PlayerSpec, Optional[str]]:
    """Validate + insert a player. Returns ``(spec, token)``; token only for remote players."""
    spec = build_spec(db, **fields)
    token = new_token() if spec.kind == PlayerKind.REMOTE else None
    db.add_player(spec, token_hash=hash_token(token) if token else None)
    return spec, token


def rotate_token(db: Database, player_id: str) -> str:
    token = new_token()
    db.set_player_token(player_id, hash_token(token))
    return token


def get_presets() -> list[dict[str, Any]]:
    try:
        from agentchess.players.factory import PRESETS

        return copy.deepcopy(list(PRESETS))
    except Exception:
        return [{"id": "random", "name": "Random", "kind": "random", "config": {}, "anchor_elo": None,
                 "max_concurrent_games": 8}]


def find_preset(preset_id: str) -> Optional[dict[str, Any]]:
    return next((p for p in get_presets() if p.get("id") == preset_id), None)


def seed_default_players(db: Database, preset_ids: Optional[list[str]] = None) -> list[PlayerSpec]:
    """On an empty DB, add ``random`` plus a small Stockfish ladder (if stockfish is installed)."""
    if db.list_players(include_inactive=True):
        return []
    have_sf = find_stockfish() is not None
    added = []
    for pid in preset_ids or DEFAULT_SEED_PRESETS:
        p = find_preset(pid)
        if p is None:
            continue
        kind = parse_kind(p.get("kind"))
        if kind == PlayerKind.ENGINE and not have_sf:
            continue
        spec = PlayerSpec(
            id=p["id"], name=p.get("name", p["id"]), kind=kind, config=dict(p.get("config") or {}),
            anchor_elo=p.get("anchor_elo"),
            max_concurrent_games=int(p.get("max_concurrent_games") or default_concurrency(kind)),
        )
        db.add_player(spec)
        added.append(spec)
    return added
