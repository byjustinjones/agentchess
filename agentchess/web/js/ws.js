// GUI event stream client: auto-reconnect with exponential backoff + tiny pub/sub.

const listeners = new Map(); // type -> Set<fn>
let socket = null;
let attempt = 0;
let reconnectTimer = null;
let status = "connecting";
let everConnected = false;

function emit(type, data) {
  for (const key of [type, "*"]) {
    const set = listeners.get(key);
    if (!set) continue;
    for (const fn of Array.from(set)) {
      try { fn(data); } catch (e) { console.error("event handler failed", type, e); }
    }
  }
}

function setStatus(s) {
  status = s;
  emit("_status", s);
}

export function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const dir = location.pathname.replace(/[^/]*$/, "");
  return `${proto}//${location.host}${dir}ws`;
}

export function connect() {
  clearTimeout(reconnectTimer);
  setStatus(attempt === 0 ? "connecting" : "reconnecting");
  try {
    socket = new WebSocket(wsUrl());
  } catch (e) {
    scheduleReconnect();
    return;
  }
  socket.onopen = () => {
    const wasReconnect = everConnected;
    attempt = 0;
    everConnected = true;
    setStatus("open");
    // Views refetch their state after a reconnect because events may have been missed.
    if (wasReconnect) emit("_reconnect", {});
  };
  socket.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg && typeof msg.type === "string") emit(msg.type, msg);
  };
  socket.onclose = () => {
    socket = null;
    scheduleReconnect();
  };
  socket.onerror = () => { /* onclose follows */ };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  attempt += 1;
  const delay = Math.min(30000, 500 * 2 ** Math.min(attempt, 8)) * (0.75 + Math.random() * 0.5);
  setStatus("offline");
  reconnectTimer = setTimeout(connect, delay);
}

/** Reconnect right away (e.g. user clicked the status indicator). */
export function reconnectNow() {
  if (socket && socket.readyState <= 1) return;
  attempt = 0;
  connect();
}

export const getStatus = () => status;

/** Subscribe to an event type ("*" for all). Returns an unsubscribe function. */
export function on(type, fn) {
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type).add(fn);
  return () => listeners.get(type)?.delete(fn);
}

/** Collects subscriptions so a view can drop them all at unmount. */
export class Subscriptions {
  constructor() { this.offs = []; }
  on(type, fn) { this.offs.push(on(type, fn)); return this; }
  add(off) { this.offs.push(off); return this; }
  clear() { this.offs.forEach((f) => f()); this.offs = []; }
}

// Pause/resume reconnect attempts when the tab becomes visible again.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !socket) reconnectNow();
});
