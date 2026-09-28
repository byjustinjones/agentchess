"""RandomPlayer: uniformly random legal moves (the rating floor)."""
from __future__ import annotations

import random

import chess

from agentchess.models import MoveRequest, MoveResponse, PlayerSpec
from agentchess.players.base import Player


class RandomPlayer(Player):
    """Config: ``seed`` (optional int) makes the move sequence reproducible."""

    def __init__(self, spec: PlayerSpec) -> None:
        super().__init__(spec)
        self._rng = random.Random(spec.config.get("seed"))

    async def get_move(self, request: MoveRequest) -> MoveResponse:
        board = chess.Board(request.fen)
        moves = sorted(m.uci() for m in board.legal_moves)  # sorted: deterministic under a seed
        if not moves:
            raise RuntimeError("no legal moves in this position")
        return MoveResponse(move=self._rng.choice(moves))
