// Play: quick ad-hoc exhibition game between two registered players.
import { get, post } from "../api.js";
import { html, render, enc, errorBanner, toast, configSummary } from "../util.js";
import { loading } from "../components.js";

const KIND_LABEL = { engine: "Engines", llm: "LLMs", remote: "Remote agents", random: "Random" };

function playerOptions(players, selected) {
  const groups = {};
  for (const p of players) (groups[p.kind] = groups[p.kind] || []).push(p);
  return Object.keys(groups).map((k) => html`<optgroup label="${KIND_LABEL[k] || k}">${groups[k].map((p) => html`<option value="${p.id}" ${p.id === selected ? "selected" : ""}>${p.name}${p.kind === "remote" ? (p.online ? " (online)" : " (offline)") : ""}</option>`)}</optgroup>`);
}

export default {
  mount(root) {
    document.title = "Play a match · agentchess";
    let alive = true;
    let players = [];
    render(root, html`
      <div class="page-head"><div><h1>Play a match</h1><p class="subtitle">A single exhibition game between any two players. It counts toward the overall ratings.</p></div></div>
      <div id="pm-error"></div>
      <div id="pm-body">${loading("Loading players")}</div>`);

    async function init() {
      try {
        const [pl, openings] = await Promise.all([get("players"), get("openings").catch(() => [])]);
        if (!alive) return;
        players = (pl || []).filter((p) => p.active !== false);
        if (players.length < 2) {
          render(root.querySelector("#pm-body"), html`<div class="empty">You need at least two players — <a href="#/players?add=engine">add players</a> first.</div>`);
          return;
        }
        const w = players.find((p) => p.kind === "llm" || p.kind === "remote") || players[0];
        const b = players.find((p) => p.id !== w.id && p.kind === "engine") || players.find((p) => p.id !== w.id);
        render(root.querySelector("#pm-body"), html`<form class="form card" id="pm-form" novalidate>
          <div class="matchup">
            <label class="field"><span><span class="side-dot white" aria-hidden="true"></span> White</span><select name="white_id">${playerOptions(players, w.id)}</select><small class="muted" data-desc="white_id"></small></label>
            <button type="button" class="btn btn-icon-txt swap" id="pm-swap" aria-label="Swap colours" title="Swap colours">⇄</button>
            <label class="field"><span><span class="side-dot black" aria-hidden="true"></span> Black</span><select name="black_id">${playerOptions(players, b.id)}</select><small class="muted" data-desc="black_id"></small></label>
          </div>
          <div class="form-grid">
            <label class="field span-2"><span>Opening</span><select name="opening_id"><option value="">Start position</option>${(openings || []).map((o) => html`<option value="${o.id}">${o.eco ? `${o.eco} · ` : ""}${o.name}</option>`)}</select></label>
            <label class="field"><span>Move timeout (s)</span><input name="move_timeout_s" type="number" min="1" step="any" value="300"></label>
            <label class="field"><span>Max illegal attempts / move</span><input name="max_illegal_attempts" type="number" min="1" max="50" value="3"></label>
            <label class="field"><span>Max plies (then draw)</span><input name="max_plies" type="number" min="2" max="2000" value="300"></label>
          </div>
          <label class="check-inline"><input type="checkbox" name="show_legal_moves" checked> Show legal moves to players</label>
          <div id="pm-form-error"></div>
          <div class="form-actions"><button type="submit" class="btn btn-primary">Start game</button></div>
        </form>`);
        const form = root.querySelector("#pm-form");
        const desc = () => {
          for (const n of ["white_id", "black_id"]) {
            const p = players.find((x) => x.id === form[n].value);
            form.querySelector(`[data-desc=${n}]`).textContent = p ? configSummary(p) : "";
          }
        };
        desc();
        form.addEventListener("change", desc);
        root.querySelector("#pm-swap").addEventListener("click", () => {
          const a = form.white_id.value;
          form.white_id.value = form.black_id.value;
          form.black_id.value = a;
          desc();
        });
        form.addEventListener("submit", submit);
      } catch (e) {
        if (!alive) return;
        root.querySelector("#pm-body").innerHTML = "";
        render(root.querySelector("#pm-error"), errorBanner(e, { retry: true }));
      }
    }

    async function submit(e) {
      e.preventDefault();
      const form = e.target;
      const errEl = root.querySelector("#pm-form-error");
      errEl.innerHTML = "";
      if (form.white_id.value === form.black_id.value) {
        render(errEl, html`<div class="banner banner-error" role="alert">Pick two different players.</div>`);
        return;
      }
      const num = (v, d) => (v === "" || Number.isNaN(Number(v)) ? d : Number(v));
      const body = {
        white_id: form.white_id.value,
        black_id: form.black_id.value,
        config: {
          move_timeout_s: num(form.move_timeout_s.value, 300),
          max_illegal_attempts: num(form.max_illegal_attempts.value, 3),
          max_plies: num(form.max_plies.value, 300),
          show_legal_moves: form.show_legal_moves.checked,
        },
      };
      if (form.opening_id.value) body.opening_id = form.opening_id.value;
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      btn.textContent = "Starting…";
      try {
        const g = await post("games", body);
        toast("Game started", "ok");
        location.hash = `#/game/${enc((g && (g.id || (g.game && g.game.id))) || "")}`;
      } catch (err) {
        render(errEl, errorBanner(err));
        btn.disabled = false;
        btn.textContent = "Start game";
      }
    }

    root.addEventListener("click", (e) => { if (e.target.closest("[data-action=retry]")) { root.querySelector("#pm-error").innerHTML = ""; init(); } });
    init();
    return () => { alive = false; };
  },
};
