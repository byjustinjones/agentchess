// Minimal chessboard: FEN piece-placement parser + CSS-grid renderer with Unicode glyphs.

export const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

// Solid glyphs for both colours (styled via CSS); U+FE0E forces text (non-emoji) presentation.
const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const NAMES = { k: "king", q: "queen", r: "rook", b: "bishop", n: "knight", p: "pawn" };
const FILES = "abcdefgh";

/**
 * Parse a FEN (only the placement field is required).
 * Returns {rows: 8 arrays of 8 (piece char|null), rank 8 first}, turn, fullmove} or null if invalid.
 */
export function parseFen(fen) {
  if (typeof fen !== "string") return null;
  const parts = fen.trim().split(/\s+/);
  const ranks = (parts[0] || "").split("/");
  if (ranks.length !== 8) return null;
  const rows = [];
  for (const r of ranks) {
    const row = [];
    for (const ch of r) {
      if (/[1-8]/.test(ch)) {
        for (let i = 0; i < Number(ch); i++) row.push(null);
      } else if (/[prnbqkPRNBQK]/.test(ch)) {
        row.push(ch);
      } else {
        return null;
      }
    }
    if (row.length !== 8) return null;
    rows.push(row);
  }
  return {
    rows,
    turn: parts[1] === "b" ? "b" : "w",
    fullmove: Number(parts[5]) || 1,
  };
}

/** "e2e4" -> {from:"e2", to:"e4"} (null for anything else). */
export function uciSquares(uci) {
  if (typeof uci !== "string" || !/^[a-h][1-8][a-h][1-8]/.test(uci)) return null;
  return { from: uci.slice(0, 2), to: uci.slice(2, 4) };
}

/** Human "Move 12 · Black to move" style info derived from a FEN. */
export function fenMoveInfo(fen) {
  const p = parseFen(fen);
  if (!p) return { moveNumber: 1, turn: "w" };
  return { moveNumber: p.fullmove, turn: p.turn };
}

export class Board {
  /**
   * @param {HTMLElement} el container
   * @param {{flipped?: boolean, coords?: boolean, mini?: boolean}} opts
   */
  constructor(el, opts = {}) {
    this.el = el;
    this.flipped = !!opts.flipped;
    this.coords = opts.coords !== false;
    this.fen = START_FEN;
    this.last = null;
    this.check = false;
    el.classList.add("board");
    if (opts.mini) el.classList.add("board-mini");
    el.setAttribute("role", "img");
    this.draw();
  }

  set(fen, { last = null, check = false } = {}) {
    this.fen = fen || START_FEN;
    this.last = last;
    this.check = check;
    this.draw();
  }

  flip(v) {
    this.flipped = v === undefined ? !this.flipped : !!v;
    this.draw();
  }

  draw() {
    const parsed = parseFen(this.fen) || parseFen(START_FEN);
    const sideKing = parsed.turn === "w" ? "K" : "k";
    const parts = [];
    for (let vr = 0; vr < 8; vr++) {
      for (let vf = 0; vf < 8; vf++) {
        const r = this.flipped ? 7 - vr : vr; // row index into parsed.rows (0 = rank 8)
        const f = this.flipped ? 7 - vf : vf;
        const rank = 8 - r;
        const sq = FILES[f] + rank;
        const light = (r + f) % 2 === 0;
        const piece = parsed.rows[r][f];
        let cls = `sq ${light ? "sq-l" : "sq-d"}`;
        if (this.last && (this.last.from === sq || this.last.to === sq)) cls += " sq-last";
        if (this.check && piece === sideKing) cls += " sq-check";
        let inner = "";
        if (piece) {
          const white = piece === piece.toUpperCase();
          const t = piece.toLowerCase();
          inner += `<span class="pc ${white ? "pc-w" : "pc-b"}" aria-hidden="true">${GLYPH[t]}︎</span>`;
        }
        if (this.coords) {
          if (vf === 0) inner += `<span class="co co-r">${rank}</span>`;
          if (vr === 7) inner += `<span class="co co-f">${FILES[f]}</span>`;
        }
        parts.push(`<div class="${cls}" data-sq="${sq}">${inner}</div>`);
      }
    }
    this.el.innerHTML = parts.join("");
    this.el.setAttribute("aria-label", `Chess position ${describe(parsed)}`);
    this.el.dataset.fen = this.fen;
  }
}

function describe(parsed) {
  const w = [];
  const b = [];
  parsed.rows.forEach((row, r) => row.forEach((p, f) => {
    if (!p) return;
    const s = `${NAMES[p.toLowerCase()]} ${FILES[f]}${8 - r}`;
    (p === p.toUpperCase() ? w : b).push(s);
  }));
  return `(${parsed.turn === "w" ? "white" : "black"} to move). White: ${w.join(", ")}. Black: ${b.join(", ")}.`;
}
