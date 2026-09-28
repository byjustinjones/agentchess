// Shared view fragments: standings table, Elo CI chart, progress bars, game lists.
import {
  html, raw, esc, enc, fmtNum, fmtPct, fmtSecs, fmtTokens, fmtCost, fmtResult, fmtTermination,
  kindBadge, statusBadge, timeAgo,
} from "./util.js";

/** Flatten a per-player stats entry (move_stats + termination_stats, possibly nested). */
export function normStats(s) {
  if (!s || typeof s !== "object") return {};
  const out = { ...s };
  for (const k of ["move_stats", "moves_stats", "termination_stats", "terminations"]) {
    if (s[k] && typeof s[k] === "object") Object.assign(out, s[k]);
  }
  return out;
}

export function forfeitsOf(st) {
  return (Number(st.forfeit_illegal) || 0) + (Number(st.forfeit_timeout) || 0) + (Number(st.forfeit_error) || 0);
}

const halfWidth = (r) => (Number(r.ci_high) - Number(r.ci_low)) / 2;

/** Leaderboard/standings table. rows: RatingRow[]; stats: {player_id: stats}. */
export function standingsTable(rows, stats = {}, { compact = false } = {}) {
  if (!rows || !rows.length) {
    return html`<div class="empty">No rated games yet — <a href="#/tournaments/new">create a tournament</a> or <a href="#/play">play a match</a>.</div>`;
  }
  const lo = Math.min(...rows.map((r) => Number(r.ci_low)));
  const hi = Math.max(...rows.map((r) => Number(r.ci_high)));
  const span = Math.max(1, hi - lo);
  const anyTokens = rows.some((r) => {
    const s = normStats(stats[r.player_id]);
    return Number(s.input_tokens) || Number(s.output_tokens);
  });
  const anyCost = rows.some((r) => Number(normStats(stats[r.player_id]).cost_usd));

  const body = rows.map((r) => {
    const st = normStats(stats[r.player_id]);
    const moves = Number(st.moves) || 0;
    const illegal = Number(st.illegal_attempts) || 0;
    const forfeits = forfeitsOf(st);
    const hw = halfWidth(r);
    const left = ((Number(r.ci_low) - lo) / span) * 100;
    const width = Math.max(0.8, ((Number(r.ci_high) - Number(r.ci_low)) / span) * 100);
    const dot = ((Number(r.elo) - lo) / span) * 100;
    const tokens = (Number(st.input_tokens) || 0) + (Number(st.output_tokens) || 0);
    const forfeitTitle = `illegal: ${st.forfeit_illegal || 0}, timeout: ${st.forfeit_timeout || 0}, error: ${st.forfeit_error || 0}, resigned: ${st.resigned || 0}`;
    return html`<tr>
      <td class="num rank">${r.rank ?? ""}</td>
      <td class="player-cell">
        <a href="#/player/${enc(r.player_id)}">${r.name || r.player_id}</a>
        ${kindBadge(r.kind)}
        ${r.anchored ? html`<span class="anchor" title="Anchored: rating fixed to the engine's configured Elo">⚓</span>` : ""}
      </td>
      <td class="num elo"><strong>${fmtNum(r.elo)}</strong></td>
      <td class="ci-cell" title="95% CI: ${fmtNum(r.ci_low)} – ${fmtNum(r.ci_high)}">
        <span class="ci-text">${r.anchored ? "fixed" : raw(`±${esc(fmtNum(hw))}`)}</span>
        <span class="ci-bar" aria-hidden="true"><span class="ci-range" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%"></span><span class="ci-dot" style="left:${dot.toFixed(2)}%"></span></span>
      </td>
      <td class="num">${fmtNum(r.games)}</td>
      <td class="num wdl"><span class="w">${r.wins ?? 0}</span>/<span class="d">${r.draws ?? 0}</span>/<span class="l">${r.losses ?? 0}</span></td>
      <td class="num">${fmtPct(r.score, 1)}</td>
      ${compact ? "" : html`<td class="num" title="${illegal} illegal attempts over ${moves} moves">${moves ? fmtPct(illegal / moves, 1) : "–"}</td>
      <td class="num" title="${forfeitTitle}">${forfeits || (moves ? "0" : "–")}</td>
      <td class="num">${fmtSecs(st.avg_move_s)}</td>
      ${anyTokens ? html`<td class="num" title="in ${fmtNum(st.input_tokens)} / out ${fmtNum(st.output_tokens)}">${fmtTokens(tokens)}</td>` : ""}
      ${anyCost ? html`<td class="num">${fmtCost(st.cost_usd)}</td>` : ""}`}
    </tr>`;
  });

  return html`<div class="table-wrap"><table class="table standings">
    <thead><tr>
      <th class="num" scope="col">#</th>
      <th scope="col">Player</th>
      <th class="num" scope="col">Elo</th>
      <th scope="col" title="95% bootstrap confidence interval">95% CI</th>
      <th class="num" scope="col">Games</th>
      <th class="num" scope="col" title="Wins / draws / losses">W/D/L</th>
      <th class="num" scope="col">Score</th>
      ${compact ? "" : html`<th class="num" scope="col" title="Illegal move attempts per move played">Illegal</th>
      <th class="num" scope="col" title="Games lost by forfeit (illegal moves, timeout, error)">Forfeits</th>
      <th class="num" scope="col">Avg move</th>
      ${anyTokens ? html`<th class="num" scope="col">Tokens</th>` : ""}
      ${anyCost ? html`<th class="num" scope="col">Cost</th>` : ""}`}
    </tr></thead>
    <tbody>${body}</tbody>
  </table></div>`;
}

const KIND_ORDER = ["engine", "llm", "remote", "random"];

let measureCtx = null;
/** Pixel width of a chart label (12.5px UI font). */
function measureText(s) {
  if (!measureCtx) {
    measureCtx = document.createElement("canvas").getContext("2d");
    const family = getComputedStyle(document.body).fontFamily || "sans-serif";
    measureCtx.font = `12.5px ${family}`;
  }
  return measureCtx.measureText(s).width;
}

/** Horizontal Elo ± CI chart (SVG) sized to `width` px. */
export function ciChartSvg(rows, width) {
  if (!rows || !rows.length) return "";
  const W = Math.max(280, Math.floor(width));
  const labelW = Math.min(170, Math.max(96, W * 0.3));
  const padR = 16;
  const rowH = 24;
  const top = 8;
  const axisH = 26;
  const H = top + rows.length * rowH + axisH;
  let lo = Math.min(...rows.map((r) => Number(r.ci_low)));
  let hi = Math.max(...rows.map((r) => Number(r.ci_high)));
  if (hi - lo < 100) { const m = (hi + lo) / 2; lo = m - 50; hi = m + 50; }
  const range = hi - lo;
  const stepCands = [25, 50, 100, 200, 250, 500, 1000];
  const plotW = W - labelW - padR;
  const step = stepCands.find((s) => (range / s) <= Math.max(3, Math.floor(plotW / 70))) || 1000;
  lo = Math.floor(lo / step) * step;
  hi = Math.ceil(hi / step) * step;
  const x = (v) => labelW + ((v - lo) / (hi - lo)) * plotW;
  const maxW = labelW - 16;
  const trunc = (s) => {
    if (measureText(s) <= maxW) return s;
    let lo2 = 0;
    let hi2 = s.length;
    while (lo2 < hi2) {
      const mid = Math.ceil((lo2 + hi2) / 2);
      if (measureText(`${s.slice(0, mid)}…`) <= maxW) lo2 = mid; else hi2 = mid - 1;
    }
    return `${s.slice(0, lo2)}…`;
  };

  const parts = [];
  for (let v = lo; v <= hi + 1e-9; v += step) {
    const xv = x(v).toFixed(1);
    parts.push(`<line class="grid" x1="${xv}" x2="${xv}" y1="${top}" y2="${H - axisH}"/>`);
    parts.push(`<text class="tick" x="${xv}" y="${H - axisH + 16}" text-anchor="middle">${v}</text>`);
  }
  rows.forEach((r, i) => {
    const cy = top + i * rowH + rowH / 2;
    const kind = KIND_ORDER.includes(r.kind) ? r.kind : "other";
    const x1 = x(Number(r.ci_low)).toFixed(1);
    const x2 = x(Number(r.ci_high)).toFixed(1);
    const xe = x(Number(r.elo)).toFixed(1);
    const tip = `${r.name || r.player_id} (${r.kind}) — Elo ${Math.round(r.elo)}${r.anchored ? " (anchored)" : ` [${Math.round(r.ci_low)}, ${Math.round(r.ci_high)}]`} · ${r.games} games · score ${(Number(r.score) * 100).toFixed(1)}%`;
    parts.push(`<g class="ci-row" data-tip="${esc(tip)}">`);
    parts.push(`<rect class="hit" x="0" y="${cy - rowH / 2}" width="${W}" height="${rowH}"/>`);
    parts.push(`<text class="lbl" x="${labelW - 10}" y="${cy + 4}" text-anchor="end">${esc(trunc(r.name || r.player_id))}</text>`);
    if (!r.anchored) {
      parts.push(`<line class="whisker k-${kind}" x1="${x1}" x2="${x2}" y1="${cy}" y2="${cy}"/>`);
      parts.push(`<line class="whisker k-${kind}" x1="${x1}" x2="${x1}" y1="${cy - 4}" y2="${cy + 4}"/>`);
      parts.push(`<line class="whisker k-${kind}" x1="${x2}" x2="${x2}" y1="${cy - 4}" y2="${cy + 4}"/>`);
      parts.push(`<circle class="mark k-${kind}" cx="${xe}" cy="${cy}" r="5"/>`);
    } else {
      parts.push(`<path class="mark k-${kind}" d="M${xe} ${cy - 6.5}L${Number(xe) + 6.5} ${cy}L${xe} ${cy + 6.5}L${Number(xe) - 6.5} ${cy}Z"/>`);
    }
    parts.push("</g>");
  });
  return `<svg class="ci-chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Elo rating with 95% confidence interval per player">${parts.join("")}</svg>`;
}

/** Container for a CI chart; call mountCiChart(el, rows) after rendering. Returns a cleanup fn. */
export function mountCiChart(host, rows) {
  if (!host) return () => {};
  const kinds = KIND_ORDER.filter((k) => rows.some((r) => r.kind === k));
  const legend = kinds.map((k) => `<span class="lg"><span class="lg-dot k-${k}"></span>${k}</span>`).join("")
    + (rows.some((r) => r.anchored) ? `<span class="lg"><span class="lg-diamond"></span>anchored (fixed)</span>` : "");
  host.innerHTML = `<div class="chart-legend">${legend}</div><div class="chart-canvas"></div><div class="chart-tip" hidden></div>`;
  const canvas = host.querySelector(".chart-canvas");
  const tipEl = host.querySelector(".chart-tip");
  const draw = () => { canvas.innerHTML = ciChartSvg(rows, canvas.clientWidth || 600); };
  draw();
  let lastW = canvas.clientWidth;
  const ro = new ResizeObserver(() => {
    if (Math.abs(canvas.clientWidth - lastW) > 4) { lastW = canvas.clientWidth; draw(); }
  });
  ro.observe(canvas);
  const move = (e) => {
    const g = e.target.closest && e.target.closest(".ci-row");
    canvas.querySelectorAll(".ci-row.hover").forEach((n) => n !== g && n.classList.remove("hover"));
    if (!g) { tipEl.hidden = true; return; }
    g.classList.add("hover");
    tipEl.textContent = g.getAttribute("data-tip");
    tipEl.hidden = false;
    const hr = host.getBoundingClientRect();
    const tw = tipEl.offsetWidth;
    let left = e.clientX - hr.left + 12;
    if (left + tw > hr.width) left = Math.max(0, e.clientX - hr.left - tw - 12);
    tipEl.style.left = `${left}px`;
    tipEl.style.top = `${e.clientY - hr.top + 14}px`;
  };
  const leave = () => { tipEl.hidden = true; canvas.querySelectorAll(".ci-row.hover").forEach((n) => n.classList.remove("hover")); };
  canvas.addEventListener("pointermove", move);
  canvas.addEventListener("pointerleave", leave);
  return () => ro.disconnect();
}

/** Stacked progress bar for tournament progress {scheduled, running, finished, aborted, total}. */
export function progressBar(p) {
  p = p || {};
  const total = Number(p.total) || 0;
  const fin = Number(p.finished) || 0;
  const run = Number(p.running) || 0;
  const ab = Number(p.aborted) || 0;
  const pct = (n) => (total ? (n / total) * 100 : 0).toFixed(2);
  const done = fin + ab;
  return html`<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="${total}" aria-valuenow="${done}" aria-label="${done} of ${total} games done">
      <span class="pseg pseg-fin" style="width:${pct(fin)}%"></span><span class="pseg pseg-run" style="width:${pct(run)}%"></span><span class="pseg pseg-ab" style="width:${pct(ab)}%"></span>
    </div>
    <div class="progress-label">${fin}/${total} finished${run ? ` · ${run} running` : ""}${ab ? ` · ${ab} aborted` : ""}</div>`;
}

export function resultClass(result, forWhite) {
  if (result === "1/2-1/2") return "res-d";
  if (result === "1-0") return forWhite ? "res-w" : "res-l";
  if (result === "0-1") return forWhite ? "res-l" : "res-w";
  return "";
}

/** Table of games. perspective: optional player id to show W/L colouring from their side. */
export function gamesTable(games, { perspective = null, showRound = false, emptyText = "No games yet." } = {}) {
  if (!games || !games.length) return html`<div class="empty">${emptyText}</div>`;
  // ply_count is only meaningful when the list endpoint loads moves; hide the column otherwise.
  const showMoves = games.some((g) => Number(g.ply_count) > 0);
  const rows = games.map((g) => {
    let resCls = "";
    if (perspective && g.result) resCls = resultClass(g.result, g.white_id === perspective);
    const moves = g.ply_count != null ? Math.ceil(Number(g.ply_count) / 2) : "–";
    return html`<tr>
      ${showRound ? html`<td class="num">${g.round ?? ""}</td>` : ""}
      <td>${statusBadge(g.status)}</td>
      <td class="players-cell">
        <span class="side-dot white" aria-hidden="true"></span><a href="#/player/${enc(g.white_id)}">${g.white_name || g.white_id}</a>
        <span class="vs">vs</span>
        <span class="side-dot black" aria-hidden="true"></span><a href="#/player/${enc(g.black_id)}">${g.black_name || g.black_id}</a>
      </td>
      <td class="num result ${resCls}">${g.status === "finished" || g.result ? fmtResult(g.result) : g.status === "running" ? "…" : ""}</td>
      <td class="muted">${fmtTermination(g.termination)}</td>
      ${showMoves ? html`<td class="num">${moves}</td>` : ""}
      <td class="muted opening-cell">${g.opening ? g.opening.name : ""}</td>
      <td class="muted nowrap">${timeAgo(g.finished_at || g.started_at || g.created_at)}</td>
      <td><a class="btn btn-sm" href="#/game/${enc(g.id)}">${g.status === "running" ? "Watch" : "View"}</a></td>
    </tr>`;
  });
  return html`<div class="table-wrap"><table class="table games">
    <thead><tr>
      ${showRound ? html`<th class="num" scope="col">Rd</th>` : ""}
      <th scope="col">Status</th><th scope="col">Players</th><th class="num" scope="col">Result</th>
      <th scope="col">Termination</th>${showMoves ? html`<th class="num" scope="col">Moves</th>` : ""}<th scope="col">Opening</th><th scope="col">When</th><th scope="col"><span class="sr-only">Actions</span></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

/** Standard loading placeholder. */
export const loading = (what = "Loading") => html`<div class="loading" aria-busy="true"><span class="spinner" aria-hidden="true"></span>${what}…</div>`;
