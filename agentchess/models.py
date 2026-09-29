"""Core data types shared by every agentchess module.

These are the contracts between the game runner, players, tournament
manager, storage layer and HTTP API. Keep them plain dataclasses so they
serialize trivially with ``to_dict()``.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


class PlayerKind(str, Enum):
    ENGINE = "engine"   # UCI engine (Stockfish) at a configured strength
    LLM = "llm"         # built-in LLM player calling a provider API
    REMOTE = "remote"   # external agent connecting over HTTP / WebSocket / MCP
    RANDOM = "random"   # uniformly random legal moves (rating floor)


class GameStatus(str, Enum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    FINISHED = "finished"
    ABORTED = "aborted"   # not rated (server shutdown, cancelled tournament, ...)


class TournamentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class Termination(str, Enum):
    CHECKMATE = "checkmate"
    STALEMATE = "stalemate"
    INSUFFICIENT_MATERIAL = "insufficient_material"
    FIFTY_MOVES = "fifty_moves"
    THREEFOLD_REPETITION = "threefold_repetition"
    MAX_PLIES = "max_plies"            # adjudicated draw after GameConfig.max_plies
    RESIGNATION = "resignation"
    ILLEGAL_MOVES = "illegal_moves"    # forfeit: exceeded max_illegal_attempts on one move
    TIMEOUT = "timeout"                # forfeit: no answer within move_timeout_s
    ERROR = "error"                    # forfeit: player raised / disconnected
    ABORTED = "aborted"


@dataclass
class PlayerSpec:
    """A registered participant. ``config`` is kind-specific (see docs/ARCHITECTURE.md)."""
    id: str
    name: str
    kind: PlayerKind
    config: dict[str, Any] = field(default_factory=dict)
    # Fixed rating used to anchor the Elo scale (e.g. Stockfish UCI_Elo levels).
    anchor_elo: Optional[float] = None
    # Max simultaneous games this player may be scheduled in.
    max_concurrent_games: int = 1
    active: bool = True
    created_at: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class GameConfig:
    """Rules applied by the game runner."""
    move_timeout_s: float = 300.0     # per move; exceeding it forfeits the game
    max_illegal_attempts: int = 3     # illegal/unparseable answers allowed per move before forfeit
    max_plies: int = 300              # adjudicate a draw after this many half-moves
    show_legal_moves: bool = True     # include legal move list in MoveRequest (benchmark variable)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "GameConfig":
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Opening:
    id: str
    name: str
    eco: str = ""
    moves_uci: list[str] = field(default_factory=list)   # played from the standard start position

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MoveRequest:
    """Everything a player gets when asked for a move."""
    request_id: str
    game_id: str
    color: str                      # "white" | "black"
    fen: str
    initial_fen: str
    ply: int                        # number of half-moves already played (0-based index of the move to make)
    move_number: int                # fullmove number
    history_san: list[str]
    history_uci: list[str]
    pgn: str                        # movetext so far, e.g. "1. e4 e5 2. Nf3"
    legal_moves_uci: list[str]      # empty when GameConfig.show_legal_moves is False
    legal_moves_san: list[str]
    opponent_name: str
    time_limit_s: float
    attempt: int = 1                # 1-based; >1 means previous answer(s) were illegal
    previous_error: Optional[str] = None
    ascii_board: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MoveResponse:
    move: str = ""                  # UCI ("e2e4", "e7e8q") or SAN ("Nf3", "O-O"); both accepted
    resign: bool = False
    comment: Optional[str] = None   # free-form reasoning shown in the GUI (truncated when stored)
    usage: dict[str, Any] = field(default_factory=dict)  # e.g. {"input_tokens":..,"output_tokens":..,"cost_usd":..}


@dataclass
class MoveAttempt:
    move: str
    error: str
    elapsed_s: float


@dataclass
class MoveRecord:
    ply: int
    color: str
    uci: str
    san: str
    fen_after: str
    elapsed_s: float                # wall time of the successful attempt
    total_elapsed_s: float          # including illegal attempts
    illegal_attempts: list[MoveAttempt] = field(default_factory=list)
    comment: Optional[str] = None
    usage: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GameRecord:
    id: str
    white_id: str
    black_id: str
    tournament_id: Optional[str] = None
    round: int = 0
    opening: Optional[Opening] = None
    initial_fen: str = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    status: GameStatus = GameStatus.SCHEDULED
    result: Optional[str] = None            # "1-0" | "0-1" | "1/2-1/2"
    termination: Optional[Termination] = None
    termination_detail: Optional[str] = None
    moves: list[MoveRecord] = field(default_factory=list)
    pgn: Optional[str] = None
    config: GameConfig = field(default_factory=GameConfig)
    created_at: float = field(default_factory=now)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    ply_count: Optional[int] = None         # set by list queries that don't load moves
    # The move that was being played when the game ended without that move being made
    # (illegal-move forfeit, timeout, resignation, error, abort): the illegal attempts and
    # token usage of that move would otherwise be lost. {player_id, color, ply, attempt,
    # illegal_attempts: [MoveAttempt dicts], usage: {...}, elapsed_s}.
    final_attempt: Optional[dict[str, Any]] = None

    def to_dict(self, include_moves: bool = True) -> dict[str, Any]:
        d = {
            "id": self.id,
            "white_id": self.white_id,
            "black_id": self.black_id,
            "tournament_id": self.tournament_id,
            "round": self.round,
            "opening": self.opening.to_dict() if self.opening else None,
            "initial_fen": self.initial_fen,
            "status": self.status.value,
            "result": self.result,
            "termination": self.termination.value if self.termination else None,
            "termination_detail": self.termination_detail,
            "pgn": self.pgn,
            "config": self.config.to_dict(),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ply_count": len(self.moves) if self.moves or self.ply_count is None else self.ply_count,
            "final_attempt": self.final_attempt,
        }
        if include_moves:
            d["moves"] = [m.to_dict() for m in self.moves]
        return d


@dataclass
class TournamentConfig:
    player_ids: list[str]
    # "round_robin": everyone plays everyone.
    # "gauntlet": each of ``candidate_ids`` plays every other participant; non-candidates don't play each other.
    format: str = "round_robin"
    candidate_ids: list[str] = field(default_factory=list)
    games_per_pair: int = 2           # must be even when openings are used, so each opening is played with both colours
    openings: str = "builtin"         # "builtin" (balanced suite in agentchess/openings.py) | "none" (start position)
    concurrency: int = 4              # max games running at once in this tournament
    wait_for_remote: bool = True      # don't start games whose remote agents are offline (instead of forfeiting)
    game: GameConfig = field(default_factory=GameConfig)
    seed: int = 0                     # deterministic opening selection / ordering

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TournamentConfig":
        d = dict(d)
        game = GameConfig.from_dict(d.pop("game", None))
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(game=game, **known)


@dataclass
class Tournament:
    id: str
    name: str
    config: TournamentConfig
    status: TournamentStatus = TournamentStatus.PENDING
    created_at: float = field(default_factory=now)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "config": self.config.to_dict(),
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def score_of(result: Optional[str], white: bool) -> Optional[float]:
    """Points scored by white (white=True) or black in a result string."""
    if result == "1-0":
        return 1.0 if white else 0.0
    if result == "0-1":
        return 0.0 if white else 1.0
    if result == "1/2-1/2":
        return 0.5
    return None
