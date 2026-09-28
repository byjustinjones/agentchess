import math
import random
import time

import pytest

from agentchess.models import PlayerKind, PlayerSpec
from agentchess.rating import (
    RatingRow,
    compute_ratings,
    crosstable,
    expected_score,
    sequential_elo,
)


def spec(pid: str, anchor=None) -> PlayerSpec:
    return PlayerSpec(id=pid, name=pid.upper(), kind=PlayerKind.ENGINE, anchor_elo=anchor)


def games(a: str, b: str, w: int, d: int, l: int, t0: float = 0.0) -> list[dict]:
    """w wins / d draws / l losses for a vs b, alternating colours."""
    out = []
    outcomes = ["a"] * w + ["d"] * d + ["b"] * l
    for k, o in enumerate(outcomes):
        white, black = (a, b) if k % 2 == 0 else (b, a)
        if o == "d":
            res = "1/2-1/2"
        else:
            winner = a if o == "a" else b
            res = "1-0" if winner == white else "0-1"
        out.append({"white_id": white, "black_id": black, "result": res, "finished_at": t0 + k})
    return out


def by_id(rows: list[RatingRow]) -> dict[str, RatingRow]:
    return {r.player_id: r for r in rows}


def simulate(true: dict[str, float], n_games: int, rng: random.Random, draw_rate: float = 0.3) -> list[dict]:
    ids = list(true)
    out = []
    for k in range(n_games):
        a, b = rng.sample(ids, 2)
        e = expected_score(true[a], true[b])
        d = draw_rate * (1 - abs(2 * e - 1))
        u = rng.random()
        res = "1-0" if u < e - d / 2 else "1/2-1/2" if u < e + d / 2 else "0-1"
        out.append({"white_id": a, "black_id": b, "result": res, "finished_at": float(k)})
    return out


def test_expected_score():
    assert expected_score(1500, 1500) == pytest.approx(0.5)
    assert expected_score(1900, 1500) == pytest.approx(10 / 11)
    assert expected_score(1500, 1900) + expected_score(1900, 1500) == pytest.approx(1.0)


def test_two_player_analytic():
    res = games("a", "b", 30, 0, 10)            # 75%
    rows = by_id(compute_ratings(res, [spec("a"), spec("b")], prior_sd=1e6, bootstrap=0))
    diff = rows["a"].elo - rows["b"].elo
    assert diff == pytest.approx(400 * math.log10(3), abs=0.05)   # 190.85
    assert (rows["a"].elo + rows["b"].elo) / 2 == pytest.approx(1500)
    assert rows["a"].rank == 1 and rows["b"].rank == 2
    assert (rows["a"].wins, rows["a"].draws, rows["a"].losses, rows["a"].games) == (30, 0, 10, 40)
    assert rows["a"].score == pytest.approx(0.75)


def test_draws_count_as_half():
    res = games("a", "b", 10, 20, 0)            # 20/30 = 2/3
    rows = by_id(compute_ratings(res, [spec("a"), spec("b")], prior_sd=1e6, bootstrap=0))
    assert rows["a"].elo - rows["b"].elo == pytest.approx(400 * math.log10(2), abs=0.05)
    assert rows["a"].draws == 20 and rows["b"].draws == 20


def test_anchor_fixed_and_prior_centre():
    res = games("a", "sf", 30, 0, 10)
    rows = by_id(compute_ratings(res, [spec("a"), spec("sf", 1800)], prior_sd=1e6, bootstrap=50))
    assert rows["sf"].elo == 1800 and rows["sf"].anchored
    assert rows["sf"].ci_low == rows["sf"].ci_high == 1800
    assert rows["a"].elo == pytest.approx(1800 + 190.85, abs=0.1)
    # use_anchors=False: unanchored, mean shifted to base
    rows2 = by_id(compute_ratings(res, [spec("a"), spec("sf", 1800)], use_anchors=False, bootstrap=0))
    assert not rows2["sf"].anchored
    assert (rows2["a"].elo + rows2["sf"].elo) / 2 == pytest.approx(1500)


def test_symmetry():
    res = games("a", "b", 12, 6, 12) + games("b", "c", 5, 4, 11) + games("a", "c", 5, 4, 11)
    players = [spec("a"), spec("b"), spec("c")]
    rows = by_id(compute_ratings(res, players, bootstrap=0))
    assert rows["a"].elo == pytest.approx(rows["b"].elo, abs=1e-6)
    # swapping colours / order of results must not matter
    flipped = [{**r, "white_id": r["black_id"], "black_id": r["white_id"],
                "result": {"1-0": "0-1", "0-1": "1-0"}.get(r["result"], r["result"])} for r in reversed(res)]
    rows2 = by_id(compute_ratings(flipped, players, bootstrap=0))
    for pid in rows:
        assert rows2[pid].elo == pytest.approx(rows[pid].elo, abs=1e-6)


def test_perfect_score_finite():
    res = games("a", "b", 10, 0, 0)
    rows = by_id(compute_ratings(res, [spec("a"), spec("b")], bootstrap=100))
    assert all(math.isfinite(r.elo) and math.isfinite(r.ci_low) and math.isfinite(r.ci_high) for r in rows.values())
    assert rows["a"].elo > rows["b"].elo
    assert rows["a"].elo - rows["b"].elo < 2000
    assert rows["a"].performance == pytest.approx(rows["b"].elo + 800)
    assert rows["b"].performance == pytest.approx(rows["a"].elo - 800)


def test_single_game():
    rows = compute_ratings([{"white_id": "a", "black_id": "b", "result": "1-0"}], [spec("a")], bootstrap=20)
    assert len(rows) == 2
    r = by_id(rows)
    assert r["a"].elo > 1500 > r["b"].elo
    assert r["b"].name == "b" and r["b"].kind == "unknown"    # fallback when spec missing
    assert r["a"].name == "A"


def test_disconnected_components():
    res = games("a", "b", 15, 0, 5) + games("c", "d", 5, 0, 15)
    rows = by_id(compute_ratings(res, [spec(x) for x in "abcd"], bootstrap=30))
    assert rows["a"].elo == pytest.approx(rows["d"].elo, abs=1e-6)
    assert rows["b"].elo == pytest.approx(rows["c"].elo, abs=1e-6)
    assert sum(r.elo for r in rows.values()) / 4 == pytest.approx(1500)


def test_only_players_with_games_and_invalid_results_ignored():
    res = games("a", "b", 1, 1, 1) + [
        {"white_id": "a", "black_id": "c", "result": "*"},
        {"white_id": "a", "black_id": "a", "result": "1-0"},
    ]
    rows = compute_ratings(res, [spec("a"), spec("b"), spec("c"), spec("z")], bootstrap=0)
    assert {r.player_id for r in rows} == {"a", "b"}
    assert compute_ratings([], [spec("a")]) == []


def test_ci_contains_point_and_shrinks():
    small = compute_ratings(games("a", "b", 15, 0, 5), [spec("a"), spec("b")], bootstrap=200, seed=1)
    big = compute_ratings(games("a", "b", 300, 0, 100), [spec("a"), spec("b")], bootstrap=200, seed=1)
    for r in small + big:
        assert r.ci_low <= r.elo <= r.ci_high
    width = lambda rows: by_id(rows)["a"].ci_high - by_id(rows)["a"].ci_low
    assert width(big) < width(small) / 2
    # deterministic for a seed
    again = compute_ratings(games("a", "b", 15, 0, 5), [spec("a"), spec("b")], bootstrap=200, seed=1)
    assert [(r.ci_low, r.ci_high) for r in again] == [(r.ci_low, r.ci_high) for r in small]
    zero = compute_ratings(games("a", "b", 15, 0, 5), [spec("a"), spec("b")], bootstrap=0)
    assert all(r.ci_low == r.elo == r.ci_high for r in zero)


def test_anchored_simulation_recovers_true_ratings():
    rng = random.Random(42)
    true = {f"p{i}": rng.uniform(1100, 2300) for i in range(10)}
    true["sf1"], true["sf2"] = 1400.0, 2000.0
    players = [spec(pid, anchor=true[pid] if pid.startswith("sf") else None) for pid in true]
    res = simulate(true, 1500, rng)
    rows = compute_ratings(res, players, bootstrap=200, seed=3)
    free = [r for r in rows if not r.anchored]
    inside = sum(r.ci_low <= true[r.player_id] <= r.ci_high for r in free)
    assert inside >= 8                      # ~95% nominal coverage of 10
    mae = sum(abs(r.elo - true[r.player_id]) for r in free) / len(free)
    assert mae < 60
    # ranking mostly right: Spearman-ish check on the order
    order_est = [r.player_id for r in sorted(free, key=lambda r: -r.elo)]
    order_true = sorted((p for p in true if not p.startswith("sf")), key=lambda p: -true[p])
    assert order_est[0] in order_true[:2]


def test_bootstrap_speed():
    """30 players x 2000 games x 300 bootstraps must finish in a few seconds."""
    rng = random.Random(7)
    true = {f"p{i}": rng.uniform(1000, 2400) for i in range(30)}
    players = [spec(pid, anchor=true[pid] if i < 3 else None) for i, pid in enumerate(true)]
    res = simulate(true, 2000, rng)
    t0 = time.perf_counter()
    rows = compute_ratings(res, players, bootstrap=300)
    elapsed = time.perf_counter() - t0
    assert len(rows) == 30
    assert elapsed < 10, f"compute_ratings took {elapsed:.1f}s"


def test_performance_rating():
    res = games("a", "b", 3, 0, 1) + games("a", "c", 1, 0, 1)
    rows = by_id(compute_ratings(res, [spec(x) for x in "abc"], bootstrap=0))
    a = rows["a"]
    opp_avg = (4 * rows["b"].elo + 2 * rows["c"].elo) / 6
    s = 4 / 6
    assert a.performance == pytest.approx(opp_avg + 400 * math.log10(s / (1 - s)))


def test_crosstable():
    res = games("a", "b", 2, 1, 1) + games("a", "c", 1, 0, 0) + [{"white_id": "x", "black_id": "a", "result": "1-0"}]
    ct = crosstable(res, ["a", "b", "c"])
    assert ct["players"] == ["a", "b", "c"]
    assert ct["cells"]["a"]["b"] == {"w": 2, "d": 1, "l": 1, "score": 2.5, "games": 4}
    assert ct["cells"]["b"]["a"] == {"w": 1, "d": 1, "l": 2, "score": 1.5, "games": 4}
    assert ct["cells"]["c"]["a"]["l"] == 1
    assert ct["cells"]["b"]["c"]["games"] == 0
    assert "a" not in ct["cells"]["a"]


def test_sequential_elo():
    res = games("a", "sf", 3, 0, 0) + games("a", "b", 0, 1, 0, t0=10)
    hist = sequential_elo(res, [spec("a"), spec("sf", 1800), spec("b")], k=16)
    assert [h["elo"] for h in hist["sf"]] == [1800, 1800, 1800]
    assert len(hist["a"]) == 4 and len(hist["b"]) == 1
    assert hist["a"][0]["elo"] == pytest.approx(1500 + 16 * (1 - expected_score(1500, 1800)))
    assert hist["a"][1]["elo"] > hist["a"][0]["elo"]
    assert [h["t"] for h in hist["a"]] == sorted(h["t"] for h in hist["a"])
