"""Tournament scheduling (Berger / circle method) and the async tournament manager.

``schedule_games`` turns a :class:`Tournament` into an ordered list of
SCHEDULED :class:`GameRecord` objects. :class:`TournamentManager` persists
tournaments, runs one scheduler loop per RUNNING tournament and enforces
concurrency limits (per tournament and, globally, per player).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import random
from typing import Any, Awaitable, Callable, Optional

from agentchess.db import Database
from agentchess.events import EventBus
from agentchess.models import (
    GameConfig,
    GameRecord,
    GameStatus,
    Opening,
    PlayerKind,
    PlayerSpec,
    Termination,
    Tournament,
    TournamentConfig,
    TournamentStatus,
    new_id,
    now,
)
from agentchess.players.base import Player, PlayerContext

log = logging.getLogger(__name__)

GameRunner = Callable[..., Awaitable[GameRecord]]
PlayerFactory = Callable[[PlayerSpec, PlayerContext], Any]

FORMATS = ("round_robin", "gauntlet")


# ----------------------------------------------------------------------------- scheduling
def _berger_rounds(n: int) -> list[list[tuple[int, int]]]:
    """Circle-method rounds over players 0..n-1 as (white, black) index pairs; a bye is
    added when n is odd. Every pair meets exactly once; each player plays ≤ 1 game per round."""
    m = n + (n % 2)
    arr = list(range(m))
    rounds: list[list[tuple[int, int]]] = []
    for r in range(m - 1):
        games = []
        for i in range(m // 2):
            a, b = arr[i], arr[m - 1 - i]
            if i == 0 and r % 2 == 1:   # fixed player alternates colours; gives optimal balance
                a, b = b, a
            if a < n and b < n:
                games.append((a, b))
        rounds.append(games)
        arr = [arr[0], arr[-1]] + arr[1:-1]
    return rounds


def _validate(cfg: TournamentConfig, players: dict[str, PlayerSpec]) -> list[str]:
    ids = list(dict.fromkeys(cfg.player_ids))
    if len(ids) != len(cfg.player_ids):
        raise ValueError("duplicate player ids in tournament")
    if len(ids) < 2:
        raise ValueError("a tournament needs at least 2 players")
    for pid in ids:
        spec = players.get(pid)
        if spec is None:
            raise ValueError(f"unknown player: {pid}")
        if not spec.active:
            raise ValueError(f"player is inactive: {pid}")
    if cfg.format not in FORMATS:
        raise ValueError(f"unknown tournament format: {cfg.format!r} (expected one of {FORMATS})")
    if cfg.format == "gauntlet":
        if not cfg.candidate_ids:
            raise ValueError("gauntlet needs at least one candidate")
        missing = [c for c in cfg.candidate_ids if c not in ids]
        if missing:
            raise ValueError(f"candidates not among players: {missing}")
    if cfg.games_per_pair < 1:
        raise ValueError("games_per_pair must be >= 1")
    if cfg.openings not in ("builtin", "none"):
        raise ValueError(f"unknown openings option: {cfg.openings!r}")
    return ids


def schedule_games(t: Tournament, players: dict[str, PlayerSpec]) -> list[GameRecord]:
    """All games of ``t`` in play order (round 1 first). Raises ValueError on invalid config.

    Each pair plays ``games_per_pair`` games in successive cycles with colours alternating;
    with builtin openings, games 2i and 2i+1 of a pair share opening i (colours reversed).
    """
    cfg = t.config
    ids = _validate(cfg, players)
    n = len(ids)
    rounds = _berger_rounds(n)
    if cfg.format == "gauntlet":
        cands = {ids.index(c) for c in cfg.candidate_ids}
        rounds = [[p for p in rnd if p[0] in cands or p[1] in cands] for rnd in rounds]
        rounds = [rnd for rnd in rounds if rnd]

    # Deterministic opening assignment: pair k uses openings shuffled[(k*m + i) % L], i < m.
    per_pair = (cfg.games_per_pair + 1) // 2
    pair_order = {frozenset(p): k for k, p in enumerate(sorted(tuple(sorted(p)) for rnd in rounds for p in rnd))}
    book: list[Opening] = []
    if cfg.openings == "builtin":
        from agentchess.openings import BUILTIN_OPENINGS  # lazy: written by another module
        book = list(BUILTIN_OPENINGS)
        random.Random(cfg.seed).shuffle(book)

    created = now()
    games: list[GameRecord] = []
    n_rounds = len(rounds)
    for cycle in range(cfg.games_per_pair):
        for r, rnd in enumerate(rounds):
            for a, b in rnd:
                w, bl = (a, b) if cycle % 2 == 0 else (b, a)
                opening = None
                if book:
                    o = book[(pair_order[frozenset((a, b))] * per_pair + cycle // 2) % len(book)]
                    opening = Opening(id=o.id, name=o.name, eco=o.eco, moves_uci=list(o.moves_uci))
                games.append(GameRecord(
                    id=new_id("g"),
                    white_id=ids[w],
                    black_id=ids[bl],
                    tournament_id=t.id,
                    round=cycle * n_rounds + r + 1,
                    opening=opening,
                    config=GameConfig.from_dict(cfg.game.to_dict()),
                    created_at=created,
                ))
    return games


# ----------------------------------------------------------------------------- manager
class TournamentManager:
    """Owns all running games (tournament and ad-hoc) of one process.

    ``game_runner`` / ``player_factory`` default to ``agentchess.game.play_game`` and
    ``agentchess.players.factory.create_player`` (imported lazily); tests inject fakes.
    """

    def __init__(self, db: Database, bus: EventBus, ctx: PlayerContext, *,
                 game_runner: Optional[GameRunner] = None,
                 player_factory: Optional[PlayerFactory] = None,
                 tick_s: float = 1.0) -> None:
        self.db = db
        self.bus = bus
        self.ctx = ctx
        self._game_runner = game_runner
        self._player_factory = player_factory
        self.tick_s = tick_s
        self._runners: dict[str, asyncio.Task] = {}          # tournament_id -> scheduler loop
        self._wake: dict[str, asyncio.Event] = {}
        self._games: dict[str, asyncio.Task] = {}            # game_id -> game task
        self._game_tournament: dict[str, Optional[str]] = {}
        self._load: dict[str, int] = {}                      # player_id -> running games

    # ------------------------------------------------------------ collaborators
    def _runner(self) -> GameRunner:
        if self._game_runner is None:
            from agentchess.game import play_game
            self._game_runner = play_game
        return self._game_runner

    def _factory(self) -> PlayerFactory:
        if self._player_factory is None:
            from agentchess.players.factory import create_player
            self._player_factory = create_player
        return self._player_factory

    # ------------------------------------------------------------ helpers
    def _get(self, tournament_id: str) -> Tournament:
        t = self.db.get_tournament(tournament_id)
        if t is None:
            raise KeyError(f"unknown tournament: {tournament_id}")
        return t

    def _publish_tournament(self, tournament_id: str) -> None:
        t = self.db.get_tournament(tournament_id)
        if t is not None:
            self.bus.publish({"type": "tournament_updated", "tournament": t.to_dict(),
                              "progress": self.db.tournament_progress(tournament_id)})

    def _wake_all(self) -> None:
        for ev in self._wake.values():
            ev.set()

    def _tournament_tasks(self, tournament_id: Optional[str]) -> dict[str, asyncio.Task]:
        return {gid: task for gid, task in self._games.items() if self._game_tournament.get(gid) == tournament_id}

    def running_game_ids(self) -> list[str]:
        return list(self._games)

    def player_load(self, player_id: str) -> int:
        """Games currently running for ``player_id`` (across tournaments and ad-hoc games)."""
        return self._load.get(player_id, 0)

    # ------------------------------------------------------------ lifecycle API
    def create(self, name: str, config: TournamentConfig) -> Tournament:
        """Validate, schedule and persist a tournament (status PENDING) with all its games."""
        players = {p.id: p for p in self.db.list_players(include_inactive=True)}
        t = Tournament(id=new_id("t"), name=name, config=config)
        games = schedule_games(t, players)
        self.db.add_tournament(t)
        try:
            self.db.add_games(games)   # single transaction
        except Exception:
            self.db.delete_tournament(t.id)
            raise
        self._publish_tournament(t.id)
        return t

    async def start(self, tournament_id: str) -> None:
        """PENDING/PAUSED -> RUNNING and ensure a scheduler loop is running."""
        t = self._get(tournament_id)
        if t.status in (TournamentStatus.FINISHED, TournamentStatus.CANCELLED):
            raise ValueError(f"tournament is {t.status.value}")
        if t.status != TournamentStatus.RUNNING:
            t.status = TournamentStatus.RUNNING
            t.started_at = t.started_at or now()
            self.db.update_tournament(t)
            self._publish_tournament(tournament_id)
        self._ensure_loop(tournament_id)

    async def retry_aborted(self, tournament_id: str) -> int:
        """Reschedule the tournament's ABORTED games (e.g. after a provider outage or a
        cancel) and run them. Reopens a FINISHED/CANCELLED tournament. Returns the count."""
        t = self._get(tournament_id)
        n = self.db.reschedule_aborted(tournament_id)
        if n == 0:
            return 0
        if t.status in (TournamentStatus.FINISHED, TournamentStatus.CANCELLED):
            t.status = TournamentStatus.PAUSED
            t.finished_at = None
            self.db.update_tournament(t)
        await self.start(tournament_id)
        return n

    async def pause(self, tournament_id: str) -> None:
        """Stop launching new games; games already running finish normally."""
        t = self._get(tournament_id)
        if t.status in (TournamentStatus.RUNNING, TournamentStatus.PENDING):
            t.status = TournamentStatus.PAUSED
            self.db.update_tournament(t)
            self._publish_tournament(tournament_id)
        ev = self._wake.get(tournament_id)
        if ev:
            ev.set()

    async def cancel(self, tournament_id: str) -> None:
        """Abort running games and all remaining scheduled games; status CANCELLED."""
        t = self._get(tournament_id)
        if t.status in (TournamentStatus.FINISHED, TournamentStatus.CANCELLED):
            return
        t.status = TournamentStatus.CANCELLED
        t.finished_at = now()
        self.db.update_tournament(t)
        runner = self._runners.pop(tournament_id, None)
        if runner and not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        self._wake.pop(tournament_id, None)
        tasks = self._tournament_tasks(tournament_id)
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        for gid in tasks:
            g = self.db.get_game(gid, include_moves=False)
            if g is not None and g.status in (GameStatus.RUNNING, GameStatus.SCHEDULED):
                self._set_aborted(g, "tournament cancelled")
                self.bus.publish({"type": "game_finished", "game": g.to_dict(include_moves=False)})
        with self.db.transaction():
            for g in self.db.list_games(tournament_id=tournament_id, status=GameStatus.SCHEDULED, limit=10**9):
                self._set_aborted(g, "tournament cancelled")
        self._publish_tournament(tournament_id)

    async def resume_all(self) -> None:
        """Startup: reset games interrupted by a crash, then restart RUNNING tournaments."""
        recovered = self.db.recover_interrupted_games()
        if recovered:
            log.info("recovered %d interrupted games", len(recovered))
        for t in self.db.list_tournaments(TournamentStatus.RUNNING):
            self._ensure_loop(t.id)

    async def shutdown(self) -> None:
        """Stop everything. Running tournament games go back to SCHEDULED (moves cleared) and
        tournaments keep their status so :meth:`resume_all` picks them up; ad-hoc games are ABORTED."""
        runners = list(self._runners.values())
        self._runners.clear()
        for r in runners:
            r.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        tasks = dict(self._games)
        owners = {gid: self._game_tournament.get(gid) for gid in tasks}
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        for gid, tid in owners.items():
            g = self.db.get_game(gid, include_moves=False)
            if g is None or g.status not in (GameStatus.RUNNING, GameStatus.SCHEDULED):
                continue
            if tid:
                self.db.clear_moves(gid)
                g.status, g.started_at, g.pgn = GameStatus.SCHEDULED, None, None
                g.result = g.termination = g.termination_detail = None
                self.db.update_game(g)
            else:
                self._set_aborted(g, "server shutdown")
        self._wake.clear()

    async def play_single(self, white_id: str, black_id: str, config: Optional[GameConfig] = None,
                          opening_id: Optional[str] = None) -> GameRecord:
        """Start an ad-hoc exhibition game in the background; returns the scheduled record."""
        if white_id == black_id:
            raise ValueError("a player cannot play itself")
        for pid in (white_id, black_id):
            if self.db.get_player(pid) is None:
                raise ValueError(f"unknown player: {pid}")
        opening = None
        if opening_id:
            from agentchess.openings import BUILTIN_OPENINGS
            match = [o for o in BUILTIN_OPENINGS if o.id == opening_id]
            if not match:
                raise ValueError(f"unknown opening: {opening_id}")
            o = match[0]
            opening = Opening(id=o.id, name=o.name, eco=o.eco, moves_uci=list(o.moves_uci))
        g = GameRecord(id=new_id("g"), white_id=white_id, black_id=black_id, opening=opening,
                       config=config or GameConfig())
        self.db.add_game(g)
        self._launch(g, None)
        return g

    async def wait_idle(self, tournament_id: Optional[str] = None) -> None:
        """Await the scheduler loop of ``tournament_id`` and its in-flight games
        (``None``: every loop and every game, including ad-hoc ones)."""
        while True:
            if tournament_id is None:
                pending = list(self._runners.values()) + list(self._games.values())
            else:
                runner = self._runners.get(tournament_id)
                pending = ([runner] if runner else []) + list(self._tournament_tasks(tournament_id).values())
            pending = [p for p in pending if not p.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def standings(self, tournament_id: str, *, bootstrap: int = 200, use_anchors: bool = True) -> list:
        """Ratings (``RatingRow`` list) over this tournament's finished games."""
        from agentchess.rating import compute_ratings
        return compute_ratings(self.db.rated_results(tournament_id), self.db.list_players(include_inactive=True),
                               use_anchors=use_anchors, bootstrap=bootstrap)

    # ------------------------------------------------------------ scheduler loop
    def _ensure_loop(self, tournament_id: str) -> None:
        task = self._runners.get(tournament_id)
        if task is not None and not task.done():
            self._wake[tournament_id].set()
            return
        self._wake[tournament_id] = asyncio.Event()
        self._runners[tournament_id] = asyncio.create_task(self._loop(tournament_id),
                                                           name=f"tournament-{tournament_id}")

    async def _loop(self, tournament_id: str) -> None:
        ev = self._wake[tournament_id]
        try:
            while True:
                ev.clear()
                try:
                    t = self.db.get_tournament(tournament_id)
                    if t is None or t.status != TournamentStatus.RUNNING:
                        return
                    self._launch_ready(t)
                    if not self._tournament_tasks(tournament_id) and \
                            self.db.count_games(tournament_id=tournament_id, status=GameStatus.SCHEDULED.value) == 0:
                        t.status = TournamentStatus.FINISHED
                        t.finished_at = now()
                        self.db.update_tournament(t)
                        self._publish_tournament(tournament_id)
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:  # never let the loop die on a transient error
                    log.exception("tournament %s scheduler iteration failed", tournament_id)
                try:
                    await asyncio.wait_for(ev.wait(), self.tick_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            if self._runners.get(tournament_id) is asyncio.current_task():
                self._runners.pop(tournament_id, None)

    def _launch_ready(self, t: Tournament) -> None:
        running = len(self._tournament_tasks(t.id))
        limit = max(1, t.config.concurrency)
        if running >= limit:
            return
        specs = {p.id: p for p in self.db.list_players(include_inactive=True)}
        for g in self.db.list_games(tournament_id=t.id, status=GameStatus.SCHEDULED, limit=10**9):
            if running >= limit:
                break
            if g.id in self._games or not self._can_start(g, specs, t.config.wait_for_remote):
                continue
            self._launch(g, t.id)
            running += 1

    def _can_start(self, g: GameRecord, specs: dict[str, PlayerSpec], wait_for_remote: bool) -> bool:
        for pid in (g.white_id, g.black_id):
            spec = specs.get(pid)
            cap = max(1, spec.max_concurrent_games) if spec else 1
            if self._load.get(pid, 0) >= cap:
                return False
            if wait_for_remote and spec is not None and spec.kind == PlayerKind.REMOTE:
                hub = self.ctx.agent_hub
                if hub is not None and not hub.is_online(pid):
                    return False
        return True

    def _launch(self, g: GameRecord, tournament_id: Optional[str]) -> None:
        for pid in (g.white_id, g.black_id):
            self._load[pid] = self._load.get(pid, 0) + 1
        self._game_tournament[g.id] = tournament_id
        task = asyncio.create_task(self._run_game(g, tournament_id), name=f"game-{g.id}")
        self._games[g.id] = task
        # Release bookkeeping in a done-callback, not in _run_game's ``finally``: a task cancelled
        # before its first step (e.g. shutdown right after play_single) never runs its body, which
        # would leak the per-player load and the _games entry (tournament then never finishes).
        task.add_done_callback(lambda _t, g=g, tid=tournament_id: self._release(g, tid))

    def _release(self, g: GameRecord, tournament_id: Optional[str]) -> None:
        self._games.pop(g.id, None)
        self._game_tournament.pop(g.id, None)
        for pid in (g.white_id, g.black_id):
            self._load[pid] = max(0, self._load.get(pid, 0) - 1)
        self._wake_all()
        if tournament_id:
            try:
                self._publish_tournament(tournament_id)
            except Exception:
                log.exception("publish failed")

    # ------------------------------------------------------------ game execution
    async def _make_player(self, spec: Optional[PlayerSpec]) -> Player:
        if spec is None:
            raise ValueError("player not found")
        p = self._factory()(spec, self.ctx)
        if inspect.isawaitable(p):
            p = await p
        return p

    async def _run_game(self, g: GameRecord, tournament_id: Optional[str]) -> None:
        # Bookkeeping (load, _games) is released by the task's done-callback (see _launch).
        specs = {pid: self.db.get_player(pid) for pid in (g.white_id, g.black_id)}
        players: dict[str, Optional[Player]] = {}
        errors: dict[str, str] = {}
        for pid in (g.white_id, g.black_id):
            try:
                players[pid] = await self._make_player(specs[pid])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("could not create player %s for game %s: %s", pid, g.id, e)
                players[pid] = None
                errors[pid] = f"{type(e).__name__}: {e}"
        if errors:
            for p in players.values():
                if p is not None:
                    try:
                        await p.close()
                    except Exception:
                        log.exception("error closing player")
            self._forfeit_setup(g, errors)
            return
        names = {pid: (s.name if s else pid) for pid, s in specs.items()}
        try:
            rec = await self._runner()(g, players[g.white_id], players[g.black_id],
                                       db=self.db, bus=self.bus, names=names)
            if isinstance(rec, GameRecord) and rec.status in (GameStatus.FINISHED, GameStatus.ABORTED):
                self.db.update_game(rec)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("game %s crashed", g.id)
            self._mark_crashed(g, e)

    def _finish(self, g: GameRecord, result: Optional[str], termination: Termination, detail: str) -> None:
        g.status = GameStatus.FINISHED if result else GameStatus.ABORTED
        g.result = result
        g.termination = termination if result else Termination.ABORTED
        g.termination_detail = detail
        g.started_at = g.started_at or now()
        g.finished_at = now()
        self.db.update_game(g)
        self.bus.publish({"type": "game_finished", "game": g.to_dict(include_moves=False)})

    def _set_aborted(self, g: GameRecord, detail: str) -> None:
        g.status, g.termination, g.termination_detail = GameStatus.ABORTED, Termination.ABORTED, detail
        g.result = None
        g.finished_at = now()
        self.db.update_game(g)

    def _forfeit_setup(self, g: GameRecord, errors: dict[str, str]) -> None:
        """Player construction failed: the failing side forfeits (ERROR); both failing -> ABORTED."""
        if len(errors) == 2:
            self._finish(g, None, Termination.ABORTED, "both players failed to start: " + "; ".join(
                f"{pid}: {e}" for pid, e in errors.items()))
            return
        (pid, err), = errors.items()
        result = "0-1" if pid == g.white_id else "1-0"
        self._finish(g, result, Termination.ERROR, f"player {pid} failed to start: {err}")

    def _mark_crashed(self, g: GameRecord, exc: BaseException) -> None:
        """The runner raised. Forfeit the side at fault if the exception names it
        (``player_id`` or ``color`` attribute), else ABORT the game."""
        current = self.db.get_game(g.id, include_moves=False)
        if current is not None and current.status in (GameStatus.FINISHED, GameStatus.ABORTED):
            return
        g = current or g
        detail = f"runner error: {type(exc).__name__}: {exc}"
        culprit = getattr(exc, "player_id", None)
        color = getattr(exc, "color", None)
        if culprit == g.white_id or color == "white":
            self._finish(g, "0-1", Termination.ERROR, detail)
        elif culprit == g.black_id or color == "black":
            self._finish(g, "1-0", Termination.ERROR, detail)
        else:
            self._finish(g, None, Termination.ABORTED, detail)
