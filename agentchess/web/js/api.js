// Thin fetch wrapper for the agentchess REST API (relative URLs so the app also works under a path prefix).

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function detailMessage(body, status) {
  if (body && typeof body === "object" && "detail" in body) {
    const d = body.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      // FastAPI validation errors: [{loc, msg, type}]
      return d.map((e) => `${(e.loc || []).slice(1).join(".") || "request"}: ${e.msg}`).join("; ");
    }
    return JSON.stringify(d);
  }
  return `HTTP ${status}`;
}

export function apiUrl(path, query) {
  let url = `api/${path.replace(/^\/+/, "")}`;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    }
    const s = qs.toString();
    if (s) url += `?${s}`;
  }
  return url;
}

export async function api(path, { method = "GET", body, query } = {}) {
  const opts = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(apiUrl(path, query), opts);
  } catch (e) {
    throw new ApiError(`Cannot reach the server (${e.message || e}).`, 0);
  }
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (_) { data = text; }
  }
  if (!res.ok) throw new ApiError(`${method} /api/${path.replace(/^\/+/, "").split("?")[0]} failed: ${detailMessage(data, res.status)}`, res.status);
  return data;
}

export const get = (path, query) => api(path, { query });
export const post = (path, body) => api(path, { method: "POST", body: body === undefined ? {} : body });
export const patch = (path, body) => api(path, { method: "PATCH", body });
export const del = (path) => api(path, { method: "DELETE" });
