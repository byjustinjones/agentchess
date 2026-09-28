import chess

from agentchess.openings import BUILTIN_OPENINGS, get_opening


def test_openings_are_legal_and_unique():
    assert 20 <= len(BUILTIN_OPENINGS) <= 30
    ids = [o.id for o in BUILTIN_OPENINGS]
    assert len(ids) == len(set(ids))
    finals = set()
    for o in BUILTIN_OPENINGS:
        assert o.name and o.eco, o.id
        assert 4 <= len(o.moves_uci) <= 8, o.id
        board = chess.Board()
        for uci in o.moves_uci:
            move = chess.Move.from_uci(uci)
            assert move in board.legal_moves, (o.id, uci)
            board.push(move)
        assert board.outcome() is None
        finals.add(board.board_fen())
    assert len(finals) == len(BUILTIN_OPENINGS)  # no two lines reach the same position


def test_get_opening_returns_copy():
    o = get_opening("ruy-lopez")
    assert o is not None and o.eco.startswith("C")
    o.moves_uci.append("x")
    assert get_opening("ruy-lopez").moves_uci[-1] != "x"
    assert get_opening("nope") is None
