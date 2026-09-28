"""Move parsing and board rendering helpers.

``parse_move`` is deliberately tolerant about *notation* (UCI or SAN, move
numbers, quotes, markdown, annotations) but strict about *legality*: it only
ever returns a legal move of ``board`` and otherwise raises ``ValueError``
with a message suitable to show back to the player.
"""
from __future__ import annotations

import re
from typing import Optional

import chess

_UCI_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$", re.IGNORECASE)
_MOVE_NUMBER_RE = re.compile(r"^\d+\s*\.+\s*")
_STRIP_CHARS = " \t\r\n\"'`*_()[]{}<>"
_TRAILING_CHARS = ".,;:!?"
# "MOVE:" marker: any case at the start of a line (after markdown decoration), or
# anywhere in the line when written in capitals ("Final answer -> MOVE: e4"). A
# lowercase mid-sentence "move:" ("White's last move: e4") is prose, not an answer.
_MOVE_LINE_RE = re.compile(r"(?:^[\s>*_#`-]*(?i:move)|MOVE)\s*[*_]*\s*:\s*(.*)$")
_TOKEN_SPLIT_RE = re.compile(r"[\s,;()\[\]{}\"'`*]+")
# Cheap pre-filter for tokens that could be a move (avoids parsing every word).
_MOVE_LIKE_RE = re.compile(
    r"^(\d+\.+)?([KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](=?[QRBNqrbn])?|[a-h][1-8][a-h][1-8][qrbnQRBN]?|[O0o]-[O0o](-[O0o])?)"
    r"[+#!?.,;:]*$"
)


def _strip_decor(s: str) -> str:
    """Remove surrounding quotes/markdown and trailing punctuation until stable."""
    prev = None
    while prev != s:
        prev = s
        s = s.strip(_STRIP_CHARS).rstrip(_TRAILING_CHARS)
    return s


def _clean(text: str) -> str:
    s = _MOVE_NUMBER_RE.sub("", _strip_decor(text))
    # trailing annotations / check marks: "Nf3!?", "e4.", "Qh5+", "Qxf7#"
    return _strip_decor(_strip_decor(s).rstrip("+#"))


def _side(board: chess.Board) -> str:
    return "white" if board.turn == chess.WHITE else "black"


def parse_move(board: chess.Board, text: str) -> chess.Move:
    """Parse ``text`` (UCI or SAN) into a legal move of ``board``.

    Raises ``ValueError`` with a human-readable explanation otherwise.
    """
    if text is None:
        raise ValueError("no move given")
    raw = str(text)
    s = _clean(raw)
    if not s:
        raise ValueError("no move given" if not raw.strip() else f"could not parse {raw.strip()!r} as a move")
    if not any(board.legal_moves):
        raise ValueError("the game is over; there are no legal moves")

    # --- UCI ---------------------------------------------------------------
    if _UCI_RE.match(s):
        uci = s.lower()
        try:
            move = board.parse_uci(uci)  # normalises king-takes-rook castling (e1h1 -> e1g1)
        except ValueError:
            move = None
        if move and move in board.legal_moves:
            return move
        raw_move = chess.Move.from_uci(uci)
        if raw_move.promotion is None:
            piece = board.piece_at(raw_move.from_square)
            if piece and piece.piece_type == chess.PAWN and chess.square_rank(raw_move.to_square) in (0, 7):
                promo = chess.Move(raw_move.from_square, raw_move.to_square, chess.QUEEN)
                if promo in board.legal_moves:
                    raise ValueError(f"illegal move {uci}: a promotion piece is required (e.g. {promo.uci()})")
        raise ValueError(f"illegal move {uci} in this position ({_side(board)} to move)")

    # --- SAN ---------------------------------------------------------------
    san = s
    san = re.sub(r"^[0Oo]-[0Oo]-[0Oo]$", "O-O-O", san)
    san = re.sub(r"^[0Oo]-[0Oo]$", "O-O", san)
    san = re.sub(r"\s*(e\.?p\.?)$", "", san)
    candidates = [san]
    if san[:1] in "nbrqk" and len(san) >= 3:
        candidates.append(san[0].upper() + san[1:])    # "nf3" -> "Nf3"
    if re.search(r"=[qrbn]$", san) or re.search(r"[1-8][qrbn]$", san):
        candidates.append(san[:-1] + san[-1].upper())  # "e8=q" -> "e8=Q"

    first_error: Optional[ValueError] = None
    for cand in candidates:
        try:
            move = board.parse_san(cand)
        except chess.AmbiguousMoveError:
            raise ValueError(f"ambiguous move {cand}: specify the origin file or rank (e.g. Nbd2) or use UCI") from None
        except chess.IllegalMoveError:
            if first_error is None:
                first_error = ValueError(f"illegal move {cand} in this position ({_side(board)} to move)")
                if re.fullmatch(r"([a-h]x)?[a-h][18]", cand):
                    try:
                        board.parse_san(cand + "=Q")
                        first_error = ValueError(f"illegal move {cand}: a promotion piece is required (e.g. {cand}=Q)")
                    except ValueError:
                        pass
            continue
        except ValueError:
            continue
        if move == chess.Move.null() or move not in board.legal_moves:
            if first_error is None:
                first_error = ValueError(f"illegal move {cand} in this position ({_side(board)} to move)")
            continue
        return move
    if first_error is not None:
        raise first_error
    raise ValueError(f"could not parse {raw.strip()!r} as UCI (e.g. e2e4) or SAN (e.g. Nf3)")


def extract_move_text(reply: str) -> Optional[str]:
    """Return the move after the last ``MOVE:`` marker in an LLM reply (or None).

    Tolerates markdown (``**MOVE:** Nf3``, ``MOVE: `e2e4```) and move numbers
    (``MOVE: 12... Nf6``).
    """
    if not reply:
        return None
    for line in reversed(reply.splitlines()):
        matches = list(_MOVE_LINE_RE.finditer(line))
        if not matches:
            continue
        rest = _MOVE_NUMBER_RE.sub("", _strip_decor(matches[-1].group(1)))
        rest = _strip_decor(rest)
        if not rest:
            continue
        token = _strip_decor(rest.split()[0])
        if token:
            return token
    return None


def find_move_in_text(board: chess.Board, reply: str) -> Optional[chess.Move]:
    """Fallback: scan tokens from the end of ``reply`` for one that is a legal move."""
    if not reply:
        return None
    tokens = [t for t in _TOKEN_SPLIT_RE.split(reply) if t]
    for tok in reversed(tokens):
        if not _MOVE_LIKE_RE.match(tok):
            continue
        try:
            return parse_move(board, tok)
        except ValueError:
            continue
    return None


def render_ascii(board: chess.Board) -> str:
    """ASCII diagram with rank/file coordinates, White at the bottom.

    Uppercase letters are White pieces, lowercase Black, ``.`` empty squares.
    """
    lines = ["  +-----------------+"]
    for rank in range(7, -1, -1):
        row = []
        for file in range(8):
            piece = board.piece_at(chess.square(file, rank))
            row.append(piece.symbol() if piece else ".")
        lines.append(f"{rank + 1} | {' '.join(row)} |")
    lines.append("  +-----------------+")
    lines.append("    a b c d e f g h")
    return "\n".join(lines)
