"""Elo ratings from game results: Bradley–Terry MAP fit, bootstrap CIs, crosstables.

Model
-----
P(A beats B) = 1 / (1 + 10^((R_B - R_A)/400)); a draw counts as half a win for
each side (the likelihood uses fractional scores). Each non-anchored player gets
a Gaussian prior N(center, prior_sd²), which keeps 100%/0% scorers finite and
ties disconnected groups of players to a common scale. ``center`` is the mean
anchor Elo when anchors are used, else ``base``. Without anchors the solution is
shifted so the mean rating equals ``base``.

The log-posterior is concave, so it is maximised with damped Newton–Raphson
(dense Cholesky solve of the free players' Hessian, backtracking line search).
Games are aggregated per player pair first, so the cost per iteration is
O(pairs + players³/6) independent of the number of games.

Confidence intervals: percentile bootstrap — resample games with replacement,
refit (warm-started from the point estimate), take the 2.5/97.5 percentiles.

Performance rating: mean final Elo of the opponents faced (per game) plus
400·log10(s/(1−s)) for score fraction s; this term is clamped to ±800, so a
perfect (or zero) score gives opponents' average ± 800.
"""
from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional

from agentchess.models import PlayerSpec, score_of

_C = math.log(10.0) / 400.0          # logistic slope on the Elo scale
PERF_CLAMP = 800.0


@dataclass
class RatingRow:
    player_id: str
    name: str
    kind: str
    elo: float
    ci_low: float
    ci_high: float           # 95% bootstrap interval
    anchored: bool           # rating fixed by anchor_elo
    games: int
    wins: int
    draws: int
    losses: int
    score: float             # points / games
    performance: Optional[float]
    rank: int
    opponents: int = 0                     # distinct opponents faced
    linked_to_anchor: bool = True          # False: no chain of games connects this player to an anchor,
                                           # so the rating is only relative to its own group (prior-centred)
    games_for_ci50: Optional[int] = None   # estimated extra games needed for a +-50 Elo 95% CI (None: already there / anchored)
    p_above_next: Optional[float] = None   # bootstrap P(rating > next-ranked player's rating); None for the last row

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RatingReport:
    """Ratings plus the pairwise comparison derived from the same bootstrap."""
    rows: list[RatingRow]
    superiority: dict[str, dict[str, float]]   # superiority[a][b] = P(rating_a > rating_b), a != b

    def to_dict(self) -> dict[str, Any]:
        return {"ratings": [r.to_dict() for r in self.rows], "superiority": self.superiority}


CI_TARGET_HALFWIDTH = 50.0


def expected_score(ra: float, rb: float) -> float:
    """Expected score of a player rated ``ra`` against one rated ``rb``."""
    return 1.0 / (1.0 + 10.0 ** ((rb - ra) / 400.0))


# --------------------------------------------------------------------------- helpers
def _softplus(x: float) -> float:
    return (x if x > 0 else 0.0) + math.log1p(math.exp(-abs(x)))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _parse_results(results: Iterable[dict]) -> list[tuple[str, str, float]]:
    """(white_id, black_id, white_points) for every decided game between two distinct players."""
    out = []
    for r in results:
        w, b = r.get("white_id"), r.get("black_id")
        s = score_of(r.get("result"), True)
        if w and b and w != b and s is not None:
            out.append((w, b, s))
    return out


def _cholesky_solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Solve A x = b for symmetric positive-definite A (lower Cholesky, in pure Python)."""
    n = len(b)
    low: list[list[float]] = []
    for i in range(n):
        li = [0.0] * n
        ai = a[i]
        for j in range(i):
            lj = low[j]
            li[j] = (ai[j] - sum(map(float.__mul__, li[:j], lj[:j]))) / lj[j]
        d = ai[i] - sum(map(float.__mul__, li[:i], li[:i]))
        li[i] = math.sqrt(d if d > 1e-300 else 1e-300)
        low.append(li)
    y = [0.0] * n
    for i in range(n):
        li = low[i]
        y[i] = (b[i] - sum(map(float.__mul__, li[:i], y[:i]))) / li[i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = y[i]
        for k in range(i + 1, n):
            s -= low[k][i] * x[k]
        x[i] = s / low[i][i]
    return x


Pair = tuple[int, int, float, float]   # (i, j, games, points of i)


def _fit(pairs: list[Pair], r0: list[float], free: list[int], mu: float, prec: float,
         tol: float = 1e-6, max_iter: int = 100) -> list[float]:
    """MAP ratings by damped Newton. ``free`` lists indices being estimated; others stay at r0."""
    r = list(r0)
    nf = len(free)
    if nf == 0:
        return r
    fidx = [-1] * len(r)
    for k, i in enumerate(free):
        fidx[i] = k

    def objective(rr: list[float]) -> float:
        v = 0.0
        for i, j, n, s in pairs:
            x = _C * (rr[i] - rr[j])
            v -= s * _softplus(-x) + (n - s) * _softplus(x)
        for i in free:
            d = rr[i] - mu
            v -= 0.5 * prec * d * d
        return v

    f_cur = objective(r)
    for _ in range(max_iter):
        grad = [-prec * (r[i] - mu) for i in free]
        hess = [[0.0] * nf for _ in range(nf)]
        for k in range(nf):
            hess[k][k] = prec
        for i, j, n, s in pairs:
            p = _sigmoid(_C * (r[i] - r[j]))
            g = _C * (s - n * p)
            h = _C * _C * n * p * (1.0 - p)
            ki, kj = fidx[i], fidx[j]
            if ki >= 0:
                grad[ki] += g
                hess[ki][ki] += h
            if kj >= 0:
                grad[kj] -= g
                hess[kj][kj] += h
            if ki >= 0 and kj >= 0:
                hess[ki][kj] -= h
                hess[kj][ki] -= h
        step = _cholesky_solve(hess, grad)
        big = max(abs(x) for x in step)
        if big > 400.0:                       # keep early steps sane
            step = [x * 400.0 / big for x in step]
            big = 400.0
        t = 1.0
        while True:
            cand = list(r)
            for k, i in enumerate(free):
                cand[i] += t * step[k]
            f_new = objective(cand)
            if f_new >= f_cur - 1e-12 or t < 1e-6:
                break
            t *= 0.5
        r, f_cur = cand, f_new
        if big * t < tol:
            break
    return r


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _components(n: int, edges: Iterable[tuple[int, int]]) -> list[int]:
    """Connected-component label per node (union-find)."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    return [find(i) for i in range(n)]


def _binom_two_sided_p(k: int, n: int) -> Optional[float]:
    """Two-sided exact sign test: P(#successes as or more extreme than k | n, p=0.5)."""
    if n <= 0:
        return None
    pk = [math.comb(n, i) / 2.0 ** n for i in range(n + 1)]
    p = sum(v for v in pk if v <= pk[k] + 1e-12)
    return min(1.0, p)


# --------------------------------------------------------------------------- public API
def compute_ratings(results: list[dict], players: list[PlayerSpec], *, use_anchors: bool = True,
                    base: float = 1500.0, prior_sd: float = 400.0, bootstrap: int = 300,
                    seed: int = 0) -> list[RatingRow]:
    """Fit Elo ratings to ``results`` (dicts with white_id, black_id, result).

    Returns one row per player with ≥1 decided game, sorted by elo desc (rank 1 = best).
    Bootstrap is deterministic for a given ``seed``; ``bootstrap=0`` gives zero-width CIs.
    """
    return rating_report(results, players, use_anchors=use_anchors, base=base, prior_sd=prior_sd,
                         bootstrap=bootstrap, seed=seed).rows


def rating_report(results: list[dict], players: list[PlayerSpec], *, use_anchors: bool = True,
                  base: float = 1500.0, prior_sd: float = 400.0, bootstrap: int = 300,
                  seed: int = 0) -> RatingReport:
    """Like :func:`compute_ratings` but also returns the pairwise superiority matrix
    ``P(rating_a > rating_b)`` estimated from the bootstrap (0.5 for every pair when ``bootstrap=0``)."""
    games = _parse_results(results)
    if not games:
        return RatingReport([], {})
    specs = {p.id: p for p in players}
    ids: list[str] = []
    index: dict[str, int] = {}
    for w, b, _ in games:
        for pid in (w, b):
            if pid not in index:
                index[pid] = len(ids)
                ids.append(pid)
    n_players = len(ids)

    anchors: dict[int, float] = {}
    if use_anchors:
        for pid, i in index.items():
            spec = specs.get(pid)
            if spec is not None and spec.anchor_elo is not None:
                anchors[i] = float(spec.anchor_elo)
    mu = sum(anchors.values()) / len(anchors) if anchors else base
    prec = 1.0 / (prior_sd * prior_sd)
    free = [i for i in range(n_players) if i not in anchors]

    # Encode games per unordered pair (lo, hi) with points for lo.
    pair_index: dict[tuple[int, int], int] = {}
    pair_ends: list[tuple[int, int]] = []
    enc: list[tuple[int, float]] = []
    wins = [0] * n_players
    draws = [0] * n_players
    losses = [0] * n_players
    for w, b, s in games:
        iw, ib = index[w], index[b]
        if s == 1.0:
            wins[iw] += 1
            losses[ib] += 1
        elif s == 0.0:
            wins[ib] += 1
            losses[iw] += 1
        else:
            draws[iw] += 1
            draws[ib] += 1
        lo, hi, pts = (iw, ib, s) if iw < ib else (ib, iw, 1.0 - s)
        key = (lo, hi)
        if key not in pair_index:
            pair_index[key] = len(pair_ends)
            pair_ends.append(key)
        enc.append((pair_index[key], pts))

    def aggregate(sample: Iterable[tuple[int, float]]) -> list[Pair]:
        n = [0.0] * len(pair_ends)
        s = [0.0] * len(pair_ends)
        for p, pts in sample:
            n[p] += 1.0
            s[p] += pts
        return [(pair_ends[k][0], pair_ends[k][1], n[k], s[k]) for k in range(len(pair_ends)) if n[k] > 0]

    def solve(pairs: list[Pair], start: list[float], tol: float) -> list[float]:
        r = _fit(pairs, start, free, mu, prec, tol=tol)
        if not anchors:
            shift = base - sum(r) / n_players
            r = [x + shift for x in r]
        return r

    init = [anchors.get(i, mu) for i in range(n_players)]
    elo = solve(aggregate(enc), init, 1e-6)

    ci_low, ci_high = list(elo), list(elo)
    samples: list[list[float]] = [[] for _ in range(n_players)]
    if bootstrap > 0:
        rng = random.Random(seed)
        n_games = len(enc)
        for _ in range(bootstrap):
            rb = solve(aggregate(rng.choices(enc, k=n_games)), elo, 1e-3)
            for i in range(n_players):
                samples[i].append(rb[i])
        for i in range(n_players):
            if i in anchors:
                continue
            vals = sorted(samples[i])
            # Percentile CI, widened if needed so it always contains the point estimate.
            ci_low[i] = min(_percentile(vals, 0.025), elo[i])
            ci_high[i] = max(_percentile(vals, 0.975), elo[i])

    # Performance: opponents' mean final elo + clamped logistic score term.
    opp_sum = [0.0] * n_players
    opponents: list[set[int]] = [set() for _ in range(n_players)]
    for w, b, _ in games:
        iw, ib = index[w], index[b]
        opp_sum[iw] += elo[ib]
        opp_sum[ib] += elo[iw]
        opponents[iw].add(ib)
        opponents[ib].add(iw)

    # Which players are tied to an anchor through a chain of games?
    comp = _components(n_players, pair_ends)
    anchored_comps = {comp[i] for i in anchors}

    rows: list[RatingRow] = []
    for pid, i in index.items():
        g = wins[i] + draws[i] + losses[i]
        score = (wins[i] + 0.5 * draws[i]) / g
        if score <= 0.0:
            delta = -PERF_CLAMP
        elif score >= 1.0:
            delta = PERF_CLAMP
        else:
            delta = max(-PERF_CLAMP, min(PERF_CLAMP, 400.0 * math.log10(score / (1.0 - score))))
        spec = specs.get(pid)
        half = (ci_high[i] - ci_low[i]) / 2.0
        needed: Optional[int] = None
        if i not in anchors and bootstrap > 0 and half > CI_TARGET_HALFWIDTH:
            # CI half-width shrinks roughly with 1/sqrt(games).
            needed = max(1, int(math.ceil(g * ((half / CI_TARGET_HALFWIDTH) ** 2 - 1.0))))
        rows.append(RatingRow(
            player_id=pid,
            name=spec.name if spec else pid,
            kind=(spec.kind.value if hasattr(spec.kind, "value") else str(spec.kind)) if spec else "unknown",
            elo=elo[i], ci_low=ci_low[i], ci_high=ci_high[i], anchored=i in anchors,
            games=g, wins=wins[i], draws=draws[i], losses=losses[i], score=score,
            performance=opp_sum[i] / g + delta, rank=0,
            opponents=len(opponents[i]),
            linked_to_anchor=(not anchors) or comp[i] in anchored_comps,
            games_for_ci50=needed,
        ))
    rows.sort(key=lambda r: (-r.elo, r.name, r.player_id))
    for k, row in enumerate(rows, 1):
        row.rank = k

    # Pairwise superiority from the bootstrap replicates (anchors are constant across replicates).
    def sample_of(i: int) -> list[float]:
        return samples[i] if samples[i] else [elo[i]]

    superiority: dict[str, dict[str, float]] = {}
    for ra in rows:
        ia = index[ra.player_id]
        sa = sample_of(ia)
        superiority[ra.player_id] = {}
        for rb_ in rows:
            if rb_ is ra:
                continue
            ib = index[rb_.player_id]
            sb = sample_of(ib)
            if len(sa) == 1 and len(sb) == 1:
                p = 1.0 if sa[0] > sb[0] else 0.0 if sa[0] < sb[0] else 0.5
            else:
                n = max(len(sa), len(sb))
                p = sum((1.0 if x > y else 0.5 if x == y else 0.0)
                        for x, y in zip(_cycle(sa, n), _cycle(sb, n))) / n
            superiority[ra.player_id][rb_.player_id] = p
    for k, row in enumerate(rows):
        if k + 1 < len(rows):
            row.p_above_next = superiority[row.player_id][rows[k + 1].player_id]
    return RatingReport(rows, superiority)


def _cycle(xs: list[float], n: int) -> list[float]:
    return xs if len(xs) == n else [xs[k % len(xs)] for k in range(n)]


def crosstable(results: list[dict], player_ids: list[str]) -> dict:
    """Head-to-head table. ``cells[a][b]`` is from a's perspective for every a != b in
    ``player_ids`` (``games == 0`` when they have not met); ``score`` is points (w + d/2).
    ``p`` is the two-sided exact sign-test p-value of wins vs losses (draws ignored; None
    without decisive games): small values mean the head-to-head edge is unlikely to be luck."""
    wanted = set(player_ids)
    cells: dict[str, dict[str, dict[str, Any]]] = {
        a: {b: {"w": 0, "d": 0, "l": 0, "score": 0.0, "games": 0, "p": None} for b in player_ids if b != a}
        for a in player_ids
    }
    for w, b, s in _parse_results(results):
        if w not in wanted or b not in wanted:
            continue
        for me, opp, pts in ((w, b, s), (b, w, 1.0 - s)):
            c = cells[me][opp]
            c["games"] += 1
            c["score"] += pts
            c["w" if pts == 1.0 else "l" if pts == 0.0 else "d"] += 1
    for a in player_ids:
        for b, c in cells[a].items():
            decisive = c["w"] + c["l"]
            c["p"] = _binom_two_sided_p(c["w"], decisive) if decisive else None
    return {"players": list(player_ids), "cells": cells}


def sequential_elo(results: list[dict], players: list[PlayerSpec], k: float = 16,
                   base: float = 1500.0) -> dict[str, list[dict[str, float]]]:
    """Classic incremental Elo in ``finished_at`` order, for rating-history charts.

    Returns {player_id: [{"t": finished_at, "elo": rating after that game}, ...]}.
    Players with ``anchor_elo`` start at and stay fixed at it.
    """
    anchors = {p.id: float(p.anchor_elo) for p in players if p.anchor_elo is not None}
    ratings: dict[str, float] = {}
    history: dict[str, list[dict[str, float]]] = {}
    ordered = sorted((r for r in results), key=lambda r: r.get("finished_at") or 0.0)
    for r in ordered:
        w, b = r.get("white_id"), r.get("black_id")
        s = score_of(r.get("result"), True)
        if not w or not b or w == b or s is None:
            continue
        rw = ratings.setdefault(w, anchors.get(w, base))
        rb = ratings.setdefault(b, anchors.get(b, base))
        e = expected_score(rw, rb)
        if w not in anchors:
            ratings[w] = rw + k * (s - e)
        if b not in anchors:
            ratings[b] = rb + k * ((1.0 - s) - (1.0 - e))
        t = r.get("finished_at") or 0.0
        history.setdefault(w, []).append({"t": t, "elo": ratings[w]})
        history.setdefault(b, []).append({"t": t, "elo": ratings[b]})
    return history
