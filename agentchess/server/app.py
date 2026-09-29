"""FastAPI application: REST API, live event WebSocket, agent API and static GUI.

``create_app(Settings(...))`` wires Database, EventBus, AgentHub and
TournamentManager in the lifespan and stores them on ``app.state``.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import chess
import chess.pgn
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agentchess import __version__
from agentchess.db import Database
from agentchess.events import EventBus
from agentchess.models import (
    GameConfig,
    GameRecord,
    GameStatus,
    PlayerKind,
    PlayerSpec,
    TournamentConfig,
    TournamentStatus,
)
from agentchess.players.base import PlayerContext
from agentchess.server import agent_api
from agentchess.server.registry import (
    RegistryError,
    find_preset,
    find_stockfish,
    get_presets,
    register_player,
    rotate_token,
    seed_default_players,
    validate_config,
)

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@dataclass
class Settings:
    db_path: str = "data/agentchess.db"
    host: str = "127.0.0.1"
    port: int = 8000
    web_dir: str = str(WEB_DIR)
    resume: bool = True          # restart tournaments left RUNNING by a previous process
    seed: bool = True            # add default players (random + stockfish ladder) to an empty DB
    bootstrap: int = 200         # bootstrap resamples for rating confidence intervals
    cors_origins: list[str] = field(default_factory=list)
    # Post-game engine analysis (ACPL, blunders...): depth 0 disables it; ``analysis_auto``
    # analyses every game as it finishes (one low-priority single-threaded engine process).
    analysis_depth: int = 12
    analysis_auto: bool = True


# ------------------------------------------------------------------- runtime
class Runtime:
    """The live services (shared by the HTTP server and the headless CLI)."""

    def __init__(self, db: Database, analysis_depth: int = 12, analysis_auto: bool = True) -> None:
        from agentchess.analysis import AnalysisService
        from agentchess.players.remote import AgentHub
        from agentchess.tournament import TournamentManager

        self.db = db
        self.bus = EventBus()
        self.hub = AgentHub(self.bus)
        self.ctx = PlayerContext(agent_hub=self.hub)
        self.manager = TournamentManager(db, self.bus, self.ctx)
        self.analysis = AnalysisService(db, find_stockfish(), depth=analysis_depth, bus=self.bus)
        self.analysis_auto = analysis_auto
        self._sweeper: Optional[asyncio.Task] = None
        self._watcher: Optional[asyncio.Task] = None

    @classmethod
    def open(cls, db_path: str, seed: bool = False, analysis_depth: int = 12,
             analysis_auto: bool = True) -> "Runtime":
        db = Database(db_path)
        if seed:
            try:
                added = seed_default_players(db)
                if added:
                    log.info("seeded default players: %s", ", ".join(p.id for p in added))
            except Exception:  # never block startup on seeding
                log.exception("seeding default players failed")
        return cls(db, analysis_depth=analysis_depth, analysis_auto=analysis_auto)

    async def start(self, resume: bool = True) -> None:
        if resume:
            await self.manager.resume_all()
        else:
            self.db.recover_interrupted_games()
        self._sweeper = asyncio.create_task(self._sweep_agents())
        self.analysis.start()
        if self.analysis_auto and self.analysis.available:
            self._watcher = asyncio.create_task(self._watch_finished())

    async def _sweep_agents(self, interval_s: float = 15.0) -> None:
        """Periodically re-check agent presence so silent agents are reported offline."""
        while True:
            await asyncio.sleep(interval_s)
            try:
                self.hub.sweep()
            except Exception:  # pragma: no cover
                log.exception("agent sweep failed")

    async def _watch_finished(self) -> None:
        """Queue every finished game for engine analysis."""
        q = self.bus.subscribe()
        try:
            while True:
                ev = await q.get()
                if ev.get("type") == "game_finished" and (ev.get("game") or {}).get("status") == "finished":
                    self.analysis.enqueue(ev["game"]["id"])
        finally:
            self.bus.unsubscribe(q)

    async def stop(self) -> None:
        for task in (self._sweeper, self._watcher):
            if task:
                task.cancel()
        try:
            await self.manager.shutdown()
            await self.analysis.stop()
        finally:
            self.db.close()


# --------------------------------------------------------------- ratings cache
def row_to_dict(row: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(row):
        return dataclasses.asdict(row)
    return dict(row) if isinstance(row, dict) else dict(vars(row))


class RatingService:
    """rating_report with a cache keyed by (tournament, anchors, #rated games, player anchors)."""

    def __init__(self, bootstrap: int) -> None:
        self.bootstrap = bootstrap
        self._cache: dict[tuple, Any] = {}

    async def report(self, db: Database, tournament_id: Optional[str] = None, anchors: bool = True,
                     results: Optional[list[dict[str, Any]]] = None) -> Any:
        """``RatingReport`` (rows + pairwise superiority)."""
        from agentchess.rating import rating_report

        results = db.rated_results(tournament_id) if results is None else results
        players = db.list_players(include_inactive=True)
        sig = tuple((p.id, p.name, p.anchor_elo) for p in players)
        key = (tournament_id, anchors, len(results), results[-1]["id"] if results else None, sig)
        if key not in self._cache:
            rep = await asyncio.to_thread(
                rating_report, results, players, use_anchors=anchors, bootstrap=self.bootstrap)
            if len(self._cache) > 64:
                self._cache.clear()
            self._cache[key] = rep
        return self._cache[key]

    async def ratings(self, db: Database, tournament_id: Optional[str] = None, anchors: bool = True,
                      results: Optional[list[dict[str, Any]]] = None) -> list[Any]:
        return (await self.report(db, tournament_id, anchors, results)).rows


# ------------------------------------------------------------------ helpers
def _names(db: Database) -> dict[str, str]:
    return {p.id: p.name for p in db.list_players(include_inactive=True)}


def player_stats(db: Database, tournament_id: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """Per-player move stats + forfeit counts + engine move-quality stats (when analysed)."""
    stats: dict[str, dict[str, Any]] = {}
    for pid, d in db.move_stats(tournament_id).items():
        stats.setdefault(pid, {}).update({k: v for k, v in d.items() if k != "player_id"})
    for pid, d in db.termination_stats(tournament_id).items():
        stats.setdefault(pid, {}).update(d)
    for pid, d in db.analysis_stats(tournament_id).items():
        stats.setdefault(pid, {}).update(d)
    return stats


def game_dict(g: GameRecord, names: dict[str, str], include_moves: bool = False) -> dict[str, Any]:
    d = g.to_dict(include_moves=include_moves)
    d["white_name"] = names.get(g.white_id, g.white_id)
    d["black_name"] = names.get(g.black_id, g.black_id)
    return d


def _fallback_pgn(g: GameRecord, names: dict[str, str]) -> str:
    game = chess.pgn.Game()
    game.headers["Event"] = g.tournament_id or "agentchess exhibition"
    game.headers["Site"] = "agentchess"
    game.headers["White"] = names.get(g.white_id, g.white_id)
    game.headers["Black"] = names.get(g.black_id, g.black_id)
    game.headers["Result"] = g.result or "*"
    if g.termination:
        game.headers["Termination"] = g.termination.value
    if g.opening:
        game.headers["Opening"] = g.opening.name
    board = chess.Board(g.initial_fen)
    if g.initial_fen != chess.STARTING_FEN:
        game.setup(board)
    node: chess.pgn.GameNode = game
    for m in g.moves:
        try:
            node = node.add_variation(chess.Move.from_uci(m.uci))
        except ValueError:
            break
    return str(game)


def game_pgn(g: GameRecord, names: dict[str, str]) -> str:
    """Stored PGN, else built on the fly (``game.build_pgn`` when available)."""
    if g.pgn:
        return g.pgn
    try:
        from agentchess.game import build_pgn

        return str(build_pgn(g, names))
    except Exception:
        return _fallback_pgn(g, names)


def _player_or_404(db: Database, player_id: str) -> PlayerSpec:
    p = db.get_player(player_id)
    if p is None:
        raise HTTPException(404, f"player {player_id} not found")
    return p


def _tournament_or_404(db: Database, tournament_id: str):
    t = db.get_tournament(tournament_id)
    if t is None:
        raise HTTPException(404, f"tournament {tournament_id} not found")
    return t


def _player_out(p: PlayerSpec, hub: Any) -> dict[str, Any]:
    d = p.to_dict()
    d["online"] = hub.is_online(p.id) if p.kind == PlayerKind.REMOTE else None
    return d


def _validate_game_config(cfg: GameConfig) -> None:
    if cfg.move_timeout_s <= 0:
        raise HTTPException(400, "game.move_timeout_s must be > 0")
    if cfg.max_illegal_attempts < 1:
        raise HTTPException(400, "game.max_illegal_attempts must be >= 1")
    if cfg.max_plies < 1:
        raise HTTPException(400, "game.max_plies must be >= 1")


def _validate_tournament_config(db: Database, cfg: TournamentConfig) -> None:
    if cfg.format not in ("round_robin", "gauntlet"):
        raise HTTPException(400, "format must be 'round_robin' or 'gauntlet'")
    if cfg.openings not in ("builtin", "none"):
        raise HTTPException(400, "openings must be 'builtin' or 'none'")
    if len(set(cfg.player_ids)) != len(cfg.player_ids):
        raise HTTPException(400, "duplicate player ids")
    if len(cfg.player_ids) < 2:
        raise HTTPException(400, "a tournament needs at least 2 players")
    for pid in cfg.player_ids:
        p = db.get_player(pid)
        if p is None:
            raise HTTPException(400, f"unknown player {pid}")
        if not p.active:
            raise HTTPException(400, f"player {pid} is inactive")
    if cfg.format == "gauntlet":
        if not cfg.candidate_ids:
            raise HTTPException(400, "gauntlet needs candidate_ids")
        if not set(cfg.candidate_ids) <= set(cfg.player_ids):
            raise HTTPException(400, "candidate_ids must be a subset of player_ids")
    if cfg.games_per_pair < 1:
        raise HTTPException(400, "games_per_pair must be >= 1")
    if cfg.openings == "builtin" and cfg.games_per_pair % 2:
        raise HTTPException(400, "games_per_pair must be even with builtin openings (each opening is "
                                 "played once with each colour); use openings 'none' for an odd number")
    if cfg.concurrency < 1:
        raise HTTPException(400, "concurrency must be >= 1")
    _validate_game_config(cfg.game)


# ------------------------------------------------------------ request models
# Typed mirrors of the config dataclasses: a body with the wrong types (``"games_per_pair": "two"``)
# is rejected with 422 by FastAPI instead of crashing later in the scheduler.
class GameConfigIn(BaseModel):
    move_timeout_s: float = 300.0
    max_illegal_attempts: int = 3
    max_plies: int = 300
    show_legal_moves: bool = True


class TournamentConfigIn(BaseModel):
    player_ids: list[str]
    format: str = "round_robin"
    candidate_ids: list[str] = []
    games_per_pair: int = 2
    openings: str = "builtin"
    concurrency: int = 4
    wait_for_remote: bool = True
    game: GameConfigIn = GameConfigIn()
    seed: int = 0


class PlayerCreate(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    config: dict[str, Any] = {}
    id: Optional[str] = None
    anchor_elo: Optional[float] = None
    max_concurrent_games: Optional[int] = None
    preset: Optional[str] = None   # fill defaults from factory.PRESETS


class PlayerPatch(BaseModel):
    name: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    anchor_elo: Optional[float] = None
    max_concurrent_games: Optional[int] = None
    active: Optional[bool] = None


class TournamentCreate(BaseModel):
    name: str
    config: TournamentConfigIn
    start: bool = False


class GameCreate(BaseModel):
    white_id: str
    black_id: str
    config: Optional[GameConfigIn] = None
    opening_id: Optional[str] = None


# ------------------------------------------------------------------- routes
def build_api_router() -> APIRouter:
    api = APIRouter(prefix="/api")

    def st(request: Request):
        return request.app.state

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "stockfish": find_stockfish()}

    # -- players
    @api.get("/players")
    async def list_players(request: Request, include_inactive: bool = False) -> list[dict[str, Any]]:
        s = st(request)
        return [_player_out(p, s.hub) for p in s.db.list_players(include_inactive=include_inactive)]

    @api.get("/players/presets")
    async def presets() -> list[dict[str, Any]]:
        return get_presets()

    @api.post("/players", status_code=201)
    async def create_player(body: PlayerCreate, request: Request) -> dict[str, Any]:
        s = st(request)
        fields = body.model_dump(exclude={"preset"}, exclude_unset=True)
        if body.preset:
            preset = find_preset(body.preset)
            if preset is None:
                raise HTTPException(400, f"unknown preset {body.preset}")
            merged = {k: preset.get(k) for k in ("id", "name", "kind", "config", "anchor_elo",
                                                  "max_concurrent_games") if preset.get(k) is not None}
            merged.update(fields)
            fields = merged
        if not fields.get("name"):
            raise HTTPException(400, "name is required")
        if not fields.get("kind"):
            raise HTTPException(400, "kind is required")
        try:
            spec, token = register_player(s.db, **fields)
        except RegistryError as e:
            raise HTTPException(400, str(e)) from None
        out: dict[str, Any] = {"player": _player_out(spec, s.hub)}
        if token:
            out["token"] = token
        return out

    @api.get("/players/{player_id}")
    async def get_player(player_id: str, request: Request) -> dict[str, Any]:
        s = st(request)
        p = _player_or_404(s.db, player_id)
        stats: dict[str, Any] = {
            "games": s.db.count_games(player_id=player_id),
            "finished": s.db.count_games(player_id=player_id, status=GameStatus.FINISHED.value),
            "running": s.db.count_games(player_id=player_id, status=GameStatus.RUNNING.value),
        }
        stats.update(player_stats(s.db).get(player_id, {}))
        rows = await s.ratings.ratings(s.db, None, True)
        rating = next((row_to_dict(r) for r in rows if r.player_id == player_id), None)
        return {**_player_out(p, s.hub), "stats": stats, "rating": rating}

    @api.patch("/players/{player_id}")
    async def patch_player(player_id: str, body: PlayerPatch, request: Request) -> dict[str, Any]:
        s = st(request)
        p = _player_or_404(s.db, player_id)
        sent = body.model_fields_set
        if "name" in sent:
            if not body.name or not body.name.strip():
                raise HTTPException(400, "name must not be empty")
            p.name = body.name.strip()
        if "config" in sent:
            try:
                p.config = validate_config(p.kind, body.config or {})
            except RegistryError as e:
                raise HTTPException(400, str(e)) from None
        if "anchor_elo" in sent:
            p.anchor_elo = body.anchor_elo
        if "max_concurrent_games" in sent:
            if body.max_concurrent_games is None or body.max_concurrent_games < 1:
                raise HTTPException(400, "max_concurrent_games must be >= 1")
            p.max_concurrent_games = body.max_concurrent_games
        if "active" in sent and body.active is not None:
            p.active = body.active
        s.db.update_player(p)
        return _player_out(p, s.hub)

    @api.delete("/players/{player_id}")
    async def delete_player(player_id: str, request: Request) -> dict[str, Any]:
        s = st(request)
        _player_or_404(s.db, player_id)
        deleted = s.db.delete_player(player_id)
        return {"deleted": deleted, "deactivated": not deleted}

    @api.post("/players/{player_id}/token")
    async def new_player_token(player_id: str, request: Request) -> dict[str, Any]:
        s = st(request)
        p = _player_or_404(s.db, player_id)
        if p.kind != PlayerKind.REMOTE:
            raise HTTPException(400, "only remote players have tokens")
        if not p.active:
            raise HTTPException(400, "player is inactive")
        return {"token": rotate_token(s.db, player_id)}

    # -- tournaments
    def t_out(s: Any, t: Any) -> dict[str, Any]:
        return {**t.to_dict(), "progress": s.db.tournament_progress(t.id)}

    @api.get("/tournaments")
    async def list_tournaments(request: Request) -> list[dict[str, Any]]:
        s = st(request)
        return [t_out(s, t) for t in s.db.list_tournaments()]

    @api.post("/tournaments", status_code=201)
    async def create_tournament(body: TournamentCreate, request: Request) -> dict[str, Any]:
        s = st(request)
        if not body.name.strip():
            raise HTTPException(400, "name is required")
        try:
            cfg = TournamentConfig.from_dict(body.config.model_dump())
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"invalid tournament config: {e}") from None
        _validate_tournament_config(s.db, cfg)
        try:
            t = s.manager.create(body.name.strip(), cfg)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from None
        if body.start:
            await s.manager.start(t.id)
            t = s.db.get_tournament(t.id)
        return t_out(s, t)

    @api.get("/tournaments/{tournament_id}")
    async def get_tournament(tournament_id: str, request: Request) -> dict[str, Any]:
        from agentchess.rating import crosstable

        s = st(request)
        t = _tournament_or_404(s.db, tournament_id)
        results = s.db.rated_results(tournament_id)
        rep = await s.ratings.report(s.db, tournament_id, True, results=results)
        players = [p.to_dict() for pid in t.config.player_ids if (p := s.db.get_player(pid))]
        return {
            **t_out(s, t),
            "standings": [row_to_dict(r) for r in rep.rows],
            "superiority": rep.superiority,
            "crosstable": crosstable(results, t.config.player_ids),
            "players": players,
        }

    @api.post("/tournaments/{tournament_id}/analyse")
    async def analyse_tournament(tournament_id: str, request: Request) -> dict[str, Any]:
        """Queue engine analysis for every finished game of the tournament that lacks one."""
        s = st(request)
        _tournament_or_404(s.db, tournament_id)
        if not s.runtime.analysis.available:
            raise HTTPException(503, "engine analysis is not available (no Stockfish or analysis disabled)")
        return {"queued": s.runtime.analysis.enqueue_missing(tournament_id),
                "pending": s.runtime.analysis.pending()}

    async def _action(request: Request, tournament_id: str, action: str) -> dict[str, Any]:
        s = st(request)
        _tournament_or_404(s.db, tournament_id)
        try:
            await getattr(s.manager, action)(tournament_id)
        except KeyError as e:
            raise HTTPException(404, str(e)) from None
        except (ValueError, RuntimeError) as e:
            raise HTTPException(409, str(e)) from None
        return t_out(s, s.db.get_tournament(tournament_id))

    @api.post("/tournaments/{tournament_id}/start")
    async def start_tournament(tournament_id: str, request: Request) -> dict[str, Any]:
        return await _action(request, tournament_id, "start")

    @api.post("/tournaments/{tournament_id}/pause")
    async def pause_tournament(tournament_id: str, request: Request) -> dict[str, Any]:
        return await _action(request, tournament_id, "pause")

    @api.post("/tournaments/{tournament_id}/cancel")
    async def cancel_tournament(tournament_id: str, request: Request) -> dict[str, Any]:
        return await _action(request, tournament_id, "cancel")

    @api.post("/tournaments/{tournament_id}/retry-aborted")
    async def retry_aborted(tournament_id: str, request: Request) -> dict[str, Any]:
        """Replay games aborted by infrastructure failures or a cancel."""
        return await _action(request, tournament_id, "retry_aborted")

    @api.delete("/tournaments/{tournament_id}")
    async def delete_tournament(tournament_id: str, request: Request) -> dict[str, Any]:
        s = st(request)
        t = _tournament_or_404(s.db, tournament_id)
        running = s.db.count_games(tournament_id=tournament_id, status=GameStatus.RUNNING.value)
        if t.status == TournamentStatus.RUNNING or running:
            raise HTTPException(409, "tournament is running; pause or cancel it first")
        s.db.delete_tournament(tournament_id)
        return {"deleted": True}

    # -- games
    @api.get("/games")
    async def list_games(
        request: Request,
        tournament_id: Optional[str] = None,
        status: Optional[str] = None,
        player_id: Optional[str] = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        order: str = "seq",
    ) -> dict[str, Any]:
        s = st(request)
        if status is not None and status not in {x.value for x in GameStatus}:
            raise HTTPException(400, f"invalid status {status}")
        if order not in ("seq", "recent"):
            raise HTTPException(400, "order must be 'seq' or 'recent'")
        games = s.db.list_games(tournament_id=tournament_id, status=status, player_id=player_id,
                                limit=limit, offset=offset, order=order)
        names = _names(s.db)
        return {
            "games": [game_dict(g, names) for g in games],
            "total": s.db.count_games(tournament_id=tournament_id, status=status, player_id=player_id),
        }

    @api.post("/games", status_code=201)
    async def create_game(body: GameCreate, request: Request) -> dict[str, Any]:
        s = st(request)
        for pid in (body.white_id, body.black_id):
            p = s.db.get_player(pid)
            if p is None:
                raise HTTPException(400, f"unknown player {pid}")
            if not p.active:
                raise HTTPException(400, f"player {pid} is inactive")
        if body.white_id == body.black_id:
            raise HTTPException(400, "white and black must be different players")
        if body.opening_id:
            from agentchess.openings import BUILTIN_OPENINGS

            if not any(o.id == body.opening_id for o in BUILTIN_OPENINGS):
                raise HTTPException(400, f"unknown opening {body.opening_id}")
        cfg = GameConfig.from_dict(body.config.model_dump() if body.config else None)
        _validate_game_config(cfg)
        try:
            g = await s.manager.play_single(body.white_id, body.black_id, cfg, opening_id=body.opening_id)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from None
        return game_dict(g, _names(s.db), include_moves=True)

    @api.get("/games/{game_id}")
    async def get_game(game_id: str, request: Request) -> dict[str, Any]:
        s = st(request)
        g = s.db.get_game(game_id)
        if g is None:
            raise HTTPException(404, f"game {game_id} not found")
        return game_dict(g, _names(s.db), include_moves=True)

    @api.get("/games/{game_id}/analysis")
    async def get_game_analysis(game_id: str, request: Request) -> dict[str, Any]:
        """Stored engine analysis of a finished game (404 until it has been analysed)."""
        s = st(request)
        if s.db.get_game(game_id, include_moves=False) is None:
            raise HTTPException(404, f"game {game_id} not found")
        a = s.db.get_analysis(game_id)
        if a is None:
            raise HTTPException(404, "game not analysed yet")
        return a

    @api.post("/games/{game_id}/analysis")
    async def analyse_game(game_id: str, request: Request) -> dict[str, Any]:
        """Analyse a finished game now (replacing any stored analysis) and return it."""
        s = st(request)
        g = s.db.get_game(game_id, include_moves=False)
        if g is None:
            raise HTTPException(404, f"game {game_id} not found")
        if g.status != GameStatus.FINISHED:
            raise HTTPException(409, "only finished games can be analysed")
        svc = s.runtime.analysis
        if not svc.available:
            raise HTTPException(503, "engine analysis is not available (no Stockfish or analysis disabled)")
        try:
            a = await svc.analyse_now(game_id)
        except Exception as e:  # engine crashed / could not start
            log.exception("analysis of %s failed", game_id)
            raise HTTPException(500, f"analysis failed: {e}") from None
        if a is None:
            raise HTTPException(409, "game is not finished")
        return a

    @api.get("/analysis/status")
    async def analysis_status(request: Request) -> dict[str, Any]:
        svc = st(request).runtime.analysis
        return {"available": svc.available, "depth": svc.depth, "engine": svc.engine_path,
                "pending": svc.pending(), "auto": st(request).runtime.analysis_auto}

    @api.get("/games/{game_id}/pgn", response_class=PlainTextResponse)
    async def get_game_pgn(game_id: str, request: Request) -> PlainTextResponse:
        s = st(request)
        g = s.db.get_game(game_id)
        if g is None:
            raise HTTPException(404, f"game {game_id} not found")
        return PlainTextResponse(game_pgn(g, _names(s.db)) + "\n")

    @api.get("/pgn", response_class=PlainTextResponse)
    async def export_pgn(request: Request, tournament_id: Optional[str] = None) -> PlainTextResponse:
        s = st(request)
        if tournament_id:
            _tournament_or_404(s.db, tournament_id)
        text = export_pgn_text(s.db, tournament_id)
        fname = f"agentchess-{tournament_id or 'all'}.pgn"
        return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    # -- ratings / live / misc
    @api.get("/ratings")
    async def ratings(request: Request, tournament_id: Optional[str] = None, anchors: bool = True) -> dict[str, Any]:
        s = st(request)
        if tournament_id:
            _tournament_or_404(s.db, tournament_id)
        results = s.db.rated_results(tournament_id)
        rep = await s.ratings.report(s.db, tournament_id, anchors, results=results)
        return {"ratings": [row_to_dict(r) for r in rep.rows], "superiority": rep.superiority,
                "stats": player_stats(s.db, tournament_id), "games": len(results)}

    @api.get("/live")
    async def live(request: Request) -> dict[str, Any]:
        s = st(request)
        names = _names(s.db)
        games = [g for gid in s.manager.running_game_ids() if (g := s.db.get_game(gid))]
        return {"games": [game_dict(g, names, include_moves=True) for g in games]}

    @api.get("/openings")
    async def openings() -> list[dict[str, Any]]:
        from agentchess.openings import BUILTIN_OPENINGS

        return [o.to_dict() for o in BUILTIN_OPENINGS]

    @api.get("/agents")
    async def agents(request: Request) -> list[dict[str, Any]]:
        return st(request).hub.status()

    return api


def export_pgn_text(db: Database, tournament_id: Optional[str] = None) -> str:
    """All finished games (optionally of one tournament) as one PGN document."""
    names = _names(db)
    out = io.StringIO()
    offset = 0
    while True:
        batch = db.list_games(tournament_id=tournament_id, status=GameStatus.FINISHED, limit=500, offset=offset)
        for g in batch:
            if not g.pgn:
                g.moves = db.get_moves(g.id)
            out.write(game_pgn(g, names).strip() + "\n\n")
        if len(batch) < 500:
            break
        offset += 500
    return out.getvalue()


async def events_ws(ws: WebSocket) -> None:
    """GUI event stream: hello, then every EventBus event as JSON."""
    bus: EventBus = ws.app.state.bus
    await ws.accept()
    q = bus.subscribe()

    async def reader() -> None:
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            while True:
                msg = await ws.receive_text()
                if msg.strip() in ('{"type":"ping"}', '{"type": "ping"}', "ping"):
                    await ws.send_text('{"type":"pong"}')

    reader_task = asyncio.create_task(reader())
    try:
        await ws.send_text(json.dumps({"type": "hello", "version": __version__}))
        while not reader_task.done():
            getter = asyncio.create_task(q.get())
            done, _ = await asyncio.wait({getter, reader_task}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                await ws.send_text(json.dumps(getter.result(), default=str))
            else:
                getter.cancel()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        bus.unsubscribe(q)
        reader_task.cancel()
        with contextlib.suppress(BaseException):
            await reader_task


PLACEHOLDER_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>agentchess</title>
<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;
background:#fff;color:#222}@media (prefers-color-scheme:dark){body{background:#111;color:#ddd}}
a{color:#4a8}</style></head>
<body><h1>agentchess</h1><p>The server is running, but the web GUI files were not found.</p>
<p>API: <a href="/api/health">/api/health</a> &middot; <a href="/docs">/docs</a></p></body></html>"""


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt = Runtime.open(settings.db_path, seed=settings.seed, analysis_depth=settings.analysis_depth,
                          analysis_auto=settings.analysis_auto)
        app.state.runtime = rt
        app.state.db = rt.db
        app.state.bus = rt.bus
        app.state.hub = rt.hub
        app.state.ctx = rt.ctx
        app.state.manager = rt.manager
        app.state.ratings = RatingService(settings.bootstrap)
        await rt.start(resume=settings.resume)
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="agentchess", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                           allow_methods=["*"], allow_headers=["*"])
    app.include_router(build_api_router())
    app.include_router(agent_api.router)
    app.add_api_websocket_route("/ws", events_ws)

    web = Path(settings.web_dir)
    if (web / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(web), html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def placeholder() -> str:
            return PLACEHOLDER_HTML
    return app
