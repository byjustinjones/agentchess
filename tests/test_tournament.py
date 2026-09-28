import asyncio
from collections import Counter, defaultdict
from typing import Optional

import pytest

from agentchess.db import Database
from agentchess.events import EventBus
from agentchess.models import (
    GameConfig,
    GameRecord,
    GameStatus,
    MoveRecord,
    MoveRequest,
    MoveResponse,
    PlayerKind,
    PlayerSpec,
    Termination,
    Tournament,
    TournamentConfig,
    TournamentStatus,
    now,
)
from agentchess.players.base import Player, PlayerContext
from agentchess.tournament import TournamentManager, schedule_games


# ----------------------------------------------------------------------------- helpers
def specs(ids, kind=PlayerKind.ENGINE, cap=4) -> dict[str, PlayerSpec]:
    return {pid: PlayerSpec(id=pid, name=pid.upper(), kind=kind, max_concurrent_games=cap) for pid in ids}


def tourney(ids, **kw) -> Tournament:
    kw.setdefault("openings", "none")
    return Tournament(id="t1", name="T", config=TournamentConfig(player_ids=list(ids), **kw))


def pair(g: GameRecord) -> frozenset:
    return frozenset((g.white_id, g.black_id))


class FakePlayer(Player):
    async def get_move(self, request: MoveRequest) -> MoveResponse:
        return MoveResponse(move="")


def fake_factory(fail: tuple[str, ...] = ()):
    def create(spec: PlayerSpec, ctx: PlayerContext) -> Player:
        if spec.id in fail:
            raise RuntimeError(f"cannot build {spec.id}")
        return FakePlayer(spec)
    return create


class FakeRunner:
    """Mimics play_game: persists RUNNING, one move, then FINISHED. Tracks concurrency."""

    def __init__(self, delay: float = 0.01, gate: Optional[asyncio.Event] = None, fail_ids=(), culprit=None):
        self.delay = delay
        self.gate = gate
        self.fail_ids = set(fail_ids)
        self.culprit = culprit
        self.running = 0
        self.max_running = 0
        self.per_player: Counter = Counter()
        self.max_per_player: Counter = Counter()
        self.started: list[str] = []
        self.closed = 0

    async def __call__(self, game: GameRecord, white: Player, black: Player, db=None, bus=None, names=None):
        assert names and set(names) == {game.white_id, game.black_id}
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        for pid in (game.white_id, game.black_id):
            self.per_player[pid] += 1
            self.max_per_player[pid] = max(self.max_per_player[pid], self.per_player[pid])
        self.started.append(game.id)
        try:
            game.status, game.started_at = GameStatus.RUNNING, now()
            db.update_game(game)
            db.add_move(game.id, game.white_id, MoveRecord(ply=0, color="white", uci="e2e4", san="e4",
                                                            fen_after="x", elapsed_s=0, total_elapsed_s=0))
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(self.delay)
            if game.id in self.fail_ids:
                exc = RuntimeError("boom")
                if self.culprit:
                    exc.player_id = game.white_id  # type: ignore[attr-defined]
                raise exc
            game.status, game.finished_at = GameStatus.FINISHED, now()
            game.result = "1-0" if game.white_id < game.black_id else "1/2-1/2"
            game.termination = Termination.CHECKMATE
            db.update_game(game)
            return game
        finally:
            self.running -= 1
            for pid in (game.white_id, game.black_id):
                self.per_player[pid] -= 1
            await white.close()
            await black.close()


class FakeHub:
    def __init__(self, online=()):
        self.online = set(online)

    def is_online(self, player_id: str, within_s: float = 60) -> bool:
        return player_id in self.online


def make_db(path=":memory:", ids=("a", "b", "c", "d"), cap=4, kinds=None) -> Database:
    db = Database(path)
    for pid, sp in specs(ids, cap=cap).items():
        if kinds and pid in kinds:
            sp.kind = kinds[pid]
        db.add_player(sp)
    return db


def manager(db, runner, factory=None, hub=None, bus=None) -> TournamentManager:
    return TournamentManager(db, bus or EventBus(), PlayerContext(agent_hub=hub),
                             game_runner=runner, player_factory=factory or fake_factory(), tick_s=0.02)


async def until(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


def statuses(db, tid) -> Counter:
    return Counter(g.status for g in db.list_games(tournament_id=tid, limit=10**6))


# ----------------------------------------------------------------------------- scheduling
@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 10, 11])
@pytest.mark.parametrize("gpp", [1, 2, 4])
def test_round_robin_schedule(n, gpp):
    ids = [f"p{i}" for i in range(n)]
    games = schedule_games(tourney(ids, games_per_pair=gpp), specs(ids))
    per_pair = Counter(pair(g) for g in games)
    assert len(per_pair) == n * (n - 1) // 2
    assert set(per_pair.values()) == {gpp}
    whites = Counter((g.white_id, g.black_id) for g in games)
    if gpp % 2 == 0:
        for g in games:
            assert whites[(g.white_id, g.black_id)] == gpp // 2
    by_round = defaultdict(list)
    for g in games:
        by_round[g.round].append(g)
        assert g.status == GameStatus.SCHEDULED and g.tournament_id == "t1" and g.opening is None
    for rnd in by_round.values():
        ps = [p for g in rnd for p in (g.white_id, g.black_id)]
        assert len(ps) == len(set(ps))
    assert [g.round for g in games] == sorted(g.round for g in games)
    assert min(by_round) == 1
    assert len({g.id for g in games}) == len(games)


@pytest.mark.parametrize("n", range(2, 13))
def test_single_round_robin_colour_balance(n):
    ids = [f"p{i}" for i in range(n)]
    games = schedule_games(tourney(ids, games_per_pair=1), specs(ids))
    whites = Counter(g.white_id for g in games)
    # optimal: equal colours for odd n (even game count), off by one for even n
    assert all(abs(whites[p] - (n - 1 - whites[p])) <= (n - 1) % 2 for p in ids)


def test_gauntlet_schedule():
    ids = ["a", "b", "x", "y", "z"]
    games = schedule_games(tourney(ids, format="gauntlet", candidate_ids=["a", "b"], games_per_pair=2), specs(ids))
    per_pair = Counter(pair(g) for g in games)
    expected = {frozenset(p) for p in [("a", "b"), ("a", "x"), ("a", "y"), ("a", "z"),
                                       ("b", "x"), ("b", "y"), ("b", "z")]}
    assert set(per_pair) == expected
    assert set(per_pair.values()) == {2}
    rounds = defaultdict(list)
    for g in games:
        rounds[g.round] += [g.white_id, g.black_id]
    assert all(len(v) == len(set(v)) for v in rounds.values())
    assert sorted(rounds) == list(range(1, len(rounds) + 1))   # no empty rounds


def test_openings_deterministic_and_paired():
    from agentchess.openings import BUILTIN_OPENINGS
    ids = ["a", "b", "c", "d"]
    t = tourney(ids, games_per_pair=6, openings="builtin", seed=5)
    g1 = schedule_games(t, specs(ids))
    g2 = schedule_games(t, specs(ids))
    assert [(g.white_id, g.black_id, g.opening.id) for g in g1] == [(g.white_id, g.black_id, g.opening.id) for g in g2]
    valid = {o.id for o in BUILTIN_OPENINGS}
    by_pair = defaultdict(list)
    for g in g1:
        assert g.opening.id in valid and g.opening.moves_uci
        by_pair[pair(g)].append(g)
    for games in by_pair.values():
        assert len(games) == 6
        for i in range(3):
            x, y = games[2 * i], games[2 * i + 1]
            assert x.opening.id == y.opening.id
            assert (x.white_id, x.black_id) == (y.black_id, y.white_id)
        if len(BUILTIN_OPENINGS) >= 3:
            assert len({g.opening.id for g in games}) == 3
    other = schedule_games(tourney(ids, games_per_pair=6, openings="builtin", seed=6), specs(ids))
    assert [g.opening.id for g in other] != [g.opening.id for g in g1]


def test_schedule_validation():
    sp = specs(["a", "b", "c"])
    with pytest.raises(ValueError):
        schedule_games(tourney(["a"]), sp)
    with pytest.raises(ValueError):
        schedule_games(tourney(["a", "zz"]), sp)
    with pytest.raises(ValueError):
        schedule_games(tourney(["a", "b"], games_per_pair=0), sp)
    with pytest.raises(ValueError):
        schedule_games(tourney(["a", "b"], format="gauntlet", candidate_ids=["c"]), sp)
    with pytest.raises(ValueError):
        schedule_games(tourney(["a", "b"], format="swiss"), sp)
    sp["b"].active = False
    with pytest.raises(ValueError):
        schedule_games(tourney(["a", "b"]), sp)


# ----------------------------------------------------------------------------- manager
async def test_full_tournament_completes_with_limits():
    db = make_db(ids=("a", "b", "c", "d", "e", "f"), cap=2)
    bus = EventBus()
    q = bus.subscribe()
    runner = FakeRunner(delay=0.01)
    m = manager(db, runner, bus=bus)
    t = m.create("rr", TournamentConfig(player_ids=list("abcdef"), games_per_pair=2, concurrency=3, openings="none"))
    assert db.get_tournament(t.id).status == TournamentStatus.PENDING
    assert db.tournament_progress(t.id)["scheduled"] == 30
    await m.start(t.id)
    await asyncio.wait_for(m.wait_idle(t.id), 10)
    assert db.get_tournament(t.id).status == TournamentStatus.FINISHED
    assert statuses(db, t.id) == Counter({GameStatus.FINISHED: 30})
    assert runner.max_running <= 3 and runner.max_running >= 2
    assert max(runner.max_per_player.values()) <= 2
    assert m.running_game_ids() == [] and all(m.player_load(p) == 0 for p in "abcdef")
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    tev = [e for e in events if e["type"] == "tournament_updated"]
    assert tev[-1]["tournament"]["status"] == "finished" and tev[-1]["progress"]["finished"] == 30
    rows = m.standings(t.id, bootstrap=10)
    assert {r.player_id for r in rows} == set("abcdef")


async def test_per_player_capacity_across_tournaments():
    db = make_db(ids=("a", "b", "c", "d", "e"), cap=1)
    runner = FakeRunner(delay=0.01)
    m = manager(db, runner)
    t1 = m.create("t1", TournamentConfig(player_ids=list("abcde"), games_per_pair=2, concurrency=4, openings="none"))
    t2 = m.create("t2", TournamentConfig(player_ids=list("abcde"), games_per_pair=2, concurrency=4, openings="none"))
    await m.start(t1.id)
    await m.start(t2.id)
    single = await m.play_single("a", "b", GameConfig())
    await asyncio.wait_for(m.wait_idle(), 10)
    assert max(runner.max_per_player.values()) <= 2   # ad-hoc game may exceed capacity once
    for t in (t1, t2):
        assert db.get_tournament(t.id).status == TournamentStatus.FINISHED
    assert db.get_game(single.id).status == GameStatus.FINISHED


async def test_capacity_strict_without_adhoc():
    db = make_db(ids=("a", "b", "c", "d"), cap=1)
    runner = FakeRunner(delay=0.01)
    m = manager(db, runner)
    ts = [m.create(f"t{i}", TournamentConfig(player_ids=list("abcd"), concurrency=4, openings="none")) for i in range(3)]
    for t in ts:
        await m.start(t.id)
    await asyncio.wait_for(m.wait_idle(), 10)
    assert max(runner.max_per_player.values()) == 1
    assert runner.max_running <= 2


async def test_pause_and_resume():
    db = make_db()
    gate = asyncio.Event()
    runner = FakeRunner(delay=0, gate=gate)
    m = manager(db, runner)
    t = m.create("p", TournamentConfig(player_ids=list("abcd"), concurrency=1, openings="none"))
    await m.start(t.id)
    await until(lambda: len(runner.started) == 1)
    await m.pause(t.id)
    assert db.get_tournament(t.id).status == TournamentStatus.PAUSED
    gate.set()
    await asyncio.wait_for(m.wait_idle(t.id), 5)
    await asyncio.sleep(0.1)
    assert len(runner.started) == 1
    assert statuses(db, t.id)[GameStatus.FINISHED] == 1
    await m.start(t.id)
    await asyncio.wait_for(m.wait_idle(t.id), 5)
    assert db.get_tournament(t.id).status == TournamentStatus.FINISHED
    assert statuses(db, t.id) == Counter({GameStatus.FINISHED: 12})


async def test_cancel():
    db = make_db()
    runner = FakeRunner(gate=asyncio.Event())    # never released
    m = manager(db, runner)
    t = m.create("c", TournamentConfig(player_ids=list("abcd"), concurrency=2, openings="none"))
    await m.start(t.id)
    await until(lambda: len(m.running_game_ids()) == 2)
    await m.cancel(t.id)
    assert db.get_tournament(t.id).status == TournamentStatus.CANCELLED
    assert statuses(db, t.id) == Counter({GameStatus.ABORTED: 12})
    assert m.running_game_ids() == [] and all(m.player_load(p) == 0 for p in "abcd")
    with pytest.raises(ValueError):
        await m.start(t.id)


async def test_shutdown_and_resume_on_file_db(tmp_path):
    path = tmp_path / "chess.db"
    db = make_db(path)
    runner = FakeRunner(gate=asyncio.Event())
    m = manager(db, runner)
    t = m.create("r", TournamentConfig(player_ids=list("abcd"), concurrency=2, openings="none"))
    await m.start(t.id)
    await until(lambda: len(m.running_game_ids()) == 2)
    running = list(m.running_game_ids())
    await m.shutdown()
    assert db.get_tournament(t.id).status == TournamentStatus.RUNNING
    assert statuses(db, t.id) == Counter({GameStatus.SCHEDULED: 12})
    for gid in running:
        assert db.get_moves(gid) == [] and db.get_game(gid).started_at is None
    # simulate a hard crash leaving a game RUNNING with moves
    g = db.list_games(tournament_id=t.id, limit=1)[0]
    g.status = GameStatus.RUNNING
    db.update_game(g)
    db.add_move(g.id, g.white_id, MoveRecord(ply=0, color="white", uci="e2e4", san="e4", fen_after="x",
                                             elapsed_s=0, total_elapsed_s=0))
    db.close()

    db2 = Database(path)
    runner2 = FakeRunner(delay=0.005)
    m2 = manager(db2, runner2)
    await m2.resume_all()
    await asyncio.wait_for(m2.wait_idle(t.id), 10)
    assert db2.get_tournament(t.id).status == TournamentStatus.FINISHED
    assert statuses(db2, t.id) == Counter({GameStatus.FINISHED: 12})
    assert len(runner2.started) == 12


async def test_wait_for_remote_offline():
    db = make_db(kinds={"d": PlayerKind.REMOTE})
    hub = FakeHub()
    runner = FakeRunner(delay=0.005)
    m = manager(db, runner, hub=hub)
    t = m.create("w", TournamentConfig(player_ids=list("abcd"), concurrency=4, openings="none"))
    await m.start(t.id)
    await until(lambda: statuses(db, t.id)[GameStatus.FINISHED] == 6)
    await asyncio.sleep(0.1)
    remaining = db.list_games(tournament_id=t.id, status=GameStatus.SCHEDULED, limit=100)
    assert len(remaining) == 6 and all("d" in (g.white_id, g.black_id) for g in remaining)
    assert db.get_tournament(t.id).status == TournamentStatus.RUNNING
    hub.online.add("d")               # picked up by the periodic tick
    await asyncio.wait_for(m.wait_idle(t.id), 5)
    assert statuses(db, t.id) == Counter({GameStatus.FINISHED: 12})


async def test_remote_no_wait_starts_immediately():
    db = make_db(kinds={"d": PlayerKind.REMOTE})
    runner = FakeRunner(delay=0.001)
    m = manager(db, runner, hub=FakeHub())
    t = m.create("w", TournamentConfig(player_ids=list("abcd"), openings="none", wait_for_remote=False))
    await m.start(t.id)
    await asyncio.wait_for(m.wait_idle(t.id), 5)
    assert db.get_tournament(t.id).status == TournamentStatus.FINISHED


async def test_faulty_runner_does_not_kill_loop():
    db = make_db()
    m0 = manager(db, FakeRunner())
    t = m0.create("f", TournamentConfig(player_ids=list("abcd"), concurrency=2, openings="none"))
    ids = [g.id for g in db.list_games(tournament_id=t.id, limit=100)]
    runner = FakeRunner(delay=0.001, fail_ids=ids[:3])
    m = manager(db, runner)
    await m.start(t.id)
    await asyncio.wait_for(m.wait_idle(t.id), 5)
    assert db.get_tournament(t.id).status == TournamentStatus.FINISHED
    for gid in ids[:3]:
        g = db.get_game(gid)
        assert g.status == GameStatus.ABORTED and g.termination == Termination.ABORTED
        assert "boom" in g.termination_detail
    assert statuses(db, t.id)[GameStatus.FINISHED] == 9

    # an exception naming the culprit forfeits that side
    db2 = make_db()
    m1 = manager(db2, FakeRunner())
    t2 = m1.create("f2", TournamentConfig(player_ids=["a", "b"], games_per_pair=1, openings="none"))
    gid = db2.list_games(tournament_id=t2.id)[0].id
    m2 = manager(db2, FakeRunner(fail_ids=[gid], culprit=True))
    await m2.start(t2.id)
    await asyncio.wait_for(m2.wait_idle(t2.id), 5)
    g = db2.get_game(gid)
    assert g.status == GameStatus.FINISHED and g.termination == Termination.ERROR and g.result == "0-1"


async def test_player_factory_failure_forfeits():
    db = make_db(ids=("a", "b", "bad", "bad2"))
    runner = FakeRunner(delay=0.001)
    m = manager(db, runner, factory=fake_factory(fail=("bad", "bad2")))
    t = m.create("x", TournamentConfig(player_ids=["a", "b", "bad", "bad2"], openings="none"))
    await m.start(t.id)
    await asyncio.wait_for(m.wait_idle(t.id), 5)
    assert db.get_tournament(t.id).status == TournamentStatus.FINISHED
    for g in db.list_games(tournament_id=t.id, limit=100):
        bad = {g.white_id, g.black_id} & {"bad", "bad2"}
        if len(bad) == 2:
            assert g.status == GameStatus.ABORTED
        elif bad:
            assert g.status == GameStatus.FINISHED and g.termination == Termination.ERROR
            assert g.result == ("0-1" if g.white_id in bad else "1-0")
        else:
            assert g.termination == Termination.CHECKMATE
    rows = {r.player_id: r for r in m.standings(t.id, bootstrap=0)}
    assert rows["bad"].losses == 4 and rows["a"].wins >= 2


async def test_play_single_and_validation():
    db = make_db()
    runner = FakeRunner(delay=0.001)
    m = manager(db, runner)
    g = await m.play_single("a", "b", GameConfig(max_plies=10), opening_id="ruy-lopez")
    assert g.tournament_id is None and g.opening.id == "ruy-lopez"
    await asyncio.wait_for(m.wait_idle(), 5)
    assert db.get_game(g.id).status == GameStatus.FINISHED
    with pytest.raises(ValueError):
        await m.play_single("a", "a", GameConfig())
    with pytest.raises(ValueError):
        await m.play_single("a", "zz", GameConfig())
    with pytest.raises(ValueError):
        await m.play_single("a", "b", GameConfig(), opening_id="nope")
    with pytest.raises(ValueError):
        m.create("bad", TournamentConfig(player_ids=["a", "zz"]))
    assert db.list_tournaments() == []


async def test_shutdown_aborts_adhoc_games():
    db = make_db()
    m = manager(db, FakeRunner(gate=asyncio.Event()))
    g = await m.play_single("a", "b", GameConfig())
    await until(lambda: db.get_game(g.id).status == GameStatus.RUNNING)
    await m.shutdown()
    assert db.get_game(g.id).status == GameStatus.ABORTED


async def test_shutdown_before_game_task_starts_releases_capacity():
    """A game task cancelled before its first step must still release player load and its
    _games entry; otherwise the players stay 'busy' and later tournaments never finish."""
    db = make_db(ids=("a", "b"), cap=1)
    m = manager(db, FakeRunner())
    g = await m.play_single("a", "b", GameConfig())
    await m.shutdown()   # no await point between launching and cancelling the game task
    assert m.player_load("a") == m.player_load("b") == 0
    assert m.running_game_ids() == []
    assert db.get_game(g.id, include_moves=False).status == GameStatus.ABORTED
    t = m.create("T", TournamentConfig(player_ids=["a", "b"], games_per_pair=2, openings="none"))
    await m.start(t.id)
    await asyncio.wait_for(m.wait_idle(t.id), 5)
    assert db.get_tournament(t.id).status == TournamentStatus.FINISHED


async def test_retry_aborted_reopens_cancelled_tournament():
    db = make_db()
    gate = asyncio.Event()
    runner = FakeRunner(gate=gate)
    m = manager(db, runner)
    t = m.create("r", TournamentConfig(player_ids=list("abcd"), concurrency=2, openings="none"))
    await m.start(t.id)
    await until(lambda: len(m.running_game_ids()) == 2)
    await m.cancel(t.id)
    assert statuses(db, t.id) == Counter({GameStatus.ABORTED: 12})
    gate.set()
    assert await m.retry_aborted(t.id) == 12
    await asyncio.wait_for(m.wait_idle(t.id), 10)
    assert db.get_tournament(t.id).status == TournamentStatus.FINISHED
    assert statuses(db, t.id) == Counter({GameStatus.FINISHED: 12})
    assert await m.retry_aborted(t.id) == 0
