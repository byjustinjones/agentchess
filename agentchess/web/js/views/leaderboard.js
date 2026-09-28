// Leaderboard: ratings table + Elo CI chart, filterable by tournament, anchors toggle.
import { get } from "../api.js";
import { Subscriptions } from "../ws.js";
import { html, render, errorBanner, debounce, esc } from "../util.js";
import { standingsTable, mountCiChart, loading } from "../components.js";

const state = { tournamentId: "", anchors: true };
try {
  const saved = JSON.parse(localStorage.getItem("agentchess-lb") || "{}");
  if (typeof saved.anchors === "boolean") state.anchors = saved.anchors;
} catch (_) { /* ignore */ }

function save() {
  try { localStorage.setItem("agentchess-lb", JSON.stringify({ anchors: state.anchors })); } catch (_) { /* ignore */ }
}

export default {
  mount(root, _params, query) {
    document.title = "Leaderboard · agentchess";
    if (query.has("t")) state.tournamentId = query.get("t") || "";
    const subs = new Subscriptions();
    let chartCleanup = () => {};
    let alive = true;
    let tournaments = [];

    render(root, html`
      <div class="page-head">
        <div>
          <h1>Leaderboard</h1>
          <p class="subtitle">Bradley–Terry Elo with 95% bootstrap confidence intervals. Engine levels anchor the scale.</p>
        </div>
        <div class="filters">
          <label class="field-inline">
            <span>Games from</span>
            <select id="lb-tournament"><option value="">All games</option></select>
          </label>
          <label class="switch">
            <input type="checkbox" id="lb-anchors" ${state.anchors ? "checked" : ""}>
            <span class="switch-track" aria-hidden="true"></span>
            <span>Anchor to engines</span>
          </label>
        </div>
      </div>
      <div id="lb-error"></div>
      <section class="card" aria-labelledby="lb-table-title">
        <div class="card-head"><h2 id="lb-table-title">Ratings</h2><span class="muted" id="lb-meta"></span></div>
        <div id="lb-table">${loading("Loading ratings")}</div>
      </section>
      <section class="card" aria-labelledby="lb-chart-title" id="lb-chart-card" hidden>
        <div class="card-head"><h2 id="lb-chart-title">Elo ± 95% CI</h2></div>
        <div class="ci-chart-host" id="lb-chart"></div>
      </section>`);
    const anchorsEl = root.querySelector("#lb-anchors");
    anchorsEl.checked = state.anchors;
    const selEl = root.querySelector("#lb-tournament");

    async function loadTournaments() {
      try {
        tournaments = (await get("tournaments")) || [];
      } catch (_) { tournaments = []; }
      if (!alive) return;
      selEl.innerHTML = `<option value="">All games</option>${tournaments.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}`;
      if (state.tournamentId && !tournaments.some((t) => t.id === state.tournamentId)) state.tournamentId = "";
      selEl.value = state.tournamentId;
    }

    async function load() {
      const errEl = root.querySelector("#lb-error");
      try {
        const data = await get("ratings", { tournament_id: state.tournamentId || undefined, anchors: state.anchors ? "true" : "false" });
        if (!alive) return;
        errEl.innerHTML = "";
        const rows = (data && data.ratings) || [];
        const stats = (data && data.stats) || {};
        render(root.querySelector("#lb-table"), standingsTable(rows, stats));
        root.querySelector("#lb-meta").textContent = data && data.games != null ? `${data.games} rated game${data.games === 1 ? "" : "s"}` : "";
        const card = root.querySelector("#lb-chart-card");
        chartCleanup();
        card.hidden = rows.length === 0;
        chartCleanup = rows.length ? mountCiChart(root.querySelector("#lb-chart"), rows) : () => {};
      } catch (e) {
        if (!alive) return;
        render(errEl, errorBanner(e, { retry: true }));
        const tbl = root.querySelector("#lb-table");
        if (tbl.querySelector(".loading")) tbl.innerHTML = "";
      }
    }

    selEl.addEventListener("change", () => {
      state.tournamentId = selEl.value;
      const q = state.tournamentId ? `?t=${encodeURIComponent(state.tournamentId)}` : "";
      history.replaceState(null, "", `#/${q}`);
      load();
    });
    anchorsEl.addEventListener("change", () => { state.anchors = anchorsEl.checked; save(); load(); });
    root.addEventListener("click", (e) => { if (e.target.closest("[data-action=retry]")) load(); });

    const refresh = debounce(load, 1500);
    subs.on("game_finished", refresh);
    subs.on("tournament_updated", debounce(loadTournaments, 1500));
    subs.on("_reconnect", load);

    loadTournaments();
    load();
    return () => { alive = false; refresh.cancel(); subs.clear(); chartCleanup(); };
  },
};
