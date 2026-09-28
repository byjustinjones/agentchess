// agentchess web GUI — entry point: router, theme, connection indicator.
import * as ws from "./js/ws.js";
import { get } from "./js/api.js";
import { html, render, errorBanner } from "./js/util.js";
import leaderboard from "./js/views/leaderboard.js";
import live from "./js/views/live.js";
import game from "./js/views/game.js";
import { tournamentsView, tournamentNewView, tournamentDetailView } from "./js/views/tournaments.js";
import { playersView, playerDetailView } from "./js/views/players.js";
import play from "./js/views/play.js";
import connect from "./js/views/connect.js";

const routes = [
  { re: /^\/?$/, view: leaderboard, nav: "leaderboard" },
  { re: /^\/leaderboard\/?$/, view: leaderboard, nav: "leaderboard" },
  { re: /^\/live\/?$/, view: live, nav: "live" },
  { re: /^\/game\/([^/]+)\/?$/, view: game, nav: "live" },
  { re: /^\/tournaments\/?$/, view: tournamentsView, nav: "tournaments" },
  { re: /^\/tournaments\/new\/?$/, view: tournamentNewView, nav: "tournaments" },
  { re: /^\/tournament\/([^/]+)\/?$/, view: tournamentDetailView, nav: "tournaments" },
  { re: /^\/players\/?$/, view: playersView, nav: "players" },
  { re: /^\/player\/([^/]+)\/?$/, view: playerDetailView, nav: "players" },
  { re: /^\/play\/?$/, view: play, nav: "play" },
  { re: /^\/connect\/?$/, view: connect, nav: "connect" },
];

const viewEl = document.getElementById("view");
let cleanup = null;
let currentPath = null;

function parseHash() {
  let h = location.hash.replace(/^#/, "");
  if (!h.startsWith("/")) h = `/${h}`;
  const [path, qs] = h.split("?");
  return { path, query: new URLSearchParams(qs || "") };
}

async function route() {
  const { path, query } = parseHash();
  if (cleanup) {
    try { cleanup(); } catch (e) { console.error(e); }
    cleanup = null;
  }
  const samePage = currentPath === path;
  currentPath = path;
  let match = null;
  let params = [];
  for (const r of routes) {
    const m = path.match(r.re);
    if (m) { match = r; params = m.slice(1).map(decodeURIComponent); break; }
  }
  document.querySelectorAll("[data-nav]").forEach((a) => {
    const active = match && a.dataset.nav === match.nav;
    a.classList.toggle("active", !!active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  viewEl.className = "container";
  if (!match) {
    document.title = "Not found · agentchess";
    render(viewEl, html`<div class="page-head"><h1>Page not found</h1></div>
      <div class="empty">Nothing here. <a href="#/">Back to the leaderboard</a>.</div>`);
    return;
  }
  try {
    // Each view gets a fresh host element so its DOM listeners die with it.
    const host = document.createElement("div");
    host.className = "view";
    viewEl.replaceChildren(host);
    const res = match.view.mount(host, params, query);
    cleanup = typeof res === "function" ? res : null;
  } catch (e) {
    console.error(e);
    render(viewEl, errorBanner(e));
  }
  if (!samePage) {
    window.scrollTo(0, 0);
    viewEl.focus({ preventScroll: true });
  }
}

window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ theme
const themeBtn = document.getElementById("theme-toggle");
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  themeBtn.setAttribute("aria-label", t === "dark" ? "Switch to light theme" : "Switch to dark theme");
}
applyTheme(document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");
themeBtn.addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem("agentchess-theme", next); } catch (_) { /* storage unavailable */ }
  window.dispatchEvent(new CustomEvent("themechange"));
});

// ------------------------------------------------------------------ connection indicator
const connEl = document.getElementById("conn");
const LABELS = { open: "live", connecting: "connecting", reconnecting: "reconnecting", offline: "offline" };
ws.on("_status", (s) => {
  connEl.dataset.state = s;
  connEl.querySelector(".conn-label").textContent = LABELS[s] || s;
  connEl.title = s === "open" ? "Receiving live updates" : "Live updates disconnected — click to retry";
});
connEl.addEventListener("click", () => ws.reconnectNow());

// ------------------------------------------------------------------ live-games counter in the nav
const liveCountEl = document.getElementById("live-count");
const running = new Set();
function showLiveCount() {
  liveCountEl.hidden = running.size === 0;
  liveCountEl.textContent = String(running.size);
}
async function refreshLiveCount() {
  try {
    const data = await get("live");
    running.clear();
    for (const g of (data && data.games) || []) running.add(g.id);
    showLiveCount();
  } catch (_) { /* the nav badge is best-effort */ }
}
ws.on("game_started", (e) => { if (e.game && e.game.id) { running.add(e.game.id); showLiveCount(); } });
ws.on("game_finished", (e) => { if (e.game && e.game.id) { running.delete(e.game.id); showLiveCount(); } });
ws.on("_reconnect", refreshLiveCount);

ws.connect();
refreshLiveCount();
route();
