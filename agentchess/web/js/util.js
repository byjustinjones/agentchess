// Small helpers: escaping, safe HTML templating, formatting, UI feedback.

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;" };

/** Escape any value for safe insertion into HTML text or a quoted attribute. */
export function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value).replace(/[&<>"'`]/g, (c) => ESC[c]);
}

class SafeHTML {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}

/** Mark a string as trusted HTML (only use for markup we generated ourselves). */
export const raw = (s) => new SafeHTML(String(s ?? ""));

function renderValue(v) {
  if (v === null || v === undefined || v === false) return "";
  if (v instanceof SafeHTML) return v.s;
  if (Array.isArray(v)) return v.map(renderValue).join("");
  return esc(v);
}

/** Tagged template: every interpolation is escaped unless wrapped in raw()/html``. */
export function html(strings, ...values) {
  let out = strings[0];
  for (let i = 0; i < values.length; i++) out += renderValue(values[i]) + strings[i + 1];
  return new SafeHTML(out);
}

/** Replace an element's content with an html`` result. */
export function render(el, content) {
  el.innerHTML = renderValue(content);
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** encodeURIComponent shortcut for ids placed in URLs. */
export const enc = (s) => encodeURIComponent(String(s ?? ""));

export function debounce(fn, ms) {
  let t = null;
  const d = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  d.cancel = () => clearTimeout(t);
  return d;
}

// ---------------------------------------------------------------- formatting

export function fmtNum(n, digits = 0) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "–";
  return Number(n).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtPct(x, digits = 1) {
  if (x === null || x === undefined || !Number.isFinite(Number(x))) return "–";
  return `${(Number(x) * 100).toFixed(digits)}%`;
}

export function fmtSecs(s) {
  if (s === null || s === undefined || !Number.isFinite(Number(s))) return "–";
  s = Number(s);
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function fmtTokens(n) {
  if (!n) return "–";
  n = Number(n);
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e4) return `${(n / 1e3).toFixed(0)}k`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

export function fmtCost(usd) {
  if (usd === null || usd === undefined || !Number(usd)) return "–";
  usd = Number(usd);
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

export function fmtDate(ts) {
  if (!ts) return "–";
  const d = new Date(Number(ts) * 1000);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function timeAgo(ts) {
  if (!ts) return "–";
  const s = Date.now() / 1000 - Number(ts);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)} d ago`;
  return fmtDate(ts);
}

export function fmtResult(r) {
  if (r === "1/2-1/2") return "½–½";
  if (r === "1-0") return "1–0";
  if (r === "0-1") return "0–1";
  return r ? String(r) : "*";
}

const TERMINATIONS = {
  checkmate: "Checkmate",
  stalemate: "Stalemate",
  insufficient_material: "Insufficient material",
  fifty_moves: "50-move rule",
  threefold_repetition: "Threefold repetition",
  max_plies: "Move limit (adjudicated)",
  resignation: "Resignation",
  illegal_moves: "Forfeit: illegal moves",
  timeout: "Forfeit: timeout",
  error: "Forfeit: error",
  aborted: "Aborted",
};
export const fmtTermination = (t) => (t ? TERMINATIONS[t] || String(t).replace(/_/g, " ") : "");

export function kindBadge(kind) {
  return html`<span class="badge kind-${esc(kind || "unknown")}">${kind || "?"}</span>`;
}

export function statusBadge(status) {
  return html`<span class="status status-${esc(status || "unknown")}">${status || "?"}</span>`;
}

/** One-line human summary of a player's kind-specific config. */
export function configSummary(p) {
  const c = p.config || {};
  switch (p.kind) {
    case "engine": {
      const parts = [];
      if (c.uci_elo) parts.push(`UCI_Elo ${c.uci_elo}`);
      if (c.skill_level !== undefined && c.skill_level !== null) parts.push(`skill ${c.skill_level}`);
      if (c.depth) parts.push(`depth ${c.depth}`);
      if (c.nodes) parts.push(`${c.nodes} nodes`);
      if (c.movetime_ms) parts.push(`${c.movetime_ms} ms/move`);
      return parts.join(" · ") || "full strength";
    }
    case "llm": {
      const parts = [`${c.provider || "?"}/${c.model || "?"}`];
      if (c.effort) parts.push(`effort ${c.effort}`);
      if (c.max_tokens) parts.push(`${fmtTokens(c.max_tokens)} max tok`);
      if (c.temperature !== undefined && c.temperature !== null) parts.push(`T=${c.temperature}`);
      return parts.join(" · ");
    }
    case "remote":
      return "external agent (HTTP / WebSocket / MCP)";
    case "random":
      return "uniformly random legal moves";
    default:
      return Object.keys(c).length ? JSON.stringify(c) : "";
  }
}

// ---------------------------------------------------------------- feedback

export function errorBanner(err, { retry = false } = {}) {
  const msg = err && err.message ? err.message : String(err);
  return html`<div class="banner banner-error" role="alert">
    <strong>Something went wrong.</strong> <span>${msg}</span>
    ${retry ? html`<button type="button" class="btn btn-sm" data-action="retry">Retry</button>` : ""}
  </div>`;
}

let toastHost = null;
export function toast(message, kind = "info", ms = 3500) {
  if (!toastHost) {
    toastHost = document.createElement("div");
    toastHost.className = "toasts";
    toastHost.setAttribute("aria-live", "polite");
    document.body.appendChild(toastHost);
  }
  const t = document.createElement("div");
  t.className = `toast toast-${kind}`;
  t.textContent = message;
  toastHost.appendChild(t);
  setTimeout(() => {
    t.classList.add("leaving");
    setTimeout(() => t.remove(), 300);
  }, ms);
}

export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) { /* fall through */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  ta.remove();
  return ok;
}

/** Open a modal <dialog>. Returns {dialog, close}. `content` is html``. */
export function openModal(title, content, { wide = false, onClose = null } = {}) {
  const dlg = document.createElement("dialog");
  dlg.className = `modal${wide ? " modal-wide" : ""}`;
  dlg.setAttribute("aria-labelledby", "modal-title");
  render(dlg, html`<div class="modal-head">
      <h2 id="modal-title">${title}</h2>
      <button type="button" class="btn-icon" data-close aria-label="Close">✕</button>
    </div>
    <div class="modal-body">${content}</div>`);
  document.body.appendChild(dlg);
  const close = () => { if (dlg.open) dlg.close(); };
  dlg.addEventListener("close", () => { dlg.remove(); if (onClose) onClose(); });
  dlg.addEventListener("click", (e) => {
    if (e.target === dlg || e.target.closest("[data-close]")) close();
  });
  dlg.showModal();
  return { dialog: dlg, close };
}

export function confirmModal(title, message, confirmLabel = "Confirm", danger = true) {
  return new Promise((resolve) => {
    let answered = false;
    const { dialog, close } = openModal(title, html`<p>${message}</p>
      <div class="form-actions">
        <button type="button" class="btn" data-close>Cancel</button>
        <button type="button" class="btn ${danger ? "btn-danger" : "btn-primary"}" data-ok>${confirmLabel}</button>
      </div>`, { onClose: () => { if (!answered) resolve(false); } });
    dialog.querySelector("[data-ok]").addEventListener("click", () => { answered = true; resolve(true); close(); });
    dialog.querySelector("[data-ok]").focus();
  });
}

/** Base URL of the app (origin + directory), without trailing slash. */
export function appBase() {
  const dir = location.pathname.replace(/[^/]*$/, "");
  return (location.origin + dir).replace(/\/$/, "");
}
