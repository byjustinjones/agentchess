"""Command line interface: ``agentchess serve|run|ratings|export-pgn|add-player|mcp``."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from agentchess.models import PlayerKind, PlayerSpec, TournamentConfig, TournamentStatus

DEFAULT_DB = "data/agentchess.db"


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


# ----------------------------------------------------------------- formatting
def format_ratings(rows: list[Any]) -> str:
    if not rows:
        return "(no rated games)"
    header = f"{'#':>3}  {'player':<28} {'elo':>6}  {'95% CI':>13}  {'games':>5}  {'W-D-L':>11}  {'score':>6}"
    lines = [header, "-" * len(header)]
    for r in rows:
        ci = "anchor" if r.anchored else f"{r.ci_low:.0f}..{r.ci_high:.0f}"
        wdl = f"{r.wins}-{r.draws}-{r.losses}"
        lines.append(f"{r.rank:>3}  {r.name[:28]:<28} {r.elo:>6.0f}  {ci:>13}  {r.games:>5}  {wdl:>11}  "
                     f"{100 * r.score:>5.1f}%")
    return "\n".join(lines)


def format_crosstable(ct: dict[str, Any], names: dict[str, str]) -> str:
    ids = ct.get("players", [])
    if not ids:
        return ""
    short = {pid: (names.get(pid, pid))[:14] for pid in ids}
    width = max(8, max(len(s) for s in short.values()))
    lines = [" " * (width + 2) + " ".join(f"{i + 1:>7}" for i in range(len(ids)))]
    for i, a in enumerate(ids):
        cells = []
        for b in ids:
            c = ct.get("cells", {}).get(a, {}).get(b)
            cells.append("-" if a == b or not c or not c.get("games") else f"{c['score']:g}/{c['games']}")
        lines.append(f"{i + 1:>2} {short[a]:<{width}} " + " ".join(f"{x:>7}" for x in cells))
    return "\n".join(lines)


# ----------------------------------------------------------------- run (YAML)
def load_run_config(path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import yaml

    from agentchess.server.registry import find_preset

    data = yaml.safe_load(Path(path).read_text()) or {}
    players: list[dict[str, Any]] = []
    for item in data.get("players") or []:
        if isinstance(item, str):
            preset = find_preset(item)
            if preset is None:
                raise SystemExit(f"unknown preset player {item!r}")
            players.append(dict(preset))
        elif isinstance(item, dict):
            base = dict(find_preset(item["preset"]) or {}) if item.get("preset") else {}
            if item.get("preset") and not base:
                raise SystemExit(f"unknown preset {item['preset']!r}")
            base.update({k: v for k, v in item.items() if k != "preset"})
            if "id" not in base and "name" not in base:
                raise SystemExit(f"player entry needs an id or name: {item}")
            players.append(base)
        else:
            raise SystemExit(f"invalid player entry: {item!r}")
    tournament = dict(data.get("tournament") or {})
    return players, tournament


def upsert_players(db: Any, entries: list[dict[str, Any]]) -> list[PlayerSpec]:
    """Create or update players from YAML/preset dicts. Prints tokens of new remote players."""
    from agentchess.server.registry import (
        build_spec, default_concurrency, hash_token, new_token, parse_kind, slugify, validate_config)

    out = []
    for e in entries:
        pid = slugify(e.get("id") or e["name"])
        kind = parse_kind(e.get("kind", "random"))
        existing = db.get_player(pid)
        if existing is not None:
            if existing.kind != kind:
                raise SystemExit(f"player {pid} already exists with kind {existing.kind.value}")
            existing.name = e.get("name", existing.name)
            existing.config = validate_config(kind, e.get("config") or {})
            existing.anchor_elo = e.get("anchor_elo")
            existing.max_concurrent_games = int(e.get("max_concurrent_games") or default_concurrency(kind))
            existing.active = True
            db.update_player(existing)
            out.append(existing)
            continue
        spec = build_spec(db, id=pid, name=e.get("name", pid), kind=kind, config=e.get("config"),
                          anchor_elo=e.get("anchor_elo"), max_concurrent_games=e.get("max_concurrent_games"))
        token = new_token() if kind == PlayerKind.REMOTE else None
        db.add_player(spec, token_hash=hash_token(token) if token else None)
        if token:
            print(f"remote player {spec.id}: token {token}  (shown once)")
        out.append(spec)
    return out


async def _run_async(args: argparse.Namespace) -> int:
    from agentchess.server.app import Runtime, Settings, create_app

    entries, tconf = load_run_config(args.config)
    server = None
    server_task = None
    if args.serve:
        import uvicorn

        app = create_app(Settings(db_path=args.db, host=args.host, port=args.port, resume=False, seed=False))
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
        server_task = asyncio.create_task(server.serve())
        while not server.started:
            if server_task.done():
                server_task.result()
                return 1
            await asyncio.sleep(0.05)
        rt: Runtime = app.state.runtime
        print(f"server listening on http://{args.host}:{args.port}")
    else:
        rt = Runtime.open(args.db)
        await rt.start(resume=False)

    db, manager = rt.db, rt.manager
    try:
        players = upsert_players(db, entries)
        name = tconf.pop("name", None) or Path(args.config).stem
        tconf.setdefault("player_ids", [p.id for p in players])
        remote = [p.id for p in players if p.kind == PlayerKind.REMOTE and p.id in tconf["player_ids"]]
        if remote and not args.serve:
            _err(f"warning: remote players {remote} need a server to connect to; use --serve "
                 "(otherwise their games wait for them forever)")
        try:
            cfg = TournamentConfig.from_dict(tconf)
            t = manager.create(name, cfg)
        except (TypeError, ValueError, KeyError) as e:
            _err(f"invalid tournament: {e}")
            return 2
        names = {p.id: p.name for p in db.list_players(include_inactive=True)}
        total = db.tournament_progress(t.id)["total"]
        print(f"tournament {t.id} '{name}': {len(cfg.player_ids)} players, {total} games")

        q = rt.bus.subscribe()
        await manager.start(t.id)
        done = 0
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                ev = None
            if ev and ev.get("type") == "game_finished" and ev["game"].get("tournament_id") == t.id:
                g = ev["game"]
                done += 1
                print(f"[{done}/{total}] {names.get(g['white_id'], g['white_id'])} {g.get('result') or '*'} "
                      f"{names.get(g['black_id'], g['black_id'])}  ({g.get('termination')}, {g.get('ply_count')} plies)"
                      + (f" - {g['termination_detail']}" if g.get('status') == 'aborted' and g.get('termination_detail') else ""),
                      flush=True)
            cur = db.get_tournament(t.id)
            if cur is None or cur.status in (TournamentStatus.FINISHED, TournamentStatus.CANCELLED):
                break
        rt.bus.unsubscribe(q)

        from agentchess.rating import compute_ratings, crosstable

        results = db.rated_results(t.id)
        rows = compute_ratings(results, db.list_players(include_inactive=True), bootstrap=args.bootstrap)
        print("\nStandings\n" + format_ratings(rows))
        print("\nCrosstable (score/games, row vs column)\n" + format_crosstable(crosstable(results, cfg.player_ids), names))
        if args.serve and args.linger:
            print("\ntournament finished; server still running (Ctrl-C to stop)")
            await server_task
        return 0
    finally:
        if server is not None:
            server.should_exit = True
            with contextlib.suppress(BaseException):
                await server_task
        else:
            await rt.stop()


def cmd_run(args: argparse.Namespace) -> int:
    try:
        return asyncio.run(_run_async(args))
    except KeyboardInterrupt:
        _err("interrupted; unfinished games were reset and resume with `agentchess serve`")
        return 130


# ------------------------------------------------------------------ commands
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from agentchess.server.app import Settings, create_app

    settings = Settings(db_path=args.db, host=args.host, port=args.port, resume=not args.no_resume,
                        seed=not args.no_seed, cors_origins=args.cors or [])
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level=args.log_level)
    return 0


def cmd_ratings(args: argparse.Namespace) -> int:
    from agentchess.db import Database
    from agentchess.rating import compute_ratings

    db = Database(args.db)
    try:
        if args.tournament and db.get_tournament(args.tournament) is None:
            _err(f"unknown tournament {args.tournament}")
            return 1
        results = db.rated_results(args.tournament)
        rows = compute_ratings(results, db.list_players(include_inactive=True),
                               use_anchors=not args.no_anchors, bootstrap=args.bootstrap)
        print(format_ratings(rows))
        print(f"\n{len(results)} rated games")
    finally:
        db.close()
    return 0


def cmd_export_pgn(args: argparse.Namespace) -> int:
    from agentchess.db import Database
    from agentchess.server.app import export_pgn_text

    db = Database(args.db)
    try:
        sys.stdout.write(export_pgn_text(db, args.tournament))
    finally:
        db.close()
    return 0


def cmd_add_player(args: argparse.Namespace) -> int:
    from agentchess.db import Database
    from agentchess.server.registry import RegistryError, register_player

    try:
        config = json.loads(args.config) if args.config else {}
    except json.JSONDecodeError as e:
        _err(f"--config is not valid JSON: {e}")
        return 2
    db = Database(args.db)
    try:
        spec, token = register_player(db, id=args.id, name=args.name, kind=args.kind, config=config,
                                      anchor_elo=args.anchor_elo, max_concurrent_games=args.max_concurrent)
    except RegistryError as e:
        _err(f"error: {e}")
        return 2
    finally:
        db.close()
    print(f"added player {spec.id} ({spec.kind.value})")
    if token:
        print(f"token: {token}")
        print("(store it now: only its hash is kept)")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    import os

    from agentchess.mcp_server import run_mcp

    token = args.token or os.environ.get("AGENTCHESS_TOKEN")
    if not token:
        _err("a token is required (--token or $AGENTCHESS_TOKEN)")
        return 2
    run_mcp(args.server, token)
    return 0


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentchess", description="Chess benchmark for LLM agents")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the web server (GUI + REST + agent API)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--no-resume", action="store_true", help="don't restart tournaments left running")
    s.add_argument("--no-seed", action="store_true", help="don't add default players to an empty DB")
    s.add_argument("--cors", action="append", metavar="ORIGIN", help="allowed CORS origin (repeatable)")
    s.add_argument("--log-level", default="info")
    s.set_defaults(func=cmd_serve)

    r = sub.add_parser("run", help="run a tournament from a YAML file (headless)")
    r.add_argument("config")
    r.add_argument("--db", default=DEFAULT_DB)
    r.add_argument("--serve", action="store_true", help="also run the HTTP server (GUI, remote agents)")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8000)
    r.add_argument("--linger", action="store_true", help="with --serve: keep serving after the tournament ends")
    r.add_argument("--bootstrap", type=int, default=200)
    r.set_defaults(func=cmd_run)

    ra = sub.add_parser("ratings", help="print the rating table")
    ra.add_argument("--db", default=DEFAULT_DB)
    ra.add_argument("--tournament")
    ra.add_argument("--no-anchors", action="store_true")
    ra.add_argument("--bootstrap", type=int, default=200)
    ra.set_defaults(func=cmd_ratings)

    e = sub.add_parser("export-pgn", help="write finished games as PGN to stdout")
    e.add_argument("--db", default=DEFAULT_DB)
    e.add_argument("--tournament")
    e.set_defaults(func=cmd_export_pgn)

    a = sub.add_parser("add-player", help="register a player (prints the token for remote players)")
    a.add_argument("--db", default=DEFAULT_DB)
    a.add_argument("--name", required=True)
    a.add_argument("--kind", required=True, choices=[k.value for k in PlayerKind])
    a.add_argument("--id")
    a.add_argument("--config", help="JSON object, e.g. '{\"uci_elo\": 1500}'")
    a.add_argument("--anchor-elo", type=float)
    a.add_argument("--max-concurrent", type=int)
    a.set_defaults(func=cmd_add_player)

    m = sub.add_parser("mcp", help="run the stdio MCP bridge for an MCP-capable agent")
    m.add_argument("--server", default="http://127.0.0.1:8000")
    m.add_argument("--token", help="remote player token (or $AGENTCHESS_TOKEN)")
    m.set_defaults(func=cmd_mcp)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # MCP speaks JSON-RPC on stdout: keep logs on stderr and quiet.
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO if args.command != "mcp" else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
