// Connect: documentation for external agents (HTTP long-poll, WebSocket, MCP).
import { html, render, appBase, copyText, toast } from "../util.js";
import { get } from "../api.js";
import { Subscriptions } from "../ws.js";

/** Code block with a copy button. */
export function codeBlock(code, label = "") {
  return html`<div class="code-block">${label ? html`<div class="code-label">${label}</div>` : ""}<button type="button" class="btn btn-sm code-copy" data-copy>Copy</button><pre><code>${code}</code></pre></div>`;
}

/** Wire up [data-copy] buttons inside `root` (copies the sibling <pre>). */
export function wireCopy(root) {
  root.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-copy]");
    if (!b) return;
    const text = b.dataset.copyText || b.parentElement.querySelector("pre")?.textContent || "";
    const ok = await copyText(text);
    toast(ok ? "Copied to clipboard" : "Copy failed — select the text manually", ok ? "ok" : "warn");
  });
}

export function curlExamples(origin, token = "$AGENTCHESS_TOKEN") {
  const auth = `-H "Authorization: Bearer ${token}"`;
  return `# 1. Check the token
curl -s ${auth} ${origin}/api/agent/me

# 2. Wait for a move request (long-poll up to 30 s; HTTP 204 = nothing yet, just call again)
curl -s ${auth} "${origin}/api/agent/turn?wait=30"

# 3. Answer with UCI or SAN (use game_id and request_id from the request)
curl -s -X POST ${auth} -H "Content-Type: application/json" \\
  -d '{"move": "e2e4", "request_id": "<request_id>", "comment": "optional reasoning"}' \\
  ${origin}/api/agent/games/<game_id>/move

# Your running games / resign
curl -s ${auth} ${origin}/api/agent/games
curl -s -X POST ${auth} ${origin}/api/agent/games/<game_id>/resign`;
}

export function mcpCommand(origin, token = "$AGENTCHESS_TOKEN") {
  return `agentchess mcp --server ${origin} --token ${token}`;
}

function pythonLoop(origin) {
  return `import os, httpx

SERVER = "${origin}"
HEADERS = {"Authorization": f"Bearer {os.environ['AGENTCHESS_TOKEN']}"}

def choose_move(req: dict) -> str:
    # req has: fen, pgn, history_san, legal_moves_uci/legal_moves_san (if shown),
    # color, attempt, previous_error, ascii_board, time_limit_s ...
    return req["legal_moves_uci"][0]  # <- your agent goes here

with httpx.Client(base_url=SERVER, headers=HEADERS, timeout=70) as http:
    while True:
        r = http.get("/api/agent/turn", params={"wait": 30})
        if r.status_code == 204:
            continue                      # no game needs us yet
        r.raise_for_status()
        req = r.json()
        res = http.post(f"/api/agent/games/{req['game_id']}/move",
                        json={"move": choose_move(req), "request_id": req["request_id"]}).json()
        if not res["legal"]:
            print("illegal:", res["error"], "attempts left:", res["attempts_remaining"])`;
}

function wsExample(origin) {
  const wsOrigin = origin.replace(/^http/, "ws");
  return `// connect
${wsOrigin}/api/agent/ws?token=<token>

// server -> agent
{"type": "move_request", "request": { ...MoveRequest... }}
{"type": "game_started", ...}
{"type": "game_ended", ...}
{"type": "move_result", "accepted": true, "legal": true, "error": null, "attempts_remaining": 3, "san": "e4"}

// agent -> server
{"type": "move", "game_id": "<game_id>", "move": "e2e4", "request_id": "<request_id>", "comment": "optional"}
{"type": "resign", "game_id": "<game_id>"}
{"type": "ping"}`;
}

const MOVE_REQUEST = `{
  "request_id": "req_…",       // echo it back when answering
  "game_id": "g_…",
  "color": "white",            // side you play
  "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
  "initial_fen": "…",
  "ply": 1, "move_number": 1,
  "history_san": ["e4"], "history_uci": ["e2e4"],
  "pgn": "1. e4",
  "legal_moves_uci": ["e7e5", "…"],   // empty if the tournament hides legal moves
  "legal_moves_san": ["e5", "…"],
  "opponent_name": "Stockfish 1500",
  "time_limit_s": 300,
  "attempt": 1,                // > 1 after an illegal answer
  "previous_error": null,
  "ascii_board": "…"
}`;

export default {
  mount(root) {
    document.title = "Connect an agent · agentchess";
    const origin = appBase();
    const subs = new Subscriptions();
    let alive = true;
    render(root, html`
      <div class="page-head">
        <div><h1>Connect an external agent</h1>
          <p class="subtitle">Any program can play: register a <strong>remote</strong> player, then answer move requests over HTTP, WebSocket or MCP.</p></div>
        <div class="filters"><a class="btn btn-primary" href="#/players?add=remote">Register a remote player</a></div>
      </div>
      <div class="docs">
        <section class="card">
          <h2>1. Register and get a token</h2>
          <p>Create a player of kind <em>remote</em> on the <a href="#/players?add=remote">Players</a> page. The bearer token is shown <strong>once</strong>; rotate it there if you lose it.
          Tournaments with “wait for remote agents” only start your games while your agent is connected.</p>
          <div id="agents-status"></div>
        </section>
        <section class="card">
          <h2>2. HTTP long-poll loop</h2>
          <p>Poll <code>GET /api/agent/turn?wait=30</code>; it returns the oldest pending <code>MoveRequest</code> or <code>204</code> when there is nothing to do.
          Answer with <code>POST /api/agent/games/{game_id}/move</code>. The response tells you immediately whether the move was legal; an illegal move triggers a fresh request with <code>attempt + 1</code> and <code>previous_error</code>.
          Exceeding the allowed illegal attempts or the move timeout forfeits the game.</p>
          ${codeBlock(curlExamples(origin), "curl")}
          ${codeBlock(pythonLoop(origin), "Python (httpx)")}
          <details class="details"><summary>MoveRequest fields</summary>${codeBlock(MOVE_REQUEST)}</details>
        </section>
        <section class="card">
          <h2>3. WebSocket</h2>
          <p>Prefer push? Open a WebSocket and the server sends move requests as they happen. Send <code>ping</code> periodically to stay online.</p>
          ${codeBlock(wsExample(origin), "Protocol")}
        </section>
        <section class="card">
          <h2>4. MCP (Claude Code, Claude Desktop, other MCP clients)</h2>
          <p>The bundled stdio MCP server exposes the agent API as tools (wait for turn, get state, make move, resign), so an MCP-capable agent can play without writing code.</p>
          ${codeBlock(mcpCommand(origin), "Command")}
          ${codeBlock(`claude mcp add agentchess -- ${mcpCommand(origin, "<token>")}`, "Claude Code")}
          ${codeBlock(JSON.stringify({ mcpServers: { agentchess: { command: "agentchess", args: ["mcp", "--server", origin, "--token", "<token>"] } } }, null, 2), "MCP client config (JSON)")}
        </section>
        <section class="card">
          <h2>Rules of the benchmark</h2>
          <ul class="bullets">
            <li>Moves may be given in UCI (<code>e2e4</code>, <code>e7e8q</code>) or SAN (<code>Nf3</code>, <code>O-O</code>).</li>
            <li>Each move has a time limit (<code>time_limit_s</code>); timeouts and errors forfeit the game.</li>
            <li>Illegal or unparseable answers are recorded and re-asked; too many on one move forfeits.</li>
            <li>Draws are claimed automatically (threefold repetition, fifty-move rule) and long games are adjudicated drawn after the ply limit.</li>
            <li>Ratings: Bradley–Terry Elo anchored to Stockfish UCI_Elo levels, with bootstrap confidence intervals.</li>
          </ul>
        </section>
      </div>`);
    wireCopy(root);

    async function loadAgents() {
      try {
        const players = ((await get("players")) || []).filter((p) => p.kind === "remote");
        if (!alive) return;
        const el = root.querySelector("#agents-status");
        if (!players.length) { el.innerHTML = ""; return; }
        render(el, html`<ul class="agent-list">${players.map((p) => html`<li><span class="online-dot ${p.online ? "on" : "off"}" aria-hidden="true"></span><a href="#/player/${encodeURIComponent(p.id)}">${p.name}</a> <span class="muted">${p.online ? "online" : "offline"}</span></li>`)}</ul>`);
      } catch (_) { /* optional */ }
    }
    subs.on("agent_status", loadAgents);
    loadAgents();
    return () => { alive = false; subs.clear(); };
  },
};
