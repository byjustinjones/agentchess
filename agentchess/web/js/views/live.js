// Live: grid of running games with mini boards updated from WebSocket events.
import { get } from "../api.js";
import { Subscriptions } from "../ws.js";
import { html, render, esc, enc, errorBanner, fmtResult, fmtTermination } from "../util.js";
import { Board, uciSquares, fenMoveInfo } from "../board.js";
import { loading } from "../components.js";

export default {
  mount(root) {
    document.title = "Live · agentchess";
    const subs = new Subscriptions();
    const games = new Map(); // id -> state
    let alive = true;
    const timers = [];

    render(root, html`
      <div class="page-head">
        <div><h1>Live games</h1><p class="subtitle">Boards update in real time. Click a game to follow it move by move.</p></div>
        <div class="filters"><a class="btn" href="#/play">New match</a></div>
      </div>
      <div id="live-error"></div>
      <div id="live-grid" class="live-grid">${loading("Loading live games")}</div>`);
    const grid = root.querySelector("#live-grid");

    function emptyState() {
      if (games.size === 0) {
        render(grid, html`<div class="empty empty-wide">No games are running right now — <a href="#/tournaments/new">create a tournament</a> or <a href="#/play">start a match</a>.</div>`);
      }
    }

    function addGame(g, names = {}) {
      if (!g || !g.id || games.has(g.id)) return;
      if (games.size === 0) grid.innerHTML = "";
      const moves = g.moves || [];
      const last = moves[moves.length - 1];
      const st = {
        game: g,
        whiteName: g.white_name || names.white_name || g.white_id,
        blackName: g.black_name || names.black_name || g.black_id,
        fen: last ? last.fen_after : g.initial_fen,
        lastUci: last ? last.uci : null,
        lastSan: last ? last.san : null,
        thinking: null,
        finished: false,
      };
      const card = document.createElement("article");
      card.className = "live-card";
      card.dataset.game = g.id;
      render(card, html`
        <a class="live-link" href="#/game/${enc(g.id)}" aria-label="Watch ${st.whiteName} vs ${st.blackName}">
          <div class="live-player" data-side="black"><span class="side-dot black" aria-hidden="true"></span><span class="pname">${st.blackName}</span><span class="thinking" hidden><span class="pulse" aria-hidden="true"></span>thinking… <span class="secs"></span></span></div>
          <div class="live-board"></div>
          <div class="live-player" data-side="white"><span class="side-dot white" aria-hidden="true"></span><span class="pname">${st.whiteName}</span><span class="thinking" hidden><span class="pulse" aria-hidden="true"></span>thinking… <span class="secs"></span></span></div>
          <div class="live-meta"><span class="live-move"></span><span class="live-last"></span></div>
          <div class="live-sub muted">${g.opening ? g.opening.name : "Start position"}${g.tournament_id ? " · tournament" : " · exhibition"}</div>
          <div class="live-flash" role="status" hidden></div>
        </a>`);
      grid.appendChild(card);
      st.card = card;
      st.board = new Board(card.querySelector(".live-board"), { mini: true, coords: false });
      games.set(g.id, st);
      paint(st);
    }

    function paint(st) {
      const check = st.lastSan && /[+#]$/.test(st.lastSan);
      st.board.set(st.fen, { last: uciSquares(st.lastUci), check });
      const info = fenMoveInfo(st.fen);
      const moveEl = st.card.querySelector(".live-move");
      moveEl.textContent = st.finished ? "" : `Move ${info.moveNumber} · ${info.turn === "w" ? "White" : "Black"} to move`;
      st.card.querySelector(".live-last").textContent = st.lastSan ? `last: ${st.lastSan}` : "";
      for (const side of ["white", "black"]) {
        const el = st.card.querySelector(`.live-player[data-side=${side}] .thinking`);
        const pid = side === "white" ? st.game.white_id : st.game.black_id;
        el.hidden = !(st.thinking && st.thinking.player_id === pid);
      }
      tick(st);
    }

    function tick(st) {
      if (!st.thinking) return;
      const secs = Math.floor((Date.now() - st.thinking.since) / 1000);
      st.card.querySelectorAll(".thinking .secs").forEach((s) => { s.textContent = secs >= 2 ? `${secs}s` : ""; });
    }

    function flash(st, text, cls) {
      const el = st.card.querySelector(".live-flash");
      el.textContent = text;
      el.className = `live-flash ${cls}`;
      el.hidden = false;
      st.card.classList.remove("flash-illegal");
      void st.card.offsetWidth; // restart animation
      if (cls === "flash-bad") st.card.classList.add("flash-illegal");
      clearTimeout(st.flashTimer);
      st.flashTimer = setTimeout(() => { el.hidden = true; st.card.classList.remove("flash-illegal"); }, 4000);
    }

    async function load() {
      try {
        const data = await get("live");
        if (!alive) return;
        root.querySelector("#live-error").innerHTML = "";
        const list = (data && data.games) || [];
        const ids = new Set(list.map((g) => g.id));
        for (const [id, st] of games) if (!ids.has(id) && !st.finished) { st.card.remove(); games.delete(id); }
        if (games.size === 0) grid.innerHTML = "";
        for (const g of list) {
          const st = games.get(g.id);
          if (!st) { addGame(g); continue; }
          const moves = g.moves || [];
          const last = moves[moves.length - 1];
          if (last) { st.fen = last.fen_after; st.lastUci = last.uci; st.lastSan = last.san; paint(st); }
        }
        emptyState();
      } catch (e) {
        if (!alive) return;
        render(root.querySelector("#live-error"), errorBanner(e, { retry: true }));
        if (grid.querySelector(".loading")) grid.innerHTML = "";
      }
    }

    subs.on("game_started", (e) => addGame(e.game, e));
    subs.on("move", (e) => {
      const st = games.get(e.game_id);
      if (!st) return;
      st.fen = e.fen;
      st.lastUci = e.uci;
      st.lastSan = e.san;
      st.thinking = null;
      paint(st);
    });
    subs.on("thinking", (e) => {
      const st = games.get(e.game_id);
      if (!st) return;
      st.thinking = { player_id: e.player_id, since: Date.now() };
      paint(st);
    });
    subs.on("illegal_move", (e) => {
      const st = games.get(e.game_id);
      if (!st) return;
      const who = e.player_id === st.game.white_id ? st.whiteName : e.player_id === st.game.black_id ? st.blackName : e.player_id;
      flash(st, `Illegal move by ${who}: “${e.move ?? ""}” — ${e.error || "rejected"} (attempt ${e.attempt ?? "?"})`, "flash-bad");
    });
    subs.on("game_finished", (e) => {
      const g = e.game;
      const st = g && games.get(g.id);
      if (!st) return;
      st.finished = true;
      st.thinking = null;
      paint(st);
      st.card.classList.add("finished");
      flash(st, `Finished ${fmtResult(g.result)} · ${fmtTermination(g.termination)}`, "flash-done");
      st.card.querySelector(".live-move").innerHTML = `<strong>${esc(fmtResult(g.result))}</strong>`;
      setTimeout(() => {
        if (!alive || !games.has(g.id)) return;
        st.card.remove();
        games.delete(g.id);
        emptyState();
      }, 8000);
    });
    subs.on("_reconnect", load);
    root.addEventListener("click", (e) => { if (e.target.closest("[data-action=retry]")) load(); });

    timers.push(setInterval(() => games.forEach(tick), 1000));
    load();
    return () => { alive = false; subs.clear(); timers.forEach(clearInterval); games.forEach((st) => clearTimeout(st.flashTimer)); };
  },
};
