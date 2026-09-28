// Game view: large board, move list navigation, per-move details, live updates.
import { get, apiUrl } from "../api.js";
import { Subscriptions } from "../ws.js";
import {
  html, render, enc, errorBanner, debounce, fmtResult, fmtTermination, fmtSecs, fmtNum, fmtCost,
  fmtDate, statusBadge, copyText, toast,
} from "../util.js";
import { Board, uciSquares, parseFen } from "../board.js";
import { loading } from "../components.js";

const isBook = (m) => m && ((m.usage && m.usage.book) || m.comment === "book");

export default {
  mount(root, [gameId]) {
    document.title = "Game · agentchess";
    const subs = new Subscriptions();
    let alive = true;
    let game = null;
    let moves = [];
    let idx = 0; // number of plies shown on the board
    let flipped = false;
    let pendingIllegal = []; // illegal attempts for the move currently being thought about (live)
    let thinking = null; // {player_id, since}
    let board = null;

    render(root, html`<div id="game-error"></div><div id="game-body">${loading("Loading game")}</div>`);

    function colorOf(pid) {
      if (!game) return null;
      return pid === game.white_id ? "white" : pid === game.black_id ? "black" : null;
    }

    function layout() {
      const g = game;
      document.title = `${g.white_name || g.white_id} vs ${g.black_name || g.black_id} · agentchess`;
      render(root.querySelector("#game-body"), html`
        <div class="page-head game-head">
          <div>
            <p class="crumbs">${g.tournament_id ? html`<a href="#/tournament/${enc(g.tournament_id)}">Tournament</a> · round ${g.round}` : html`<a href="#/live">Live</a> · exhibition`}</p>
            <h1 class="game-title">${g.white_name || g.white_id} <span class="vs">vs</span> ${g.black_name || g.black_id}</h1>
            <p class="subtitle" id="game-sub"></p>
          </div>
          <div class="filters">
            <a class="btn" href="${apiUrl(`games/${enc(g.id)}/pgn`)}" download="${`${g.id}.pgn`}">Download PGN</a>
          </div>
        </div>
        <div class="game-layout">
          <div class="game-board-col">
            <div class="player-bar" data-pos="top"></div>
            <div class="board-wrap"><div id="game-board"></div></div>
            <div class="player-bar" data-pos="bottom"></div>
            <div class="board-controls" role="toolbar" aria-label="Board navigation">
              <button type="button" class="btn btn-icon-txt" data-nav="start" aria-label="First position (Home)" title="First position (Home)">⏮</button>
              <button type="button" class="btn btn-icon-txt" data-nav="prev" aria-label="Previous move (Left arrow)" title="Previous move (←)">◀</button>
              <button type="button" class="btn btn-icon-txt" data-nav="next" aria-label="Next move (Right arrow)" title="Next move (→)">▶</button>
              <button type="button" class="btn btn-icon-txt" data-nav="end" aria-label="Last position (End)" title="Last position (End)">⏭</button>
              <span class="spacer"></span>
              <button type="button" class="btn" data-act="flip" aria-pressed="false">Flip</button>
              <button type="button" class="btn" data-act="fen">Copy FEN</button>
            </div>
            <p class="fen-line muted" id="fen-line"></p>
          </div>
          <div class="game-side-col">
            <section class="card card-tight" aria-labelledby="moves-title">
              <div class="card-head"><h2 id="moves-title">Moves</h2><span class="muted" id="ply-info"></span></div>
              <div class="move-list" id="move-list" tabindex="0" aria-label="Move list"></div>
            </section>
            <section class="card card-tight" aria-labelledby="detail-title">
              <div class="card-head"><h2 id="detail-title">Details</h2></div>
              <div id="move-detail"></div>
            </section>
          </div>
        </div>`);
      board = new Board(root.querySelector("#game-board"), { flipped });
    }

    function paintHeader() {
      const g = game;
      const sub = root.querySelector("#game-sub");
      const parts = [];
      parts.push(statusBadge(g.status).toString());
      if (g.result) parts.push(`<strong class="result-big">${fmtResult(g.result)}</strong>`);
      if (g.termination) parts.push(`<span>${html`${fmtTermination(g.termination)}${g.termination_detail ? ` — ${g.termination_detail}` : ""}`}</span>`);
      parts.push(`<span>${html`${g.opening ? `${g.opening.eco ? `${g.opening.eco} ` : ""}${g.opening.name}` : "Start position"}`}</span>`);
      sub.innerHTML = parts.join('<span class="sep" aria-hidden="true">·</span>');
    }

    function playerBar(side) {
      const g = game;
      const pid = side === "white" ? g.white_id : g.black_id;
      const name = side === "white" ? g.white_name || g.white_id : g.black_name || g.black_id;
      let score = "";
      if (g.result === "1-0") score = side === "white" ? "1" : "0";
      else if (g.result === "0-1") score = side === "white" ? "0" : "1";
      else if (g.result === "1/2-1/2") score = "½";
      const fen = idx === 0 ? g.initial_fen : moves[idx - 1].fen_after;
      const p = parseFen(fen);
      const toMove = g.status === "running" && idx === moves.length && p && ((p.turn === "w") === (side === "white"));
      const isThinking = thinking && thinking.player_id === pid && idx === moves.length;
      return html`<span class="side-dot ${side}" aria-hidden="true"></span>
        <a class="pname" href="#/player/${enc(pid)}">${name}</a>
        <span class="muted side-label">${side}</span>
        ${isThinking ? html`<span class="thinking"><span class="pulse" aria-hidden="true"></span>thinking… <span class="secs"></span></span>` : toMove ? html`<span class="to-move">to move</span>` : ""}
        ${score ? html`<span class="score-pill">${score}</span>` : ""}`;
    }

    function paintBoard() {
      const g = game;
      const fen = idx === 0 ? g.initial_fen : moves[idx - 1].fen_after;
      const m = idx > 0 ? moves[idx - 1] : null;
      board.set(fen, { last: m ? uciSquares(m.uci) : null, check: !!(m && /[+#]$/.test(m.san || "")) });
      const top = flipped ? "white" : "black";
      const bottom = flipped ? "black" : "white";
      render(root.querySelector('.player-bar[data-pos="top"]'), playerBar(top));
      render(root.querySelector('.player-bar[data-pos="bottom"]'), playerBar(bottom));
      root.querySelector("#fen-line").textContent = fen;
      tickThinking();
    }

    function paintMoves() {
      const list = root.querySelector("#move-list");
      if (!moves.length) {
        list.innerHTML = `<div class="empty small">${game.status === "scheduled" ? "Game has not started yet." : "No moves yet."}</div>`;
        root.querySelector("#ply-info").textContent = "";
        return;
      }
      const start = parseFen(game.initial_fen) || { fullmove: 1, turn: "w" };
      let num = start.fullmove;
      const rows = [];
      let i = 0;
      const cell = (k) => {
        const m = moves[k];
        const ill = Array.isArray(m.illegal_attempts) ? m.illegal_attempts.length : Number(m.illegal_attempts) || 0;
        return html`<button type="button" class="mv${k + 1 === idx ? " active" : ""}${isBook(m) ? " book" : ""}" data-ply="${k + 1}" aria-current="${k + 1 === idx ? "true" : "false"}">${m.san || m.uci}${ill ? html`<span class="ill-badge" title="${ill} illegal attempt${ill > 1 ? "s" : ""}">!${ill > 1 ? ill : ""}</span>` : ""}</button>`;
      };
      if (start.turn === "b" && moves.length) {
        rows.push(html`<span class="mv-num">${num}.</span><span class="mv-empty">…</span>${cell(0)}`);
        i = 1;
        num += 1;
      }
      for (; i < moves.length; i += 2) {
        rows.push(html`<span class="mv-num">${num}.</span>${cell(i)}${i + 1 < moves.length ? cell(i + 1) : html`<span class="mv-empty"></span>`}`);
        num += 1;
      }
      let tail = "";
      if (game.result) tail = html`<div class="mv-result">${fmtResult(game.result)}</div>`;
      render(list, html`<div class="mv-grid">${rows}</div>${tail}`);
      root.querySelector("#ply-info").textContent = `${moves.length} ply`;
      const active = list.querySelector(".mv.active");
      if (active) {
        const lr = list.getBoundingClientRect();
        const ar = active.getBoundingClientRect();
        if (ar.top < lr.top || ar.bottom > lr.bottom) list.scrollTop += ar.top - lr.top - lr.height / 2 + ar.height / 2;
      } else if (idx === 0) {
        list.scrollTop = 0;
      }
    }

    function usageBlock(u) {
      if (!u || typeof u !== "object") return "";
      const keys = Object.keys(u).filter((k) => k !== "book");
      if (!keys.length) return "";
      const known = [];
      if (u.input_tokens != null) known.push(html`<div><dt>Input tokens</dt><dd>${fmtNum(u.input_tokens)}</dd></div>`);
      if (u.output_tokens != null) known.push(html`<div><dt>Output tokens</dt><dd>${fmtNum(u.output_tokens)}</dd></div>`);
      if (u.cost_usd != null) known.push(html`<div><dt>Cost</dt><dd>${fmtCost(u.cost_usd)}</dd></div>`);
      const other = keys.filter((k) => !["input_tokens", "output_tokens", "cost_usd"].includes(k));
      for (const k of other) {
        const v = u[k];
        known.push(html`<div><dt>${k.replace(/_/g, " ")}</dt><dd>${typeof v === "object" ? JSON.stringify(v) : String(v)}</dd></div>`);
      }
      return known;
    }

    function illegalList(list) {
      if (!list || !list.length) return "";
      return html`<div class="illegal-list"><h3>Illegal attempts (${list.length})</h3><ol>${list.map((a) => html`<li>
        <code>${a.move === "" || a.move == null ? "(empty)" : a.move}</code> — <span>${a.error || "rejected"}</span>${a.elapsed_s != null ? html` <span class="muted">(${fmtSecs(a.elapsed_s)})</span>` : ""}
      </li>`)}</ol></div>`;
    }

    function paintDetail() {
      const el = root.querySelector("#move-detail");
      const g = game;
      if (idx === 0) {
        const c = g.config || {};
        render(el, html`<dl class="kv">
          <div><dt>Status</dt><dd>${statusBadge(g.status)}</dd></div>
          <div><dt>Opening</dt><dd>${g.opening ? `${g.opening.name}${g.opening.eco ? ` (${g.opening.eco})` : ""}` : "Start position"}</dd></div>
          <div><dt>Started</dt><dd>${fmtDate(g.started_at)}</dd></div>
          <div><dt>Finished</dt><dd>${fmtDate(g.finished_at)}</dd></div>
          <div><dt>Move timeout</dt><dd>${c.move_timeout_s != null ? fmtSecs(c.move_timeout_s) : "–"}</dd></div>
          <div><dt>Illegal attempts allowed</dt><dd>${c.max_illegal_attempts ?? "–"}</dd></div>
          <div><dt>Max plies</dt><dd>${c.max_plies ?? "–"}</dd></div>
          <div><dt>Legal moves shown</dt><dd>${c.show_legal_moves === false ? "no" : "yes"}</dd></div>
        </dl>
        ${moves.length ? html`<p class="muted small">Select a move (or use ← → Home End) to see its details.</p>` : ""}
        ${livePending()}`);
        return;
      }
      const m = moves[idx - 1];
      const ill = Array.isArray(m.illegal_attempts) ? m.illegal_attempts : [];
      const illCount = Array.isArray(m.illegal_attempts) ? m.illegal_attempts.length : Number(m.illegal_attempts) || 0;
      const who = m.color === "white" ? g.white_name || g.white_id : g.black_name || g.black_id;
      const comment = m.comment && m.comment !== "book" ? m.comment : "";
      render(el, html`<div class="move-head">
          <span class="side-dot ${m.color}" aria-hidden="true"></span>
          <strong class="move-san">${Math.floor((idx - 1 + (parseFen(g.initial_fen)?.turn === "b" ? 1 : 0)) / 2) + (parseFen(g.initial_fen)?.fullmove || 1)}${m.color === "white" ? "." : "…"} ${m.san}</strong>
          <span class="muted">${m.uci} · ${who}</span>
          ${isBook(m) ? html`<span class="badge">book</span>` : ""}
        </div>
        <dl class="kv">
          <div><dt>Think time</dt><dd>${fmtSecs(m.elapsed_s)}</dd></div>
          ${m.total_elapsed_s != null && Math.abs(m.total_elapsed_s - m.elapsed_s) > 0.005 ? html`<div><dt>Total incl. retries</dt><dd>${fmtSecs(m.total_elapsed_s)}</dd></div>` : ""}
          <div><dt>Illegal attempts</dt><dd class="${illCount ? "text-bad" : ""}">${illCount}</dd></div>
          ${usageBlock(m.usage)}
        </dl>
        ${illegalList(ill)}
        ${comment ? html`<h3 class="detail-h">Reasoning / comment</h3><pre class="comment">${comment}</pre>` : isBook(m) ? html`<p class="muted small">Book move from the opening suite (not played by the model).</p>` : html`<p class="muted small">No comment for this move.</p>`}
        ${idx === moves.length ? livePending() : ""}`);
    }

    function livePending() {
      if (!game || game.status !== "running" || !pendingIllegal.length) return "";
      return html`<div class="pending-illegal">${illegalList(pendingIllegal)}<p class="muted small">…on the move currently being played.</p></div>`;
    }

    function tickThinking() {
      if (!thinking) return;
      const s = Math.floor((Date.now() - thinking.since) / 1000);
      root.querySelectorAll(".player-bar .thinking .secs").forEach((e) => { e.textContent = s >= 1 ? `${s}s` : ""; });
    }

    function goto(n) {
      if (!game) return;
      idx = Math.max(0, Math.min(moves.length, n));
      paintBoard();
      paintMoves();
      paintDetail();
    }

    function paintAll() {
      paintHeader();
      paintBoard();
      paintMoves();
      paintDetail();
    }

    async function load(first = false) {
      try {
        const g = await get(`games/${enc(gameId)}`);
        if (!alive) return;
        root.querySelector("#game-error").innerHTML = "";
        const following = first || idx >= moves.length;
        const wasLayout = !!game;
        game = g;
        moves = (g.moves || []).slice().sort((a, b) => a.ply - b.ply);
        if (g.status !== "running") { thinking = null; pendingIllegal = []; }
        if (!wasLayout) layout();
        idx = following ? moves.length : Math.min(idx, moves.length);
        paintAll();
      } catch (e) {
        if (!alive) return;
        if (!game) root.querySelector("#game-body").innerHTML = "";
        render(root.querySelector("#game-error"), errorBanner(e, { retry: true }));
      }
    }
    const refetch = debounce(() => load(false), 400);

    // ---- events
    subs.on("move", (e) => {
      if (!game || e.game_id !== game.id) return;
      const following = idx >= moves.length;
      const rec = {
        ply: e.ply, color: e.color, uci: e.uci, san: e.san, fen_after: e.fen,
        elapsed_s: e.elapsed_s, total_elapsed_s: e.elapsed_s,
        illegal_attempts: pendingIllegal.length ? pendingIllegal.slice() : [],
        comment: e.comment, usage: {},
      };
      const at = moves.findIndex((m) => m.ply === e.ply);
      if (at >= 0) moves[at] = { ...moves[at], ...rec }; else moves.push(rec);
      moves.sort((a, b) => a.ply - b.ply);
      pendingIllegal = [];
      thinking = null;
      if (game.status === "scheduled") game.status = "running";
      if (following) idx = moves.length;
      paintBoard();
      paintMoves();
      paintDetail();
      refetch();
    });
    subs.on("thinking", (e) => {
      if (!game || e.game_id !== game.id) return;
      thinking = { player_id: e.player_id, since: Date.now() };
      paintBoard();
    });
    subs.on("illegal_move", (e) => {
      if (!game || e.game_id !== game.id) return;
      pendingIllegal.push({ move: e.move, error: e.error, attempt: e.attempt });
      const col = colorOf(e.player_id);
      toast(`Illegal move by ${col || e.player_id}: “${e.move ?? ""}” — ${e.error || "rejected"}`, "warn");
      if (idx >= moves.length) paintDetail();
    });
    subs.on("game_started", (e) => { if (game && e.game && e.game.id === game.id) load(false); });
    subs.on("game_finished", (e) => {
      if (!game || !e.game || e.game.id !== game.id) return;
      thinking = null;
      pendingIllegal = [];
      load(false);
    });
    subs.on("_reconnect", () => load(false));

    // ---- controls
    const onClick = async (e) => {
      if (e.target.closest("[data-action=retry]")) { load(true); return; }
      const mv = e.target.closest(".mv[data-ply]");
      if (mv) { goto(Number(mv.dataset.ply)); return; }
      const nav = e.target.closest("[data-nav]");
      if (nav && game && root.contains(nav) && nav.closest(".board-controls")) {
        const a = nav.dataset.nav;
        goto(a === "start" ? 0 : a === "end" ? moves.length : a === "prev" ? idx - 1 : idx + 1);
        return;
      }
      const act = e.target.closest("[data-act]");
      if (!act || !game) return;
      if (act.dataset.act === "flip") {
        flipped = !flipped;
        act.setAttribute("aria-pressed", String(flipped));
        board.flip(flipped);
        paintBoard();
      } else if (act.dataset.act === "fen") {
        const fen = idx === 0 ? game.initial_fen : moves[idx - 1].fen_after;
        toast((await copyText(fen)) ? "FEN copied to clipboard" : "Could not copy — FEN is shown below the board", "info");
      }
    };
    root.addEventListener("click", onClick);
    const onKey = (e) => {
      if (!game || e.altKey || e.ctrlKey || e.metaKey) return;
      const t = e.target;
      if (t && (t.closest("input, textarea, select, [contenteditable=true]") || t.closest("dialog"))) return;
      const map = { ArrowLeft: idx - 1, ArrowRight: idx + 1, Home: 0, End: moves.length };
      if (!(e.key in map)) return;
      e.preventDefault();
      goto(map[e.key]);
    };
    document.addEventListener("keydown", onKey);
    const timer = setInterval(tickThinking, 1000);

    load(true);
    return () => {
      alive = false;
      subs.clear();
      refetch.cancel();
      clearInterval(timer);
      root.removeEventListener("click", onClick);
      document.removeEventListener("keydown", onKey);
    };
  },
};
