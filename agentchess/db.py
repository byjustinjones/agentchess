"""SQLite persistence. Synchronous (sqlite3) behind a lock; calls are short.

All tournament/game state lives here so a crashed or restarted server can
resume tournaments exactly where they stopped.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from agentchess.models import (
    GameConfig,
    GameRecord,
    GameStatus,
    MoveAttempt,
    MoveRecord,
    Opening,
    PlayerKind,
    PlayerSpec,
    Termination,
    Tournament,
    TournamentConfig,
    TournamentStatus,
    now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    anchor_elo REAL,
    max_concurrent_games INTEGER NOT NULL DEFAULT 1,
    token_hash TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_players_token ON players(token_hash) WHERE token_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS tournaments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS games (
    id TEXT PRIMARY KEY,
    tournament_id TEXT REFERENCES tournaments(id) ON DELETE CASCADE,
    round INTEGER NOT NULL DEFAULT 0,
    seq INTEGER NOT NULL DEFAULT 0,
    white_id TEXT NOT NULL REFERENCES players(id),
    black_id TEXT NOT NULL REFERENCES players(id),
    opening_json TEXT,
    initial_fen TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    termination TEXT,
    termination_detail TEXT,
    pgn TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_games_tournament ON games(tournament_id, status);
CREATE INDEX IF NOT EXISTS idx_games_white ON games(white_id);
CREATE INDEX IF NOT EXISTS idx_games_black ON games(black_id);

CREATE TABLE IF NOT EXISTS moves (
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply INTEGER NOT NULL,
    color TEXT NOT NULL,
    player_id TEXT NOT NULL,
    uci TEXT NOT NULL,
    san TEXT NOT NULL,
    fen_after TEXT NOT NULL,
    elapsed_s REAL NOT NULL,
    total_elapsed_s REAL NOT NULL,
    illegal_count INTEGER NOT NULL DEFAULT 0,
    illegal_json TEXT NOT NULL DEFAULT '[]',
    comment TEXT,
    usage_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    PRIMARY KEY (game_id, ply)
);

-- Post-game engine analysis (agentchess/analysis.py). One row per analysed game, one
-- per (game, player) summary and one per judged ply. Analysing again replaces all three.
CREATE TABLE IF NOT EXISTS game_analysis (
    game_id TEXT PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    engine TEXT NOT NULL,
    depth INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS player_analysis (
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    player_id TEXT NOT NULL,
    moves INTEGER NOT NULL,            -- judged moves (non-book, position not already decided)
    total_loss REAL NOT NULL,          -- sum of centipawn losses over judged moves
    blunders INTEGER NOT NULL,
    mistakes INTEGER NOT NULL,
    inaccuracies INTEGER NOT NULL,
    best_moves INTEGER NOT NULL,       -- judged moves that matched the engine's first choice
    max_eval INTEGER NOT NULL,         -- best evaluation the player reached (cp, own POV)
    won INTEGER NOT NULL,              -- 1 if the player won the game
    missed_win INTEGER NOT NULL,       -- 1 if max_eval >= WIN_CP and the player did not win
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS idx_player_analysis_player ON player_analysis(player_id);
CREATE TABLE IF NOT EXISTS move_evals (
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply INTEGER NOT NULL,
    player_id TEXT NOT NULL,
    eval_before INTEGER NOT NULL,      -- cp from the mover's point of view before the move
    eval_after INTEGER NOT NULL,       -- cp from the mover's point of view after the move
    loss INTEGER NOT NULL,
    best_uci TEXT,
    judged INTEGER NOT NULL,           -- 0 for book moves / already decided positions
    PRIMARY KEY (game_id, ply)
);
"""

# Columns added after the first release: (table, column, DDL type/default). Applied by
# ``Database._migrate`` with ``ALTER TABLE ... ADD COLUMN`` when missing, so an old DB
# (e.g. one a running server still writes to) keeps working.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("games", "final_attempt_json", "TEXT"),
]

MAX_COMMENT_CHARS = 8000


def _col(r: sqlite3.Row, name: str) -> Any:
    """Column value or None when the row (e.g. from an older query) lacks the column."""
    return r[name] if name in r.keys() else None


class Database:
    def __init__(self, path: str | Path = "agentchess.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, column, ddl in MIGRATIONS:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def transaction(self):
        """Context manager: ``with db.transaction(): ...`` (re-entrant lock held)."""
        db = self

        class _Tx:
            def __enter__(self_inner):
                db._lock.acquire()
                db._conn.execute("BEGIN")
                return db

            def __exit__(self_inner, exc_type, exc, tb):
                try:
                    db._conn.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    db._lock.release()
                return False

        return _Tx()

    # ------------------------------------------------------------------ players
    @staticmethod
    def _row_to_player(r: sqlite3.Row) -> PlayerSpec:
        return PlayerSpec(
            id=r["id"],
            name=r["name"],
            kind=PlayerKind(r["kind"]),
            config=json.loads(r["config_json"] or "{}"),
            anchor_elo=r["anchor_elo"],
            max_concurrent_games=r["max_concurrent_games"],
            active=bool(r["active"]),
            created_at=r["created_at"],
        )

    def add_player(self, spec: PlayerSpec, token_hash: Optional[str] = None) -> PlayerSpec:
        self._exec(
            "INSERT INTO players (id,name,kind,config_json,anchor_elo,max_concurrent_games,token_hash,active,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (spec.id, spec.name, spec.kind.value, json.dumps(spec.config), spec.anchor_elo,
             spec.max_concurrent_games, token_hash, int(spec.active), spec.created_at),
        )
        return spec

    def update_player(self, spec: PlayerSpec) -> PlayerSpec:
        self._exec(
            "UPDATE players SET name=?, config_json=?, anchor_elo=?, max_concurrent_games=?, active=? WHERE id=?",
            (spec.name, json.dumps(spec.config), spec.anchor_elo, spec.max_concurrent_games,
             int(spec.active), spec.id),
        )
        return spec

    def set_player_token(self, player_id: str, token_hash: Optional[str]) -> None:
        self._exec("UPDATE players SET token_hash=? WHERE id=?", (token_hash, player_id))

    def get_player(self, player_id: str) -> Optional[PlayerSpec]:
        r = self._one("SELECT * FROM players WHERE id=?", (player_id,))
        return self._row_to_player(r) if r else None

    def get_player_by_token_hash(self, token_hash: str) -> Optional[PlayerSpec]:
        r = self._one("SELECT * FROM players WHERE token_hash=?", (token_hash,))
        return self._row_to_player(r) if r else None

    def list_players(self, include_inactive: bool = False) -> list[PlayerSpec]:
        sql = "SELECT * FROM players" + ("" if include_inactive else " WHERE active=1") + " ORDER BY created_at"
        return [self._row_to_player(r) for r in self._all(sql)]

    def player_has_games(self, player_id: str) -> bool:
        return self._one("SELECT 1 FROM games WHERE white_id=? OR black_id=? LIMIT 1", (player_id, player_id)) is not None

    def delete_player(self, player_id: str) -> bool:
        """Hard-delete a player without games; otherwise deactivate it. Returns True if hard-deleted."""
        if self.player_has_games(player_id):
            self._exec("UPDATE players SET active=0, token_hash=NULL WHERE id=?", (player_id,))
            return False
        self._exec("DELETE FROM players WHERE id=?", (player_id,))
        return True

    # -------------------------------------------------------------- tournaments
    @staticmethod
    def _row_to_tournament(r: sqlite3.Row) -> Tournament:
        return Tournament(
            id=r["id"],
            name=r["name"],
            config=TournamentConfig.from_dict(json.loads(r["config_json"])),
            status=TournamentStatus(r["status"]),
            created_at=r["created_at"],
            started_at=r["started_at"],
            finished_at=r["finished_at"],
        )

    def add_tournament(self, t: Tournament) -> Tournament:
        self._exec(
            "INSERT INTO tournaments (id,name,status,config_json,created_at,started_at,finished_at) VALUES (?,?,?,?,?,?,?)",
            (t.id, t.name, t.status.value, json.dumps(t.config.to_dict()), t.created_at, t.started_at, t.finished_at),
        )
        return t

    def update_tournament(self, t: Tournament) -> Tournament:
        self._exec(
            "UPDATE tournaments SET name=?, status=?, config_json=?, started_at=?, finished_at=? WHERE id=?",
            (t.name, t.status.value, json.dumps(t.config.to_dict()), t.started_at, t.finished_at, t.id),
        )
        return t

    def get_tournament(self, tournament_id: str) -> Optional[Tournament]:
        r = self._one("SELECT * FROM tournaments WHERE id=?", (tournament_id,))
        return self._row_to_tournament(r) if r else None

    def list_tournaments(self, status: Optional[TournamentStatus] = None) -> list[Tournament]:
        if status:
            rows = self._all("SELECT * FROM tournaments WHERE status=? ORDER BY created_at DESC", (status.value,))
        else:
            rows = self._all("SELECT * FROM tournaments ORDER BY created_at DESC")
        return [self._row_to_tournament(r) for r in rows]

    def delete_tournament(self, tournament_id: str) -> None:
        self._exec("DELETE FROM tournaments WHERE id=?", (tournament_id,))

    def tournament_progress(self, tournament_id: str) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM games WHERE tournament_id=? GROUP BY status", (tournament_id,))
        out = {s.value: 0 for s in GameStatus}
        for r in rows:
            out[r["status"]] = r["n"]
        out["total"] = sum(out[s.value] for s in GameStatus)
        return out

    # -------------------------------------------------------------------- games
    @staticmethod
    def _row_to_game(r: sqlite3.Row) -> GameRecord:
        opening = json.loads(r["opening_json"]) if r["opening_json"] else None
        return GameRecord(
            id=r["id"],
            white_id=r["white_id"],
            black_id=r["black_id"],
            tournament_id=r["tournament_id"],
            round=r["round"],
            opening=Opening(**opening) if opening else None,
            initial_fen=r["initial_fen"],
            status=GameStatus(r["status"]),
            result=r["result"],
            termination=Termination(r["termination"]) if r["termination"] else None,
            termination_detail=r["termination_detail"],
            pgn=r["pgn"],
            config=GameConfig.from_dict(json.loads(r["config_json"] or "{}")),
            created_at=r["created_at"],
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            final_attempt=json.loads(r["final_attempt_json"]) if _col(r, "final_attempt_json") else None,
        )

    @staticmethod
    def _row_to_move(r: sqlite3.Row) -> MoveRecord:
        return MoveRecord(
            ply=r["ply"],
            color=r["color"],
            uci=r["uci"],
            san=r["san"],
            fen_after=r["fen_after"],
            elapsed_s=r["elapsed_s"],
            total_elapsed_s=r["total_elapsed_s"],
            illegal_attempts=[MoveAttempt(**a) for a in json.loads(r["illegal_json"] or "[]")],
            comment=r["comment"],
            usage=json.loads(r["usage_json"] or "{}"),
            created_at=r["created_at"],
        )

    def add_game(self, g: GameRecord, seq: int = 0) -> GameRecord:
        self._exec(
            "INSERT INTO games (id,tournament_id,round,seq,white_id,black_id,opening_json,initial_fen,status,result,"
            "termination,termination_detail,pgn,config_json,created_at,started_at,finished_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (g.id, g.tournament_id, g.round, seq, g.white_id, g.black_id,
             json.dumps(g.opening.to_dict()) if g.opening else None, g.initial_fen, g.status.value, g.result,
             g.termination.value if g.termination else None, g.termination_detail, g.pgn,
             json.dumps(g.config.to_dict()), g.created_at, g.started_at, g.finished_at),
        )
        return g

    def add_games(self, games: list[GameRecord]) -> None:
        with self.transaction():
            for i, g in enumerate(games):
                self.add_game(g, seq=i)

    def update_game(self, g: GameRecord) -> GameRecord:
        """Persist status/result/termination/pgn/timestamps/final_attempt (not moves; use add_move)."""
        self._exec(
            "UPDATE games SET status=?, result=?, termination=?, termination_detail=?, pgn=?, started_at=?, finished_at=?,"
            " final_attempt_json=? WHERE id=?",
            (g.status.value, g.result, g.termination.value if g.termination else None, g.termination_detail,
             g.pgn, g.started_at, g.finished_at,
             json.dumps(g.final_attempt) if g.final_attempt is not None else None, g.id),
        )
        return g

    def get_game(self, game_id: str, include_moves: bool = True) -> Optional[GameRecord]:
        r = self._one("SELECT * FROM games WHERE id=?", (game_id,))
        if not r:
            return None
        g = self._row_to_game(r)
        if include_moves:
            g.moves = self.get_moves(game_id)
        return g

    def list_games(
        self,
        tournament_id: Optional[str] = None,
        status: Optional[GameStatus | str] = None,
        player_id: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
        order: str = "seq",
    ) -> list[GameRecord]:
        where, params = [], []
        if tournament_id:
            where.append("tournament_id=?")
            params.append(tournament_id)
        if status:
            where.append("status=?")
            params.append(status.value if isinstance(status, GameStatus) else status)
        if player_id:
            where.append("(white_id=? OR black_id=?)")
            params += [player_id, player_id]
        order_sql = {
            "seq": "created_at, seq",
            "recent": "COALESCE(finished_at, started_at, created_at) DESC",
        }.get(order, "created_at, seq")
        sql = ("SELECT games.*, (SELECT COUNT(*) FROM moves WHERE moves.game_id=games.id) AS n_moves FROM games"
               + (" WHERE " + " AND ".join(where) if where else ""))
        sql += f" ORDER BY {order_sql} LIMIT ? OFFSET ?"
        params += [limit, offset]
        out = []
        for r in self._all(sql, params):
            g = self._row_to_game(r)
            g.ply_count = r["n_moves"]
            out.append(g)
        return out

    def count_games(self, tournament_id: Optional[str] = None, status: Optional[str] = None,
                    player_id: Optional[str] = None) -> int:
        where, params = [], []
        if tournament_id:
            where.append("tournament_id=?")
            params.append(tournament_id)
        if status:
            where.append("status=?")
            params.append(status)
        if player_id:
            where.append("(white_id=? OR black_id=?)")
            params += [player_id, player_id]
        sql = "SELECT COUNT(*) AS n FROM games" + (" WHERE " + " AND ".join(where) if where else "")
        return self._one(sql, params)["n"]

    def get_moves(self, game_id: str) -> list[MoveRecord]:
        return [self._row_to_move(r) for r in self._all("SELECT * FROM moves WHERE game_id=? ORDER BY ply", (game_id,))]

    def add_move(self, game_id: str, player_id: str, m: MoveRecord) -> None:
        comment = m.comment[:MAX_COMMENT_CHARS] if m.comment else m.comment
        self._exec(
            "INSERT OR REPLACE INTO moves (game_id,ply,color,player_id,uci,san,fen_after,elapsed_s,total_elapsed_s,"
            "illegal_count,illegal_json,comment,usage_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (game_id, m.ply, m.color, player_id, m.uci, m.san, m.fen_after, m.elapsed_s, m.total_elapsed_s,
             len(m.illegal_attempts), json.dumps([a.__dict__ for a in m.illegal_attempts]), comment,
             json.dumps(m.usage), m.created_at),
        )

    def clear_moves(self, game_id: str) -> None:
        self._exec("DELETE FROM moves WHERE game_id=?", (game_id,))

    def recover_interrupted_games(self) -> list[str]:
        """Called at startup. Games left RUNNING by a crash are reset: tournament games go
        back to SCHEDULED (moves wiped, replayed from scratch); ad-hoc games become ABORTED."""
        rows = self._all("SELECT id, tournament_id FROM games WHERE status=?", (GameStatus.RUNNING.value,))
        with self.transaction():
            for r in rows:
                self._conn.execute("DELETE FROM moves WHERE game_id=?", (r["id"],))
                if r["tournament_id"]:
                    self._conn.execute(
                        "UPDATE games SET status=?, started_at=NULL, pgn=NULL, final_attempt_json=NULL WHERE id=?",
                        (GameStatus.SCHEDULED.value, r["id"]))
                else:
                    self._conn.execute(
                        "UPDATE games SET status=?, termination=?, finished_at=? WHERE id=?",
                        (GameStatus.ABORTED.value, Termination.ABORTED.value, now(), r["id"]))
        return [r["id"] for r in rows]

    def reschedule_aborted(self, tournament_id: str) -> int:
        """Reset a tournament's ABORTED games to SCHEDULED (moves wiped). Returns how many."""
        rows = self._all("SELECT id FROM games WHERE tournament_id=? AND status=?",
                         (tournament_id, GameStatus.ABORTED.value))
        with self.transaction():
            for r in rows:
                self._conn.execute("DELETE FROM moves WHERE game_id=?", (r["id"],))
                self._conn.execute(
                    "UPDATE games SET status=?, result=NULL, termination=NULL, termination_detail=NULL, pgn=NULL,"
                    " started_at=NULL, finished_at=NULL, final_attempt_json=NULL WHERE id=?",
                    (GameStatus.SCHEDULED.value, r["id"]))
        return len(rows)

    def rated_results(self, tournament_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Finished, decided games for rating: [{white_id, black_id, result, termination, tournament_id, finished_at}]."""
        sql = ("SELECT id, white_id, black_id, result, termination, tournament_id, finished_at FROM games"
               " WHERE status=? AND result IN ('1-0','0-1','1/2-1/2')")
        params: list[Any] = [GameStatus.FINISHED.value]
        if tournament_id:
            sql += " AND tournament_id=?"
            params.append(tournament_id)
        sql += " ORDER BY finished_at"
        return [dict(r) for r in self._all(sql, params)]

    def move_stats(self, tournament_id: Optional[str] = None) -> dict[str, dict[str, Any]]:
        """Per-player move quality/cost stats over finished games."""
        sql = (
            "SELECT m.player_id AS player_id, COUNT(*) AS moves, SUM(m.illegal_count) AS illegal_attempts,"
            " SUM(CASE WHEN m.illegal_count>0 THEN 1 ELSE 0 END) AS moves_with_illegal,"
            " AVG(m.total_elapsed_s) AS avg_move_s,"
            " SUM(COALESCE(json_extract(m.usage_json,'$.input_tokens'),0)) AS input_tokens,"
            " SUM(COALESCE(json_extract(m.usage_json,'$.output_tokens'),0)) AS output_tokens,"
            " SUM(COALESCE(json_extract(m.usage_json,'$.cost_usd'),0)) AS cost_usd"
            " FROM moves m JOIN games g ON g.id=m.game_id WHERE g.status=?"
            " AND COALESCE(json_extract(m.usage_json,'$.book'),0)=0"
        )
        params: list[Any] = [GameStatus.FINISHED.value]
        if tournament_id:
            sql += " AND g.tournament_id=?"
            params.append(tournament_id)
        sql += " GROUP BY m.player_id"
        out = {r["player_id"]: dict(r) for r in self._all(sql, params)}
        # The unfinished last move of forfeited/aborted games (illegal attempts, tokens) counts too.
        fsql = "SELECT final_attempt_json FROM games WHERE status IN (?, ?) AND final_attempt_json IS NOT NULL"
        fparams: list[Any] = [GameStatus.FINISHED.value, GameStatus.ABORTED.value]
        if tournament_id:
            fsql += " AND tournament_id=?"
            fparams.append(tournament_id)
        for r in self._all(fsql, fparams):
            try:
                fa = json.loads(r["final_attempt_json"])
            except ValueError:
                continue
            pid = fa.get("player_id") if isinstance(fa, dict) else None
            if not pid:
                continue
            d = out.setdefault(pid, {"player_id": pid, "moves": 0, "illegal_attempts": 0, "moves_with_illegal": 0,
                                     "avg_move_s": None, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0})
            attempts = fa.get("illegal_attempts") or []
            d["illegal_attempts"] = (d.get("illegal_attempts") or 0) + len(attempts)
            usage = fa.get("usage") or {}
            for k in ("input_tokens", "output_tokens", "cost_usd"):
                v = usage.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    d[k] = (d.get(k) or 0) + v
        return out

    # ----------------------------------------------------------------- analysis
    def save_analysis(self, game_id: str, engine: str, depth: int, moves: list[dict[str, Any]],
                      players: dict[str, dict[str, Any]]) -> None:
        """Replace the engine analysis of a game (see ``agentchess.analysis``)."""
        with self.transaction():
            for table in ("move_evals", "player_analysis", "game_analysis"):
                self._conn.execute(f"DELETE FROM {table} WHERE game_id=?", (game_id,))
            self._conn.execute("INSERT INTO game_analysis (game_id, engine, depth, created_at) VALUES (?,?,?,?)",
                               (game_id, engine, depth, now()))
            self._conn.executemany(
                "INSERT INTO move_evals (game_id, ply, player_id, eval_before, eval_after, loss, best_uci, judged)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [(game_id, m["ply"], m["player_id"], m["eval_before"], m["eval_after"], m["loss"],
                  m.get("best_uci"), int(bool(m["judged"]))) for m in moves])
            self._conn.executemany(
                "INSERT INTO player_analysis (game_id, player_id, moves, total_loss, blunders, mistakes, inaccuracies,"
                " best_moves, max_eval, won, missed_win) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(game_id, pid, s["moves"], s["total_loss"], s["blunders"], s["mistakes"], s["inaccuracies"],
                  s["best_moves"], s["max_eval"], int(bool(s["won"])), int(bool(s["missed_win"])))
                 for pid, s in players.items()])

    def get_analysis(self, game_id: str) -> Optional[dict[str, Any]]:
        head = self._one("SELECT * FROM game_analysis WHERE game_id=?", (game_id,))
        if head is None:
            return None
        moves = [dict(r) for r in self._all("SELECT ply, player_id, eval_before, eval_after, loss, best_uci, judged"
                                            " FROM move_evals WHERE game_id=? ORDER BY ply", (game_id,))]
        for m in moves:
            m["judged"] = bool(m["judged"])
        players = {}
        for r in self._all("SELECT * FROM player_analysis WHERE game_id=?", (game_id,)):
            d = dict(r)
            d.pop("game_id", None)
            d["won"], d["missed_win"] = bool(d["won"]), bool(d["missed_win"])
            d["acpl"] = d["total_loss"] / d["moves"] if d["moves"] else None
            players[d.pop("player_id")] = d
        return {"game_id": game_id, "engine": head["engine"], "depth": head["depth"],
                "created_at": head["created_at"], "moves": moves, "players": players}

    def delete_analysis(self, game_id: str) -> None:
        with self.transaction():
            for table in ("move_evals", "player_analysis", "game_analysis"):
                self._conn.execute(f"DELETE FROM {table} WHERE game_id=?", (game_id,))

    def unanalysed_game_ids(self, tournament_id: Optional[str] = None, player_id: Optional[str] = None,
                            limit: int = 10**9) -> list[str]:
        """Finished games without a stored analysis, oldest first."""
        sql = ("SELECT g.id FROM games g LEFT JOIN game_analysis a ON a.game_id=g.id"
               " WHERE g.status=? AND a.game_id IS NULL")
        params: list[Any] = [GameStatus.FINISHED.value]
        if tournament_id:
            sql += " AND g.tournament_id=?"
            params.append(tournament_id)
        if player_id:
            sql += " AND (g.white_id=? OR g.black_id=?)"
            params += [player_id, player_id]
        sql += " ORDER BY g.finished_at LIMIT ?"
        params.append(limit)
        return [r["id"] for r in self._all(sql, params)]

    def analysis_stats(self, tournament_id: Optional[str] = None) -> dict[str, dict[str, Any]]:
        """Per-player move quality over analysed finished games:
        {player_id: {analysed_games, judged_moves, acpl, blunders, mistakes, inaccuracies,
                     blunders_per_100, mistakes_per_100, best_move_rate, missed_wins}}."""
        sql = ("SELECT a.player_id AS player_id, COUNT(*) AS analysed_games, SUM(a.moves) AS judged_moves,"
               " SUM(a.total_loss) AS total_loss, SUM(a.blunders) AS blunders, SUM(a.mistakes) AS mistakes,"
               " SUM(a.inaccuracies) AS inaccuracies, SUM(a.best_moves) AS best_moves, SUM(a.missed_win) AS missed_wins"
               " FROM player_analysis a JOIN games g ON g.id=a.game_id WHERE g.status=?")
        params: list[Any] = [GameStatus.FINISHED.value]
        if tournament_id:
            sql += " AND g.tournament_id=?"
            params.append(tournament_id)
        sql += " GROUP BY a.player_id"
        out: dict[str, dict[str, Any]] = {}
        for r in self._all(sql, params):
            d = dict(r)
            n = d["judged_moves"] or 0
            d["acpl"] = (d.pop("total_loss") or 0) / n if n else None
            d["blunders_per_100"] = 100.0 * d["blunders"] / n if n else None
            d["mistakes_per_100"] = 100.0 * d["mistakes"] / n if n else None
            d["best_move_rate"] = d["best_moves"] / n if n else None
            out[d.pop("player_id")] = d
        return out

    def termination_stats(self, tournament_id: Optional[str] = None) -> dict[str, dict[str, int]]:
        """{player_id: {"forfeit_illegal": n, "forfeit_timeout": n, "forfeit_error": n, "resigned": n}} (losses only)."""
        sql = ("SELECT white_id, black_id, result, termination FROM games WHERE status=?"
               " AND termination IN ('illegal_moves','timeout','error','resignation')")
        params: list[Any] = [GameStatus.FINISHED.value]
        if tournament_id:
            sql += " AND tournament_id=?"
            params.append(tournament_id)
        key = {"illegal_moves": "forfeit_illegal", "timeout": "forfeit_timeout",
               "error": "forfeit_error", "resignation": "resigned"}
        out: dict[str, dict[str, int]] = {}
        for r in self._all(sql, params):
            loser = r["white_id"] if r["result"] == "0-1" else r["black_id"] if r["result"] == "1-0" else None
            if loser:
                d = out.setdefault(loser, {v: 0 for v in key.values()})
                d[key[r["termination"]]] += 1
        return out
