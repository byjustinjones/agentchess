// Tournaments: list, creation form, detail page (standings, crosstable, games).
import { get, post, del, apiUrl } from "../api.js";
import { Subscriptions } from "../ws.js";
import {
  html, render, esc, enc, errorBanner, debounce, fmtDate, timeAgo, statusBadge, kindBadge,
  configSummary, toast, confirmModal,
} from "../util.js";
import { standingsTable, progressBar, gamesTable, loading } from "../components.js";

const KINDS = ["engine", "llm", "remote", "random"];
const KIND_LABEL = { engine: "Engines", llm: "LLMs", remote: "Remote agents", random: "Random" };

// ================================================================ list
export const tournamentsView = {
  mount(root) {
    document.title = "Tournaments · agentchess";
    const subs = new Subscriptions();
    let alive = true;
    render(root, html`
      <div class="page-head">
        <div><h1>Tournaments</h1><p class="subtitle">Round robins and gauntlets between engines, LLMs and external agents.</p></div>
        <div class="filters"><a class="btn btn-primary" href="#/tournaments/new">New tournament</a></div>
      </div>
      <div id="t-error"></div>
      <div id="t-list">${loading("Loading tournaments")}</div>`);

    async function load() {
      try {
        const list = (await get("tournaments")) || [];
        if (!alive) return;
        root.querySelector("#t-error").innerHTML = "";
        const el = root.querySelector("#t-list");
        if (!list.length) {
          render(el, html`<div class="empty">No tournaments yet — <a href="#/tournaments/new">create a tournament</a> to start benchmarking.</div>`);
          return;
        }
        render(el, html`<div class="t-grid">${list.map((t) => {
          const c = t.config || {};
          const n = (c.player_ids || []).length;
          return html`<a class="card t-card" href="#/tournament/${enc(t.id)}">
            <div class="t-card-head"><h2>${t.name}</h2>${statusBadge(t.status)}</div>
            <p class="muted small">${c.format === "gauntlet" ? `Gauntlet · ${(c.candidate_ids || []).length} candidate(s)` : "Round robin"} · ${n} players · ${c.games_per_pair ?? "?"} games/pair · openings: ${c.openings || "–"}</p>
            ${progressBar(t.progress)}
            <p class="muted small">Created ${timeAgo(t.created_at)}${t.finished_at ? ` · finished ${timeAgo(t.finished_at)}` : ""}</p>
          </a>`;
        })}</div>`);
      } catch (e) {
        if (!alive) return;
        render(root.querySelector("#t-error"), errorBanner(e, { retry: true }));
        root.querySelector("#t-list").innerHTML = "";
      }
    }
    const refresh = debounce(load, 800);
    subs.on("tournament_updated", refresh).on("game_finished", refresh).on("game_started", refresh).on("_reconnect", load);
    root.addEventListener("click", (e) => { if (e.target.closest("[data-action=retry]")) load(); });
    load();
    return () => { alive = false; subs.clear(); refresh.cancel(); };
  },
};

// ================================================================ new
export const tournamentNewView = {
  mount(root) {
    document.title = "New tournament · agentchess";
    let alive = true;
    let players = [];
    let presets = [];
    const registered = new Map(); // preset id -> id of the player registered for it
    render(root, html`
      <div class="page-head">
        <div><p class="crumbs"><a href="#/tournaments">Tournaments</a></p><h1>New tournament</h1></div>
      </div>
      <div id="nt-error"></div>
      <div id="nt-body">${loading("Loading players")}</div>`);

    async function init() {
      try {
        const [pl, pr] = await Promise.all([get("players"), get("players/presets").catch(() => [])]);
        if (!alive) return;
        players = (pl || []).filter((p) => p.active !== false);
        const ids = new Set(players.map((p) => p.id));
        presets = (pr || []).filter((p) => p && p.id && !ids.has(p.id));
        draw();
      } catch (e) {
        if (!alive) return;
        root.querySelector("#nt-body").innerHTML = "";
        render(root.querySelector("#nt-error"), errorBanner(e, { retry: true }));
      }
    }

    function participantGroups() {
      const entries = [
        ...players.map((p) => ({ ...p, _preset: false })),
        ...presets.map((p) => ({ ...p, _preset: true })),
      ];
      return KINDS.concat(["other"]).map((k) => {
        const items = entries.filter((p) => (KINDS.includes(p.kind) ? p.kind === k : k === "other"));
        if (!items.length) return "";
        return html`<fieldset class="pgroup" data-kind="${k}">
          <legend>${KIND_LABEL[k] || "Other"} <span class="muted">(${items.length})</span></legend>
          <div class="pgroup-tools">
            <button type="button" class="btn btn-sm" data-select="${k}">All</button>
            <button type="button" class="btn btn-sm" data-unselect="${k}">None</button>
          </div>
          <div class="check-list">${items.map((p) => html`<label class="check">
            <input type="checkbox" name="participant" value="${p.id}" data-preset="${p._preset ? "1" : ""}">
            <span class="check-main">${p.name || p.id}${p._preset ? html` <span class="badge">preset</span>` : ""}${p.kind === "remote" && p.online !== undefined && p.online !== null ? html` <span class="online-dot ${p.online ? "on" : "off"}" title="${p.online ? "online" : "offline"}"></span>` : ""}</span>
            <span class="check-sub muted">${configSummary(p)}${p.anchor_elo ? ` · anchor ${p.anchor_elo}` : ""}</span>
          </label>`)}</div>
        </fieldset>`;
      });
    }

    function draw() {
      const def = new Date().toISOString().slice(0, 16).replace("T", " ");
      render(root.querySelector("#nt-body"), html`<form id="nt-form" class="form card" novalidate>
        <div class="form-grid">
          <label class="field span-2"><span>Name</span><input name="name" required maxlength="120" value="Tournament ${def}"></label>
        </div>
        <h2 class="form-h">Participants <span class="muted small" id="nt-count"></span></h2>
        ${players.length || presets.length ? html`<p class="muted small">Presets are registered as players automatically when the tournament is created. <a href="#/players">Manage players</a>.</p>
          <div class="pgroups">${participantGroups()}</div>`
          : html`<div class="empty">No players registered yet — <a href="#/players">add players</a> first.</div>`}
        <h2 class="form-h">Format</h2>
        <div class="form-grid">
          <label class="field"><span>Format</span>
            <select name="format"><option value="round_robin">Round robin</option><option value="gauntlet">Gauntlet</option></select>
          </label>
          <label class="field"><span>Games per pair</span><input name="games_per_pair" type="number" min="1" max="100" value="2" required></label>
          <label class="field"><span>Openings</span>
            <select name="openings"><option value="builtin">Built-in suite</option><option value="none">None (start position)</option></select>
          </label>
          <label class="field"><span>Concurrency <small class="muted">(games at once)</small></span><input name="concurrency" type="number" min="1" max="64" value="4"></label>
          <label class="field"><span>Seed</span><input name="seed" type="number" value="0"></label>
        </div>
        <fieldset class="candidates" id="nt-candidates" hidden>
          <legend>Candidates <span class="muted small">— each plays every other participant</span></legend>
          <div class="check-list inline" id="nt-candidate-list"></div>
        </fieldset>
        <h2 class="form-h">Game rules</h2>
        <div class="form-grid">
          <label class="field"><span>Move timeout (s)</span><input name="move_timeout_s" type="number" min="1" step="any" value="300"></label>
          <label class="field"><span>Max illegal attempts / move</span><input name="max_illegal_attempts" type="number" min="1" max="50" value="3"></label>
          <label class="field"><span>Max plies (then draw)</span><input name="max_plies" type="number" min="2" max="2000" value="300"></label>
        </div>
        <div class="checks-row">
          <label class="check-inline"><input type="checkbox" name="show_legal_moves" checked> Show legal moves to players</label>
          <label class="check-inline"><input type="checkbox" name="wait_for_remote" checked> Wait for remote agents to be online</label>
          <label class="check-inline"><input type="checkbox" name="start" checked> Start immediately</label>
        </div>
        <div id="nt-summary" class="muted small"></div>
        <div id="nt-form-error"></div>
        <div class="form-actions">
          <a class="btn" href="#/tournaments">Cancel</a>
          <button type="submit" class="btn btn-primary">Create tournament</button>
        </div>
      </form>`);
      const form = root.querySelector("#nt-form");
      form.addEventListener("change", update);
      form.addEventListener("input", update);
      form.addEventListener("click", (e) => {
        const s = e.target.closest("[data-select],[data-unselect]");
        if (!s) return;
        const k = s.dataset.select || s.dataset.unselect;
        form.querySelectorAll(`fieldset[data-kind="${k}"] input[name=participant]`).forEach((i) => { i.checked = !!s.dataset.select; });
        update();
      });
      form.addEventListener("submit", submit);
      update();
    }

    function selected() {
      return Array.from(root.querySelectorAll("input[name=participant]:checked")).map((i) => i.value);
    }

    function update() {
      const form = root.querySelector("#nt-form");
      if (!form) return;
      const sel = selected();
      root.querySelector("#nt-count").textContent = `${sel.length} selected`;
      const gauntlet = form.format.value === "gauntlet";
      const cand = root.querySelector("#nt-candidates");
      cand.hidden = !gauntlet;
      const list = root.querySelector("#nt-candidate-list");
      const prevCands = new Set(Array.from(list.querySelectorAll("input:checked")).map((i) => i.value));
      const byId = new Map([...players, ...presets].map((p) => [p.id, p]));
      const current = Array.from(list.querySelectorAll("input")).map((i) => i.value).join(",");
      if (current !== sel.join(",")) {
        list.innerHTML = sel.length
          ? sel.map((id) => `<label class="check-inline"><input type="checkbox" name="candidate" value="${esc(id)}" ${prevCands.has(id) ? "checked" : ""}> ${esc(byId.get(id)?.name || id)}</label>`).join("")
          : `<span class="muted small">Select participants first.</span>`;
      }
      const n = sel.length;
      const gpp = Math.max(1, Number(form.games_per_pair.value) || 1);
      let pairs = 0;
      if (gauntlet) {
        const c = root.querySelectorAll("input[name=candidate]:checked").length;
        pairs = c * (n - c) + (c * (c - 1)) / 2;
      } else {
        pairs = (n * (n - 1)) / 2;
      }
      root.querySelector("#nt-summary").textContent = n >= 2 ? `${pairs} pairing${pairs === 1 ? "" : "s"} × ${gpp} = ${pairs * gpp} games` : "";
    }

    async function submit(e) {
      e.preventDefault();
      const form = e.target;
      const errEl = root.querySelector("#nt-form-error");
      errEl.innerHTML = "";
      const sel = selected();
      const cands = Array.from(root.querySelectorAll("input[name=candidate]:checked")).map((i) => i.value);
      const gpp = Number(form.games_per_pair.value);
      const problems = [];
      if (!form.name.value.trim()) problems.push("Give the tournament a name.");
      if (sel.length < 2) problems.push("Select at least two participants.");
      if (form.format.value === "gauntlet" && (cands.length < 1 || cands.length >= sel.length)) problems.push("A gauntlet needs at least one candidate and at least one non-candidate.");
      if (!(gpp >= 1)) problems.push("Games per pair must be at least 1.");
      if (form.openings.value === "builtin" && gpp % 2 === 1) problems.push("With built-in openings, games per pair must be even so each opening is played with both colours.");
      if (problems.length) {
        render(errEl, html`<div class="banner banner-error" role="alert"><ul>${problems.map((p) => html`<li>${p}</li>`)}</ul></div>`);
        return;
      }
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      btn.textContent = "Creating…";
      try {
        // Register any selected presets first.
        // The server may assign a different id (e.g. "sf-1500-2" when an inactive "sf-1500"
        // exists), so map every selected/candidate id to the id actually registered.
        const presetIds = new Set(presets.map((p) => p.id));
        for (const id of sel.filter((i) => presetIds.has(i) && !registered.has(i))) {
          const p = presets.find((x) => x.id === id);
          const body = { id: p.id, name: p.name, kind: p.kind, config: p.config || {} };
          if (p.anchor_elo != null) body.anchor_elo = p.anchor_elo;
          if (p.max_concurrent_games != null) body.max_concurrent_games = p.max_concurrent_games;
          const res = await post("players", body);
          const newId = (res && res.player && res.player.id) || id;
          registered.set(id, newId); // a retry after a failed submit must not register it again
        }
        const realId = (i) => registered.get(i) || i;
        const num = (v, d) => (v === "" || Number.isNaN(Number(v)) ? d : Number(v));
        const body = {
          name: form.name.value.trim(),
          start: form.start.checked,
          config: {
            player_ids: sel.map(realId),
            format: form.format.value,
            candidate_ids: form.format.value === "gauntlet" ? cands.map(realId) : [],
            games_per_pair: gpp,
            openings: form.openings.value,
            concurrency: num(form.concurrency.value, 4),
            wait_for_remote: form.wait_for_remote.checked,
            seed: num(form.seed.value, 0),
            game: {
              move_timeout_s: num(form.move_timeout_s.value, 300),
              max_illegal_attempts: num(form.max_illegal_attempts.value, 3),
              max_plies: num(form.max_plies.value, 300),
              show_legal_moves: form.show_legal_moves.checked,
            },
          },
        };
        const t = await post("tournaments", body);
        toast(`Tournament “${t.name || body.name}” created`, "ok");
        location.hash = `#/tournament/${enc(t.id)}`;
      } catch (err) {
        render(errEl, errorBanner(err));
        btn.disabled = false;
        btn.textContent = "Create tournament";
      }
    }

    root.addEventListener("click", (e) => { if (e.target.closest("[data-action=retry]")) { root.querySelector("#nt-error").innerHTML = ""; init(); } });
    init();
    return () => { alive = false; };
  },
};

// ================================================================ detail
function crosstableHtml(ct, order, names) {
  if (!ct || !ct.cells || !order.length) return html`<div class="empty">No results yet.</div>`;
  const cells = ct.cells;
  const head = order.map((id, i) => html`<th scope="col" title="${names[id] || id}"><span class="xt-idx">${i + 1}</span></th>`);
  const rows = order.map((a, i) => {
    let tp = 0;
    let tg = 0;
    const tds = order.map((b) => {
      if (a === b) return html`<td class="xt-self" aria-label="same player"></td>`;
      const c = (cells[a] && cells[a][b]) || null;
      const g = c ? Number(c.games) || (Number(c.w) || 0) + (Number(c.d) || 0) + (Number(c.l) || 0) : 0;
      if (!g) return html`<td class="xt-none">·</td>`;
      const pts = (Number(c.w) || 0) + (Number(c.d) || 0) / 2;
      tp += pts;
      tg += g;
      const f = pts / g;
      const mix = f >= 0.5
        ? `color-mix(in oklab, var(--xt-pos) ${Math.round((f - 0.5) * 2 * 100)}%, var(--xt-mid))`
        : `color-mix(in oklab, var(--xt-neg) ${Math.round((0.5 - f) * 2 * 100)}%, var(--xt-mid))`;
      const ptsTxt = Number.isInteger(pts) ? String(pts) : `${Math.floor(pts) || ""}½`;
      const pTxt = c.p != null ? ` · sign test p = ${Number(c.p).toFixed(2)}${Number(c.p) <= 0.05 ? " (significant)" : ""}` : "";
      return html`<td class="xt-cell${c.p != null && Number(c.p) <= 0.05 ? " xt-sig" : ""}" style="background:${mix}" title="${names[a] || a} vs ${names[b] || b}: +${c.w || 0} =${c.d || 0} −${c.l || 0}${pTxt}">${ptsTxt}<span class="xt-of">/${g}</span></td>`;
    });
    const tot = Number.isInteger(tp) ? String(tp) : `${Math.floor(tp) || ""}½`;
    return html`<tr><th scope="row" class="xt-name"><span class="xt-idx">${i + 1}</span> ${names[a] || a}</th>${tds}<td class="xt-total">${tot}<span class="xt-of">/${tg}</span></td></tr>`;
  });
  return html`<div class="table-wrap"><table class="table crosstable"><thead><tr><th scope="col">Player</th>${head}<th scope="col">Total</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

export const tournamentDetailView = {
  mount(root, [tid]) {
    document.title = "Tournament · agentchess";
    const subs = new Subscriptions();
    let alive = true;
    let t = null;
    let games = [];
    let statusFilter = "";
    let busy = false;

    render(root, html`<div id="td-error"></div><div id="td-body">${loading("Loading tournament")}</div>`);

    function actions(status) {
      const b = [];
      if (status !== "running" && t && t.progress && t.progress.aborted > 0) b.push(html`<button type="button" class="btn" data-t="retry-aborted" title="Replay games aborted by provider/engine failures or a cancel">Retry ${t.progress.aborted} aborted</button>`);
      if (status === "pending" || status === "paused") b.push(html`<button type="button" class="btn btn-primary" data-t="start">${status === "paused" ? "Resume" : "Start"}</button>`);
      if (status === "running") b.push(html`<button type="button" class="btn" data-t="pause">Pause</button>`);
      if (["pending", "running", "paused"].includes(status)) b.push(html`<button type="button" class="btn btn-danger-outline" data-t="cancel">Cancel</button>`);
      if (status !== "running") b.push(html`<button type="button" class="btn btn-danger-outline" data-t="delete">Delete</button>`);
      return b;
    }

    function layout() {
      render(root.querySelector("#td-body"), html`
        <div class="page-head">
          <div>
            <p class="crumbs"><a href="#/tournaments">Tournaments</a></p>
            <h1 id="td-name"></h1>
            <p class="subtitle" id="td-sub"></p>
          </div>
          <div class="filters" id="td-actions"></div>
        </div>
        <section class="card"><div id="td-progress"></div><div class="muted small" id="td-config"></div></section>
        <section class="card" aria-labelledby="td-st-title">
          <div class="card-head"><h2 id="td-st-title">Standings</h2><a class="btn btn-sm" href="#/?t=${enc(tid)}">Open in leaderboard</a></div>
          <div id="td-standings"></div>
        </section>
        <section class="card" aria-labelledby="td-xt-title">
          <div class="card-head"><h2 id="td-xt-title">Crosstable</h2><span class="muted small">row player's points vs column player · bold: head-to-head edge significant (sign test p ≤ 0.05)</span></div>
          <div id="td-crosstable"></div>
        </section>
        <section class="card" aria-labelledby="td-g-title">
          <div class="card-head"><h2 id="td-g-title">Games</h2>
            <div class="filters">
              <label class="field-inline"><span>Status</span><select id="td-gfilter">
                <option value="">All</option><option value="running">Running</option><option value="finished">Finished</option><option value="scheduled">Scheduled</option><option value="aborted">Aborted</option>
              </select></label>
              <a class="btn btn-sm" href="${apiUrl("pgn", { tournament_id: tid })}" download="${`${tid}.pgn`}">Export PGN</a>
            </div>
          </div>
          <div id="td-games"></div>
        </section>`);
      root.querySelector("#td-gfilter").addEventListener("change", (e) => { statusFilter = e.target.value; paintGames(); });
    }

    function paintHead() {
      document.title = `${t.name} · agentchess`;
      root.querySelector("#td-name").textContent = t.name;
      const c = t.config || {};
      render(root.querySelector("#td-sub"), html`${statusBadge(t.status)}<span class="sep" aria-hidden="true">·</span>${c.format === "gauntlet" ? "Gauntlet" : "Round robin"}<span class="sep" aria-hidden="true">·</span>created ${fmtDate(t.created_at)}${t.finished_at ? html`<span class="sep" aria-hidden="true">·</span>finished ${fmtDate(t.finished_at)}` : ""}`);
      render(root.querySelector("#td-actions"), actions(t.status));
      render(root.querySelector("#td-progress"), progressBar(t.progress));
      const g = c.game || {};
      const names = namesMap();
      root.querySelector("#td-config").textContent = [
        `${(c.player_ids || []).length} players`,
        c.format === "gauntlet" ? `candidates: ${(c.candidate_ids || []).map((id) => names[id] || id).join(", ") || "–"}` : null,
        `${c.games_per_pair} games/pair`,
        `openings: ${c.openings}`,
        `concurrency ${c.concurrency}`,
        `timeout ${g.move_timeout_s}s/move`,
        `max ${g.max_illegal_attempts} illegal/move`,
        `max ${g.max_plies} plies`,
        g.show_legal_moves === false ? "legal moves hidden" : "legal moves shown",
        c.wait_for_remote ? "waits for remote agents" : null,
      ].filter(Boolean).join(" · ");
    }

    function namesMap() {
      const m = {};
      for (const p of (t && t.players) || []) m[p.id] = p.name;
      for (const r of (t && t.standings) || []) m[r.player_id] = m[r.player_id] || r.name;
      for (const g of games) { m[g.white_id] = m[g.white_id] || g.white_name; m[g.black_id] = m[g.black_id] || g.black_name; }
      return m;
    }

    function paintResults(stats) {
      render(root.querySelector("#td-standings"), standingsTable(t.standings || [], stats || {}));
      const order = (t.standings || []).map((r) => r.player_id);
      for (const id of (t.crosstable && t.crosstable.players) || (t.config && t.config.player_ids) || []) if (!order.includes(id)) order.push(id);
      render(root.querySelector("#td-crosstable"), crosstableHtml(t.crosstable, order, namesMap()));
    }

    function paintGames() {
      const list = statusFilter ? games.filter((g) => g.status === statusFilter) : games;
      render(root.querySelector("#td-games"), gamesTable(list, { showRound: true, emptyText: statusFilter ? `No ${statusFilter} games.` : "No games scheduled." }));
    }

    async function load() {
      try {
        const [detail, gl, ratings] = await Promise.all([
          get(`tournaments/${enc(tid)}`),
          get("games", { tournament_id: tid, limit: 1000, order: "seq" }),
          get("ratings", { tournament_id: tid }).catch(() => null),
        ]);
        if (!alive) return;
        root.querySelector("#td-error").innerHTML = "";
        const first = !t;
        t = detail;
        games = (gl && gl.games) || [];
        if (first) layout();
        paintHead();
        paintResults(ratings && ratings.stats);
        paintGames();
      } catch (e) {
        if (!alive) return;
        if (!t) root.querySelector("#td-body").innerHTML = "";
        render(root.querySelector("#td-error"), errorBanner(e, { retry: true }));
      }
    }
    const refresh = debounce(load, 1000);

    root.addEventListener("click", async (e) => {
      if (e.target.closest("[data-action=retry]")) { load(); return; }
      const b = e.target.closest("[data-t]");
      if (!b || busy || !t) return;
      const act = b.dataset.t;
      if (act === "cancel" && !(await confirmModal("Cancel tournament?", "Running games will be aborted and remaining games will not be played. Finished games keep counting for ratings.", "Cancel tournament"))) return;
      if (act === "delete" && !(await confirmModal("Delete tournament?", `“${t.name}” and all its games will be permanently deleted.`, "Delete"))) return;
      busy = true;
      b.disabled = true;
      try {
        if (act === "delete") {
          await del(`tournaments/${enc(tid)}`);
          toast("Tournament deleted", "ok");
          location.hash = "#/tournaments";
          return;
        }
        await post(`tournaments/${enc(tid)}/${act}`);
        toast({ start: "Tournament started", pause: "Tournament paused", cancel: "Tournament cancelled", "retry-aborted": "Aborted games rescheduled" }[act] || "Done", "ok");
        await load();
      } catch (err) {
        render(root.querySelector("#td-error"), errorBanner(err));
      } finally {
        busy = false;
        b.disabled = false;
      }
    });

    subs.on("tournament_updated", (e) => {
      if (!t || !e.tournament || e.tournament.id !== tid) return;
      Object.assign(t, e.tournament);
      if (e.progress) t.progress = e.progress;
      paintHead();
      refresh();
    });
    const mine = (e) => e.game && e.game.tournament_id === tid;
    subs.on("game_started", (e) => { if (mine(e)) refresh(); });
    subs.on("game_finished", (e) => { if (mine(e)) refresh(); });
    subs.on("_reconnect", load);
    load();
    return () => { alive = false; subs.clear(); refresh.cancel(); };
  },
};
