// Players: registry list, add-player form (kind-specific), token modal, player detail.
import { get, post, patch, del } from "../api.js";
import { Subscriptions } from "../ws.js";
import {
  html, render, enc, errorBanner, debounce, kindBadge, configSummary, toast, confirmModal, openModal,
  fmtNum, fmtPct, fmtSecs, fmtTokens, fmtCost, timeAgo, appBase,
} from "../util.js";
import { gamesTable, loading, normStats, forfeitsOf } from "../components.js";
import { codeBlock, wireCopy, curlExamples, mcpCommand } from "./connect.js";

// ---------------------------------------------------------------- token modal
export function showTokenModal(player, token) {
  const origin = appBase();
  const { dialog } = openModal(`Token for ${player.name || player.id}`, html`
    <div class="banner banner-warn" role="note"><strong>Copy this token now.</strong> It is shown only once — only a hash is stored. You can rotate it later from the Players page.</div>
    <div class="token-row">
      <code class="token" id="token-value">${token}</code>
      <button type="button" class="btn btn-primary" data-copy data-copy-text="${token}">Copy token</button>
    </div>
    <h3>Connect with MCP</h3>
    ${codeBlock(mcpCommand(origin, token))}
    <h3>Or over HTTP</h3>
    ${codeBlock(curlExamples(origin, token))}
    <p class="muted small">Full protocol (WebSocket, Python loop): see <a href="#/connect" data-close>Connect</a>.</p>
    <div class="form-actions"><button type="button" class="btn" data-close>Done</button></div>`, { wide: true });
  wireCopy(dialog);
}

// ---------------------------------------------------------------- add-player form
const LLM_DEFAULT_ENV = { anthropic: "ANTHROPIC_API_KEY", openai: "OPENAI_API_KEY" };

function kindFields(kind, presets) {
  if (kind === "engine") {
    const engPresets = presets.filter((p) => p.kind === "engine");
    return html`
      ${engPresets.length ? html`<label class="field span-2"><span>Preset</span>
        <select name="preset"><option value="">Custom…</option>${engPresets.map((p) => html`<option value="${p.id}">${p.name}${p.anchor_elo ? ` (anchor ${p.anchor_elo})` : ""}</option>`)}</select>
      </label>` : ""}
      <label class="field"><span>UCI_Elo <small class="muted">1320–3190, limits strength</small></span><input name="uci_elo" type="number" min="1320" max="3190" placeholder="e.g. 1500"></label>
      <label class="field"><span>Skill level <small class="muted">0–20</small></span><input name="skill_level" type="number" min="0" max="20" placeholder="optional"></label>
      <label class="field"><span>Move time (ms)</span><input name="movetime_ms" type="number" min="1" value="100"></label>
      <label class="field"><span>Depth limit</span><input name="depth" type="number" min="1" max="99" placeholder="optional"></label>
      <label class="field"><span>Anchor Elo <small class="muted">fixes this rating</small></span><input name="anchor_elo" type="number" placeholder="blank = not anchored"></label>
      <label class="field"><span>Max concurrent games</span><input name="max_concurrent_games" type="number" min="1" max="64" value="2"></label>
      <label class="field span-2"><span>Engine path <small class="muted">blank = auto-detect Stockfish</small></span><input name="path" placeholder="/usr/games/stockfish"></label>`;
  }
  if (kind === "llm") {
    return html`
      <label class="field"><span>Provider</span><select name="provider"><option value="anthropic">Anthropic</option><option value="openai">OpenAI-compatible</option></select></label>
      <label class="field"><span>Model</span><input name="model" required placeholder="claude-opus-5-5" value="claude-opus-5-5"></label>
      <label class="field"><span>API key env var <small class="muted">keys are never stored</small></span><input name="api_key_env" placeholder="ANTHROPIC_API_KEY"></label>
      <label class="field" data-only="openai" hidden><span>Base URL</span><input name="base_url" type="url" placeholder="https://api.openai.com/v1"></label>
      <label class="field" data-only="anthropic"><span>Effort</span><select name="effort"><option value="">Default</option><option>low</option><option>medium</option><option>high</option><option>max</option></select></label>
      <label class="field" data-only="openai" hidden><span>Temperature</span><input name="temperature" type="number" step="0.1" min="0" max="2" placeholder="default"></label>
      <label class="field"><span>Max tokens</span><input name="max_tokens" type="number" min="16" value="16000"></label>
      <label class="field"><span>Max concurrent games</span><input name="max_concurrent_games" type="number" min="1" max="64" value="4"></label>
      <label class="field"><span>Input $ / Mtok</span><input name="input_cost_per_mtok" type="number" step="any" min="0" placeholder="optional"></label>
      <label class="field"><span>Output $ / Mtok</span><input name="output_cost_per_mtok" type="number" step="any" min="0" placeholder="optional"></label>
      <label class="field span-2"><span>System prompt override <small class="muted">optional</small></span><textarea name="system_prompt" rows="3" placeholder="Leave blank for the default benchmark prompt"></textarea></label>`;
  }
  if (kind === "remote") {
    return html`<label class="field"><span>Max concurrent games</span><input name="max_concurrent_games" type="number" min="1" max="64" value="1"></label>
      <p class="muted small span-2">After creation you get a bearer token for the agent API (shown once), plus copy-paste connection instructions.</p>`;
  }
  return html`<label class="field"><span>Max concurrent games</span><input name="max_concurrent_games" type="number" min="1" max="64" value="8"></label>
    <p class="muted small span-2">Plays uniformly random legal moves — a useful rating floor.</p>`;
}

function mountAddForm(host, presets, initialKind, onCreated) {
  let kind = initialKind || "engine";
  render(host, html`<form class="form" id="add-form" novalidate>
    <div class="seg" role="radiogroup" aria-label="Player kind">
      ${["engine", "llm", "remote", "random"].map((k) => html`<label class="seg-opt"><input type="radio" name="kind" value="${k}" ${k === kind ? "checked" : ""}><span>${{ engine: "Engine", llm: "LLM", remote: "Remote agent", random: "Random" }[k]}</span></label>`)}
    </div>
    <div class="form-grid">
      <label class="field"><span>Name</span><input name="name" required maxlength="80" placeholder="Display name"></label>
      <label class="field"><span>ID <small class="muted">optional, derived from name</small></span><input name="id" maxlength="64" pattern="[A-Za-z0-9_.\\-]+" placeholder="auto"></label>
    </div>
    <div class="form-grid" id="kind-fields"></div>
    <div id="add-error"></div>
    <div class="form-actions"><button type="button" class="btn" data-cancel>Cancel</button><button type="submit" class="btn btn-primary">Add player</button></div>
  </form>`);
  const form = host.querySelector("#add-form");
  form.querySelectorAll("input[name=kind]").forEach((r) => { r.checked = r.value === kind; });

  function drawKind() {
    render(form.querySelector("#kind-fields"), kindFields(kind, presets));
    syncProvider();
  }
  function syncProvider() {
    if (kind !== "llm") return;
    const prov = form.provider.value;
    form.querySelectorAll("[data-only]").forEach((el) => { el.hidden = el.dataset.only !== prov; });
    form.api_key_env.placeholder = LLM_DEFAULT_ENV[prov];
  }
  form.addEventListener("change", (e) => {
    if (e.target.name === "kind") { kind = e.target.value; drawKind(); return; }
    if (e.target.name === "provider") syncProvider();
    if (e.target.name === "preset") {
      const p = presets.find((x) => x.id === e.target.value);
      if (!p) return;
      const c = p.config || {};
      form.name.value = p.name || "";
      form.id.value = p.id || "";
      for (const k of ["uci_elo", "skill_level", "movetime_ms", "depth"]) if (form[k]) form[k].value = c[k] ?? "";
      form.anchor_elo.value = p.anchor_elo ?? "";
      form.max_concurrent_games.value = p.max_concurrent_games ?? 2;
    }
    if (e.target.name === "uci_elo" && form.anchor_elo && !form.anchor_elo.value && e.target.value) form.anchor_elo.value = e.target.value;
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const errEl = form.querySelector("#add-error");
    errEl.innerHTML = "";
    const v = (n) => (form[n] ? String(form[n].value).trim() : "");
    const num = (n) => (v(n) === "" ? undefined : Number(v(n)));
    if (!v("name")) { render(errEl, html`<div class="banner banner-error" role="alert">Name is required.</div>`); form.name.focus(); return; }
    const body = { name: v("name"), kind, config: {} };
    if (v("id")) body.id = v("id");
    if (num("max_concurrent_games") !== undefined) body.max_concurrent_games = num("max_concurrent_games");
    const c = body.config;
    if (kind === "engine") {
      for (const k of ["uci_elo", "skill_level", "movetime_ms", "depth"]) if (num(k) !== undefined) c[k] = num(k);
      if (v("path")) c.path = v("path");
      if (num("anchor_elo") !== undefined) body.anchor_elo = num("anchor_elo");
      const preset = presets.find((x) => x.id === v("preset"));
      if (preset && preset.config) for (const [k, val] of Object.entries(preset.config)) if (!(k in c) && !["uci_elo", "skill_level", "movetime_ms", "depth"].includes(k)) c[k] = val;
    } else if (kind === "llm") {
      if (!v("model")) { render(errEl, html`<div class="banner banner-error" role="alert">Model is required.</div>`); return; }
      c.provider = v("provider");
      c.model = v("model");
      c.api_key_env = v("api_key_env") || LLM_DEFAULT_ENV[c.provider];
      if (c.provider === "openai" && v("base_url")) c.base_url = v("base_url");
      if (c.provider === "anthropic" && v("effort")) c.effort = v("effort");
      if (c.provider === "openai" && num("temperature") !== undefined) c.temperature = num("temperature");
      if (num("max_tokens") !== undefined) c.max_tokens = num("max_tokens");
      if (num("input_cost_per_mtok") !== undefined) c.input_cost_per_mtok = num("input_cost_per_mtok");
      if (num("output_cost_per_mtok") !== undefined) c.output_cost_per_mtok = num("output_cost_per_mtok");
      if (v("system_prompt")) c.system_prompt = v("system_prompt");
    }
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const res = await post("players", body);
      const player = (res && res.player) || res;
      toast(`Added ${player.name || body.name}`, "ok");
      if (res && res.token) showTokenModal(player, res.token);
      onCreated(player);
    } catch (err) {
      render(errEl, errorBanner(err));
    } finally {
      btn.disabled = false;
    }
  });
  drawKind();
  return form;
}

// ---------------------------------------------------------------- list
export const playersView = {
  mount(root, _p, query) {
    document.title = "Players · agentchess";
    const subs = new Subscriptions();
    let alive = true;
    let presets = [];
    let showInactive = false;
    render(root, html`
      <div class="page-head">
        <div><h1>Players</h1><p class="subtitle">Engines anchor the scale; LLMs and remote agents are what we measure.</p></div>
        <div class="filters">
          <label class="check-inline"><input type="checkbox" id="pl-inactive"> Show inactive</label>
          <button type="button" class="btn btn-primary" id="pl-add-btn" aria-expanded="false" aria-controls="pl-add">Add player</button>
        </div>
      </div>
      <div id="pl-error"></div>
      <section class="card" id="pl-add" hidden aria-label="Add player"></section>
      <div id="pl-list">${loading("Loading players")}</div>`);

    const addCard = root.querySelector("#pl-add");
    const addBtn = root.querySelector("#pl-add-btn");
    function openAdd(kind) {
      addCard.hidden = false;
      addBtn.setAttribute("aria-expanded", "true");
      const form = mountAddForm(addCard, presets, kind, () => { closeAdd(); load(); });
      form.querySelector("[data-cancel]").addEventListener("click", closeAdd);
      form.name.focus();
    }
    function closeAdd() { addCard.hidden = true; addCard.innerHTML = ""; addBtn.setAttribute("aria-expanded", "false"); }
    addBtn.addEventListener("click", () => (addCard.hidden ? openAdd() : closeAdd()));
    root.querySelector("#pl-inactive").addEventListener("change", (e) => { showInactive = e.target.checked; load(); });

    async function load() {
      try {
        const list = (await get("players", showInactive ? { include_inactive: "true" } : undefined)) || [];
        if (!alive) return;
        root.querySelector("#pl-error").innerHTML = "";
        const el = root.querySelector("#pl-list");
        if (!list.length) {
          render(el, html`<div class="empty">No players yet — add an engine preset and an LLM to get started.</div>`);
          return;
        }
        render(el, html`<div class="table-wrap card card-flush"><table class="table players">
          <thead><tr><th scope="col">Player</th><th scope="col">Kind</th><th scope="col">Configuration</th><th class="num" scope="col">Anchor</th><th class="num" scope="col" title="Max concurrent games">Max games</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
          <tbody>${list.map((p) => html`<tr class="${p.active === false ? "inactive" : ""}">
            <td class="player-cell">
              ${p.kind === "remote" ? html`<span class="online-dot ${p.online ? "on" : "off"}" title="${p.online ? "online" : "offline"}" role="img" aria-label="${p.online ? "online" : "offline"}"></span>` : ""}
              <a href="#/player/${enc(p.id)}">${p.name}</a> <span class="muted small">${p.id}</span>
              ${p.active === false ? html`<span class="badge">inactive</span>` : ""}
            </td>
            <td>${kindBadge(p.kind)}</td>
            <td class="muted">${configSummary(p)}</td>
            <td class="num">${p.anchor_elo != null ? html`<span title="Anchored rating">⚓ ${fmtNum(p.anchor_elo)}</span>` : "–"}</td>
            <td class="num">${p.max_concurrent_games ?? "–"}</td>
            <td class="actions">
              ${p.kind === "remote" ? html`<button type="button" class="btn btn-sm" data-rotate="${p.id}" data-name="${p.name}">Rotate token</button>` : ""}
              <button type="button" class="btn btn-sm btn-danger-outline" data-delete="${p.id}" data-name="${p.name}">Delete</button>
            </td>
          </tr>`)}</tbody></table></div>`);
      } catch (e) {
        if (!alive) return;
        root.querySelector("#pl-list").innerHTML = "";
        render(root.querySelector("#pl-error"), errorBanner(e, { retry: true }));
      }
    }

    root.addEventListener("click", async (e) => {
      if (e.target.closest("[data-action=retry]")) { load(); return; }
      const rot = e.target.closest("[data-rotate]");
      if (rot) {
        if (!(await confirmModal("Rotate token?", `The current token of “${rot.dataset.name}” stops working immediately.`, "Rotate token"))) return;
        try {
          const res = await post(`players/${enc(rot.dataset.rotate)}/token`);
          showTokenModal({ id: rot.dataset.rotate, name: rot.dataset.name }, res.token);
        } catch (err) { render(root.querySelector("#pl-error"), errorBanner(err)); }
        return;
      }
      const d = e.target.closest("[data-delete]");
      if (d) {
        if (!(await confirmModal("Delete player?", `“${d.dataset.name}” will be deleted — or deactivated if it already has games, so ratings history is kept.`, "Delete"))) return;
        try {
          const res = await del(`players/${enc(d.dataset.delete)}`);
          toast(res && res.deactivated && !res.deleted ? `${d.dataset.name} has games — deactivated instead` : `${d.dataset.name} deleted`, "ok");
          load();
        } catch (err) { render(root.querySelector("#pl-error"), errorBanner(err)); }
      }
    });

    const refresh = debounce(load, 500);
    subs.on("agent_status", refresh).on("_reconnect", load);
    get("players/presets").then((p) => { presets = p || []; }).catch(() => {}).finally(() => {
      if (alive && query.has("add")) openAdd(query.get("add") || "engine");
    });
    load();
    return () => { alive = false; subs.clear(); refresh.cancel(); };
  },
};

// ---------------------------------------------------------------- detail
function tile(label, value, sub = "") {
  return html`<div class="tile"><div class="tile-label">${label}</div><div class="tile-value">${value}</div>${sub ? html`<div class="tile-sub">${sub}</div>` : ""}</div>`;
}

export const playerDetailView = {
  mount(root, [pid]) {
    document.title = "Player · agentchess";
    const subs = new Subscriptions();
    let alive = true;
    let player = null;
    render(root, html`<div id="pd-error"></div><div id="pd-body">${loading("Loading player")}</div>`);

    async function load() {
      try {
        const [p, gl] = await Promise.all([
          get(`players/${enc(pid)}`),
          get("games", { player_id: pid, order: "recent", limit: 50 }),
        ]);
        if (!alive) return;
        player = p;
        root.querySelector("#pd-error").innerHTML = "";
        document.title = `${p.name} · agentchess`;
        const r = p.rating;
        const st = normStats(p.stats);
        const moves = Number(st.moves) || 0;
        const tokens = (Number(st.input_tokens) || 0) + (Number(st.output_tokens) || 0);
        const games = (gl && gl.games) || [];
        render(root.querySelector("#pd-body"), html`
          <div class="page-head">
            <div>
              <p class="crumbs"><a href="#/players">Players</a></p>
              <h1>${p.name} ${kindBadge(p.kind)} ${p.active === false ? html`<span class="badge">inactive</span>` : ""}</h1>
              <p class="subtitle">${p.kind === "remote" ? html`<span class="online-dot ${p.online ? "on" : "off"}" aria-hidden="true"></span> ${p.online ? "online" : "offline"} · ` : ""}<span class="muted">${p.id}</span> · ${configSummary(p)}</p>
            </div>
            <div class="filters">
              ${p.kind === "remote" ? html`<button type="button" class="btn" data-pd="rotate">Rotate token</button>` : ""}
              <button type="button" class="btn" data-pd="toggle">${p.active === false ? "Activate" : "Deactivate"}</button>
              <button type="button" class="btn btn-danger-outline" data-pd="delete">Delete</button>
            </div>
          </div>
          <div class="tiles">
            ${tile("Elo", r ? fmtNum(r.elo) : "–", r ? (r.anchored ? "anchored" : `95% CI ${fmtNum(r.ci_low)}–${fmtNum(r.ci_high)}`) : "no rated games")}
            ${tile("Rank", r && r.rank ? `#${r.rank}` : "–")}
            ${tile("Games", r ? fmtNum(r.games) : fmtNum(games.filter((g) => g.status === "finished").length), r ? `${r.wins}W · ${r.draws}D · ${r.losses}L` : "")}
            ${tile("Score", r ? fmtPct(r.score) : "–", r && r.performance ? `perf. ${fmtNum(r.performance)}` : "")}
            ${tile("Illegal rate", moves ? fmtPct((Number(st.illegal_attempts) || 0) / moves) : "–", moves ? `${fmtNum(st.illegal_attempts || 0)} over ${fmtNum(moves)} moves` : "")}
            ${tile("Forfeits", moves ? String(forfeitsOf(st)) : "–", moves ? `illegal ${st.forfeit_illegal || 0} · timeout ${st.forfeit_timeout || 0} · error ${st.forfeit_error || 0}` : "")}
            ${tile("Avg move time", fmtSecs(st.avg_move_s))}
            ${tokens ? tile("Tokens", fmtTokens(tokens), `in ${fmtTokens(st.input_tokens)} · out ${fmtTokens(st.output_tokens)}`) : ""}
            ${Number(st.cost_usd) ? tile("Cost", fmtCost(st.cost_usd), moves ? `${fmtCost(st.cost_usd / moves)} / move` : "") : ""}
          </div>
          <section class="card">
            <div class="card-head"><h2>Configuration</h2><span class="muted small">created ${timeAgo(p.created_at)}</span></div>
            <dl class="kv kv-wide">
              <div><dt>Kind</dt><dd>${p.kind}</dd></div>
              <div><dt>Anchor Elo</dt><dd>${p.anchor_elo ?? "not anchored"}</dd></div>
              <div><dt>Max concurrent games</dt><dd>${p.max_concurrent_games ?? "–"}</dd></div>
              ${Object.entries(p.config || {}).map(([k, v]) => html`<div><dt>${k}</dt><dd><code>${typeof v === "object" ? JSON.stringify(v) : String(v)}</code></dd></div>`)}
            </dl>
          </section>
          <section class="card">
            <div class="card-head"><h2>Recent games</h2><span class="muted small">${gl && gl.total != null ? `${gl.total} total` : ""}</span></div>
            ${gamesTable(games, { perspective: pid, emptyText: "No games yet — add this player to a tournament." })}
          </section>`);
      } catch (e) {
        if (!alive) return;
        root.querySelector("#pd-body").innerHTML = "";
        render(root.querySelector("#pd-error"), errorBanner(e, { retry: true }));
      }
    }

    root.addEventListener("click", async (e) => {
      if (e.target.closest("[data-action=retry]")) { load(); return; }
      const b = e.target.closest("[data-pd]");
      if (!b || !player) return;
      try {
        if (b.dataset.pd === "rotate") {
          if (!(await confirmModal("Rotate token?", "The current token stops working immediately.", "Rotate token"))) return;
          const res = await post(`players/${enc(pid)}/token`);
          showTokenModal(player, res.token);
        } else if (b.dataset.pd === "toggle") {
          await patch(`players/${enc(pid)}`, { active: player.active === false });
          toast(player.active === false ? "Player activated" : "Player deactivated", "ok");
          load();
        } else if (b.dataset.pd === "delete") {
          if (!(await confirmModal("Delete player?", `“${player.name}” will be deleted — or deactivated if it already has games.`, "Delete"))) return;
          const res = await del(`players/${enc(pid)}`);
          toast(res && res.deleted ? "Player deleted" : "Player has games — deactivated instead", "ok");
          if (res && res.deleted) location.hash = "#/players"; else load();
        }
      } catch (err) {
        render(root.querySelector("#pd-error"), errorBanner(err));
      }
    });

    const refresh = debounce(load, 1500);
    subs.on("game_finished", (e) => { if (e.game && (e.game.white_id === pid || e.game.black_id === pid)) refresh(); });
    subs.on("game_started", (e) => { if (e.game && (e.game.white_id === pid || e.game.black_id === pid)) refresh(); });
    subs.on("agent_status", (e) => { if (e.player_id === pid) refresh(); });
    load();
    return () => { alive = false; subs.clear(); refresh.cancel(); };
  },
};
