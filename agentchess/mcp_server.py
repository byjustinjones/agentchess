"""stdio MCP server that lets an MCP-capable agent (e.g. Claude Code) play chess.

It proxies to a running agentchess server's Agent API using a remote player's
token. Run it with ``agentchess mcp --server http://localhost:8000 --token ac_...``.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

INSTRUCTIONS = """\
You are playing chess on an agentchess server as a registered remote player.
Games are started by the server (tournaments or exhibition games); you just answer move requests.

Loop until told to stop:
1. Call `wait_for_turn` (it blocks up to ~55 s). If it says no move is requested yet, call it again.
2. Read the position it returns (FEN, board, move history, legal moves). Think carefully.
3. Call `make_move` with the game_id and your move in UCI (e.g. e2e4, e7e8q) or SAN (e.g. Nf3, O-O).
4. If the move was illegal, call `wait_for_turn` again: you get the same position with the error and
   the remaining attempts. Too many illegal moves or taking longer than the time limit forfeits the game.
5. Repeat from 1. You may be playing several games at once; always use the game_id from the request.
Use `list_games` / `get_game` to inspect games and `resign` only if the position is hopeless.
"""


def format_request(req: dict[str, Any]) -> str:
    """Human/LLM-readable description of a MoveRequest dict."""
    lines = [
        f"It is your move in game {req['game_id']} (request_id {req['request_id']}).",
        f"You play {req['color']} against {req.get('opponent_name') or 'the opponent'}.",
        f"Move number {req['move_number']}, ply {req['ply']}. Time limit: {req.get('time_limit_s', 0):.0f} s.",
        "",
        f"FEN: {req['fen']}",
    ]
    if req.get("ascii_board"):
        lines += ["", "Board (white pieces uppercase, rank 8 at top):", req["ascii_board"]]
    lines += ["", f"Moves so far: {req.get('pgn') or '(none - starting position)'}"]
    if req.get("legal_moves_san"):
        lines += [f"Legal moves (SAN): {' '.join(req['legal_moves_san'])}",
                  f"Legal moves (UCI): {' '.join(req.get('legal_moves_uci') or [])}"]
    if req.get("attempt", 1) > 1 or req.get("previous_error"):
        lines += ["", f"Attempt {req.get('attempt')}: your previous answer was rejected: {req.get('previous_error')}"]
    lines += ["", f'Answer with make_move(game_id="{req["game_id"]}", move="<your move>").']
    return "\n".join(lines)


def format_move_result(res: dict[str, Any]) -> str:
    if res.get("legal") and res.get("accepted"):
        return f"Move accepted: {res.get('san')}. Call wait_for_turn for your next move."
    if not res.get("accepted") and res.get("legal"):
        return f"Move not accepted: {res.get('error')}. Call wait_for_turn."
    return (f"ILLEGAL move: {res.get('error')}. Attempts remaining: {res.get('attempts_remaining')}. "
            "Call wait_for_turn to get the position again and choose a legal move.")


class AgentClient:
    """Thin async client for the Agent API."""

    def __init__(self, server_url: str, token: str) -> None:
        self.http = httpx.AsyncClient(base_url=server_url.rstrip("/"),
                                      headers={"Authorization": f"Bearer {token}"}, timeout=30.0)

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.http.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text
            raise RuntimeError(f"HTTP {r.status_code}: {detail}")
        return None if r.status_code == 204 else r.json()

    async def turn(self, wait: float) -> Optional[dict[str, Any]]:
        return await self._json("GET", "/api/agent/turn", params={"wait": wait}, timeout=wait + 15)

    async def move(self, game_id: str, move: str, comment: Optional[str] = None) -> dict[str, Any]:
        return await self._json("POST", f"/api/agent/games/{game_id}/move",
                                json={"move": move, "comment": comment or None})

    async def resign(self, game_id: str) -> dict[str, Any]:
        return await self._json("POST", f"/api/agent/games/{game_id}/resign")

    async def games(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/api/agent/games")

    async def game(self, game_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/api/agent/games/{game_id}")


def build_server(server_url: str, token: str):
    """Create the MCP server object (mcp 2.x ``MCPServer`` or 1.x ``FastMCP``)."""
    try:
        from mcp.server.mcpserver import MCPServer as Server
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as Server

    client = AgentClient(server_url, token)
    mcp = Server(name="agentchess", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def wait_for_turn(timeout_s: int = 55) -> str:
        """Wait (long-poll, up to timeout_s seconds, max 60) until one of your games needs a move.
        Returns the position (FEN, board, history, legal moves) and the game_id to answer with make_move."""
        try:
            req = await client.turn(float(min(max(timeout_s, 0), 60)))
        except Exception as e:
            return f"Error contacting the agentchess server: {e}"
        if req is None:
            return "No move requested yet (no game is waiting for you). Call wait_for_turn again."
        return format_request(req)

    @mcp.tool()
    async def make_move(game_id: str, move: str, comment: str = "") -> str:
        """Play a move in UCI (e2e4, e7e8q) or SAN (Nf3, O-O) for the pending request of game_id.
        Optional comment: short reasoning shown to spectators."""
        try:
            return format_move_result(await client.move(game_id, move, comment))
        except Exception as e:
            return f"Move rejected: {e}"

    @mcp.tool()
    async def get_game(game_id: str) -> str:
        """Show a game's state: players, status/result, moves, current FEN and whether it is your turn."""
        try:
            g = await client.game(game_id)
        except Exception as e:
            return f"Error: {e}"
        lines = [
            f"Game {g['id']}: {g.get('white_name')} (white) vs {g.get('black_name')} (black)",
            f"You play {g.get('color')}. Status: {g['status']}"
            + (f", result {g['result']} ({g.get('termination')})" if g.get("result") else ""),
            f"FEN: {g.get('fen')}",
            "Moves: " + (" ".join(m["san"] for m in g.get("moves", [])) or "(none)"),
        ]
        if g.get("request"):
            lines += ["", format_request(g["request"])]
        return "\n".join(lines)

    @mcp.tool()
    async def list_games() -> str:
        """List your running games (color, opponent, FEN, whether it is your turn)."""
        try:
            games = await client.games()
        except Exception as e:
            return f"Error: {e}"
        if not games:
            return "You have no running games."
        return "\n".join(
            f"{g['game_id']}: {g['color']} vs {g['opponent']} - {'YOUR TURN' if g['your_turn'] else 'waiting'}"
            f" - FEN {g['fen']}" for g in games)

    @mcp.tool()
    async def resign(game_id: str) -> str:
        """Resign game_id (only possible when it is your turn). This loses the game."""
        try:
            res = await client.resign(game_id)
        except Exception as e:
            return f"Error: {e}"
        return "Resigned." if res.get("accepted") else f"Resignation not accepted: {json.dumps(res)}"

    return mcp


def run_mcp(server_url: str, token: str) -> None:
    """Run the MCP bridge on stdio (blocks)."""
    build_server(server_url, token).run("stdio")
