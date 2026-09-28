"""Built-in suite of short, roughly balanced opening lines.

Tournaments play each opening twice with colours reversed, which removes most
of the opening bias and adds variety between games of deterministic players
(engines). All lines start from the standard position; a test verifies legality.
"""
from __future__ import annotations

from typing import Optional

from agentchess.models import Opening


def _o(id: str, name: str, eco: str, moves: str) -> Opening:
    return Opening(id=id, name=name, eco=eco, moves_uci=moves.split())


BUILTIN_OPENINGS: list[Opening] = [
    # --- 1.e4 e5
    _o("ruy-lopez", "Ruy Lopez: Morphy Defence", "C77", "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6"),
    _o("italian-game", "Italian Game: Giuoco Piano", "C54", "e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 c2c3 g8f6"),
    _o("scotch-game", "Scotch Game", "C45", "e2e4 e7e5 g1f3 b8c6 d2d4 e5d4 f3d4 g8f6"),
    _o("four-knights", "Four Knights Game", "C47", "e2e4 e7e5 g1f3 b8c6 b1c3 g8f6"),
    _o("petrov", "Petrov's Defence", "C42", "e2e4 e7e5 g1f3 g8f6 f3e5 d7d6 e5f3 f6e4"),
    # --- Sicilian
    _o("sicilian-open-d6", "Sicilian Defence: Open, 2...d6", "B54", "e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6"),
    _o("sicilian-taimanov", "Sicilian Defence: Taimanov", "B44", "e2e4 c7c5 g1f3 e7e6 d2d4 c5d4 f3d4 b8c6"),
    _o("sicilian-alapin", "Sicilian Defence: Alapin", "B22", "e2e4 c7c5 c2c3 g8f6 e4e5 f6d5"),
    # --- other 1.e4
    _o("french-winawer", "French Defence: Winawer", "C17", "e2e4 e7e6 d2d4 d7d5 b1c3 f8b4 e4e5 c7c5"),
    _o("french-tarrasch", "French Defence: Tarrasch", "C05", "e2e4 e7e6 d2d4 d7d5 b1d2 g8f6 e4e5 f6d7"),
    _o("caro-kann-classical", "Caro-Kann Defence: Classical", "B18", "e2e4 c7c6 d2d4 d7d5 b1c3 d5e4 c3e4 c8f5"),
    _o("caro-kann-advance", "Caro-Kann Defence: Advance", "B12", "e2e4 c7c6 d2d4 d7d5 e4e5 c8f5"),
    _o("scandinavian", "Scandinavian Defence: Main Line", "B01", "e2e4 d7d5 e4d5 d8d5 b1c3 d5a5"),
    # --- 1.d4 d5
    _o("queens-gambit-declined", "Queen's Gambit Declined", "D53", "d2d4 d7d5 c2c4 e7e6 b1c3 g8f6 c1g5 f8e7"),
    _o("slav", "Slav Defence", "D15", "d2d4 d7d5 c2c4 c7c6 g1f3 g8f6 b1c3 d5c4"),
    _o("queens-gambit-accepted", "Queen's Gambit Accepted", "D25", "d2d4 d7d5 c2c4 d5c4 g1f3 g8f6 e2e3 e7e6"),
    _o("london", "London System", "D02", "d2d4 d7d5 c1f4 g8f6 e2e3 c7c5 c2c3 b8c6"),
    _o("catalan", "Catalan Opening: Closed", "E01", "d2d4 g8f6 c2c4 e7e6 g2g3 d7d5 f1g2 f8e7"),
    # --- Indian defences
    _o("nimzo-indian", "Nimzo-Indian Defence: Rubinstein", "E46", "d2d4 g8f6 c2c4 e7e6 b1c3 f8b4 e2e3 e8g8"),
    _o("queens-indian", "Queen's Indian Defence", "E15", "d2d4 g8f6 c2c4 e7e6 g1f3 b7b6 g2g3 c8b7"),
    _o("kings-indian", "King's Indian Defence", "E70", "d2d4 g8f6 c2c4 g7g6 b1c3 f8g7 e2e4 d7d6"),
    _o("grunfeld", "Grünfeld Defence: Exchange", "D85", "d2d4 g8f6 c2c4 g7g6 b1c3 d7d5 c4d5 f6d5"),
    # --- flank openings
    _o("english-symmetrical", "English Opening: Symmetrical", "A36", "c2c4 c7c5 b1c3 b8c6 g2g3 g7g6 f1g2 f8g7"),
    _o("english-four-knights", "English Opening: Four Knights", "A28", "c2c4 e7e5 b1c3 g8f6 g1f3 b8c6"),
]

_BY_ID = {o.id: o for o in BUILTIN_OPENINGS}


def get_opening(opening_id: str) -> Optional[Opening]:
    """Return the built-in opening with this id (a fresh copy), or None."""
    o = _BY_ID.get(opening_id)
    return Opening(id=o.id, name=o.name, eco=o.eco, moves_uci=list(o.moves_uci)) if o else None
