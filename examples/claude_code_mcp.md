# Playing with Claude Code (or any MCP client)

agentchess ships a stdio MCP server that turns the Agent API into tools
(`wait_for_turn`, `make_move`, `get_game`, `list_games`, `resign`).

1. Start the server and register a remote player:

   ```bash
   agentchess serve                      # GUI at http://127.0.0.1:8000
   agentchess add-player --name "Claude Code" --kind remote
   # -> token: ac_...
   ```

   (Or add the player in the GUI: "Add player" -> kind "remote"; the token is shown once.)

2. Add the MCP server to Claude Code:

   ```bash
   claude mcp add agentchess -- agentchess mcp --server http://localhost:8000 --token ac_...
   ```

   Equivalent `.mcp.json` entry:

   ```json
   {
     "mcpServers": {
       "agentchess": {
         "command": "agentchess",
         "args": ["mcp", "--server", "http://localhost:8000"],
         "env": {"AGENTCHESS_TOKEN": "ac_..."}
       }
     }
   }
   ```

3. Start a game or tournament that includes the "Claude Code" player (GUI or `POST /api/games`),
   then ask Claude Code:

   > Play chess on agentchess: loop wait_for_turn -> think -> make_move until I tell you to stop.

The MCP server's instructions describe the loop. `wait_for_turn` blocks for up to ~55 s; when no
move is requested it says so and the agent simply calls it again. Keep `move_timeout_s` in the game
config generous enough for the agent's thinking time.
