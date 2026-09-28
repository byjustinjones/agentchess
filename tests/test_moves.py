import chess
import pytest

from agentchess.moves import extract_move_text, find_move_in_text, parse_move, render_ascii


@pytest.mark.parametrize("text,uci", [
    ("e2e4", "e2e4"),
    ("E2E4", "e2e4"),
    ("e4", "e2e4"),
    ("  'e4'  ", "e2e4"),
    ("`Nf3`", "g1f3"),
    ("**Nf3**", "g1f3"),
    ("Nf3!?", "g1f3"),
    ("Nf3.", "g1f3"),
    ("1. e4", "e2e4"),
    ("1.e4", "e2e4"),
    ("nf3", "g1f3"),
    ("Ng1-f3", "g1f3"),
    ("g1f3", "g1f3"),
])
def test_parse_start_position(text, uci):
    assert parse_move(chess.Board(), text).uci() == uci


def test_parse_black_move_number_and_check_suffix():
    board = chess.Board()
    board.push_san("e4")
    assert parse_move(board, "1...e5").uci() == "e7e5"
    assert parse_move(board, "1... e5").uci() == "e7e5"
    board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 2")
    assert parse_move(board, "Qh4#").uci() == "d8h4"
    assert parse_move(board, "Qh4+").uci() == "d8h4"
    assert parse_move(board, "Qh4").uci() == "d8h4"


def test_castling_variants():
    board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    for text in ("O-O", "0-0", "o-o", "e1g1", "e1h1"):
        assert parse_move(board, text).uci() == "e1g1", text
    for text in ("O-O-O", "0-0-0", "e1c1"):
        assert parse_move(board, text).uci() == "e1c1", text


def test_captures_and_promotion():
    board = chess.Board("4k3/1P6/8/3p4/4P3/8/8/4K3 w - - 0 1")
    assert parse_move(board, "exd5").uci() == "e4d5"
    assert parse_move(board, "b8=Q").uci() == "b7b8q"
    assert parse_move(board, "b8Q").uci() == "b7b8q"
    assert parse_move(board, "b8=q").uci() == "b7b8q"
    assert parse_move(board, "b7b8n").uci() == "b7b8n"
    assert parse_move(board, "B7B8Q").uci() == "b7b8q"
    with pytest.raises(ValueError, match="promotion"):
        parse_move(board, "b7b8")
    with pytest.raises(ValueError, match="promotion"):
        parse_move(board, "b8")


def test_errors_are_helpful():
    board = chess.Board()
    with pytest.raises(ValueError, match="illegal move e2e5"):
        parse_move(board, "e2e5")
    with pytest.raises(ValueError, match="illegal move Nf6"):
        parse_move(board, "Nf6")
    with pytest.raises(ValueError, match="could not parse 'xyz'"):
        parse_move(board, "xyz")
    with pytest.raises(ValueError, match="no move"):
        parse_move(board, "   ")
    with pytest.raises(ValueError):
        parse_move(board, "0000")
    ambiguous = chess.Board("4k3/8/8/8/8/8/8/1N2KN2 w - - 0 1")
    with pytest.raises(ValueError, match="ambiguous"):
        parse_move(ambiguous, "Nd2")
    assert parse_move(ambiguous, "Nbd2").uci() == "b1d2"


def test_extract_move_text():
    assert extract_move_text("I think...\nMOVE: Nf3") == "Nf3"
    assert extract_move_text("MOVE: e4\nhmm, no\nmove: d4") == "d4"
    assert extract_move_text("**MOVE:** e2e4") == "e2e4"
    assert extract_move_text("**MOVE: Qxf7#**") == "Qxf7#"
    assert extract_move_text("Move: `O-O`.") == "O-O"
    assert extract_move_text("MOVE: 12... Nf6") == "Nf6"
    assert extract_move_text("Final answer -> MOVE: exd5") == "exd5"
    assert extract_move_text("I play e4") is None
    assert extract_move_text("After White's last move: e4, I consider Nf6") is None
    assert extract_move_text("- move: Nc6") == "Nc6"
    assert extract_move_text("") is None
    assert extract_move_text("MOVE:\nMOVE: ") is None


def test_find_move_in_text():
    board = chess.Board()
    assert find_move_in_text(board, "I considered Nf6 but I'll go with e4. Nothing else").uci() == "e2e4"
    assert find_move_in_text(board, "Best is d4, then maybe Nc3") .uci() == "b1c3"
    assert find_move_in_text(board, "no idea") is None
    assert find_move_in_text(board, "") is None


def test_render_ascii():
    text = render_ascii(chess.Board())
    lines = text.splitlines()
    assert lines[1] == "8 | r n b q k b n r |"
    assert lines[8] == "1 | R N B Q K B N R |"
    assert lines[-1].split() == list("abcdefgh")
