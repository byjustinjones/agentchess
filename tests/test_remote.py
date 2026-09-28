import asyncio

import chess

from agentchess.events import EventBus
from agentchess.game import play_game
from agentchess.models import GameConfig, GameRecord, MoveRequest, MoveResponse, PlayerKind, PlayerSpec, Termination
from agentchess.players.base import GameEnd, GameStart
from agentchess.players.random_player import RandomPlayer
from agentchess.players.remote import AgentHub, RemotePlayer


def req(rid: str, game_id: str = "g1") -> MoveRequest:
    return MoveRequest(request_id=rid, game_id=game_id, color="white", fen=chess.STARTING_FEN,
                       initial_fen=chess.STARTING_FEN, ply=0, move_number=1, history_san=[], history_uci=[],
                       pgn="", legal_moves_uci=[], legal_moves_san=[], opponent_name="x", time_limit_s=10)


async def test_request_submit_roundtrip():
    hub = AgentHub()
    task = asyncio.create_task(hub.request_move("a", req("r1")))
    await asyncio.sleep(0)
    assert [r.request_id for r in hub.pending_requests("a")] == ["r1"]
    assert hub.find_request("a", "g1").request_id == "r1"
    assert hub.find_request("a", "other") is None
    assert hub.submit("a", "nope", MoveResponse(move="e4")) is False
    assert hub.submit("a", "r1", MoveResponse(move="e4")) is True
    assert (await task).move == "e4"
    assert hub.pending_requests("a") == []
    assert hub.submit("a", "r1", MoveResponse(move="d4")) is False  # already answered


async def test_cancel_removes_pending():
    hub = AgentHub()
    task = asyncio.create_task(hub.request_move("a", req("r1")))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert hub.pending_requests("a") == []
    assert hub.submit("a", "r1", MoveResponse(move="e4")) is False


async def test_wait_for_request_long_poll():
    hub = AgentHub()
    assert await hub.wait_for_request("a", 0.02) is None
    waiter = asyncio.create_task(hub.wait_for_request("a", 5))
    await asyncio.sleep(0.01)
    assert hub.is_online("a", within_s=0)  # currently polling
    mover = asyncio.create_task(hub.request_move("a", req("r1")))
    got = await asyncio.wait_for(waiter, 1)
    assert got.request_id == "r1"
    # already-delivered requests can be excluded (WS pushers)
    assert await hub.wait_for_request("a", 0.02, exclude={"r1"}) is None
    assert (await hub.wait_for_request("a", 0.02)).request_id == "r1"
    hub.submit("a", "r1", MoveResponse(move="e4"))
    await mover


async def test_one_pending_per_game():
    hub = AgentHub()
    t1 = asyncio.create_task(hub.request_move("a", req("r1")))
    await asyncio.sleep(0)
    t2 = asyncio.create_task(hub.request_move("a", req("r2")))
    t3 = asyncio.create_task(hub.request_move("a", req("r3", game_id="g2")))
    await asyncio.sleep(0)
    assert [r.request_id for r in hub.pending_requests("a")] == ["r2", "r3"]
    res = await asyncio.gather(t1, return_exceptions=True)
    assert isinstance(res[0], RuntimeError)
    hub.submit("a", "r2", MoveResponse(move="e4"))
    hub.submit("a", "r3", MoveResponse(move="d4"))
    assert (await t2).move == "e4" and (await t3).move == "d4"


async def test_notifications_and_status_events():
    bus = EventBus()
    q = bus.subscribe()
    hub = AgentHub(bus)
    assert await hub.next_notification("a", 0.01) is None
    hub.notify_game_start("a", GameStart("g1", "white", "b", "B", chess.STARTING_FEN))
    hub.notify_game_end("a", GameEnd("g1", "1-0", "checkmate"))
    n1 = await hub.next_notification("a", 0.1)
    n2 = await hub.next_notification("a", 0.1)
    assert n1["type"] == "game_started" and n1["game_id"] == "g1" and n1["color"] == "white"
    assert n2["type"] == "game_ended" and n2["result"] == "1-0"

    assert not hub.is_online("a")
    hub.connect("a")
    assert hub.is_online("a", within_s=0)
    hub.disconnect("a")
    assert hub.is_online("a")          # seen just now
    hub._last_seen["a"] -= 120         # simulate inactivity
    hub.sweep()
    events = [q.get_nowait() for _ in range(q.qsize())]
    statuses = [e["online"] for e in events if e["type"] == "agent_status" and e["player_id"] == "a"]
    assert statuses == [True, False]
    st = {s["player_id"]: s for s in hub.status()}
    assert st["a"]["online"] is False and st["a"]["connections"] == 0 and st["a"]["pending"] == 0


async def test_notification_waiter_is_woken():
    hub = AgentHub()
    waiter = asyncio.create_task(hub.next_notification("a", 5))
    await asyncio.sleep(0.01)
    hub.notify_game_start("a", GameStart("g1", "black", "b", "B", chess.STARTING_FEN))
    assert (await asyncio.wait_for(waiter, 1))["color"] == "black"


async def test_remote_player_in_game():
    hub = AgentHub()
    spec = PlayerSpec(id="agent", name="Agent", kind=PlayerKind.REMOTE)
    game = GameRecord(id="g-remote", white_id="agent", black_id="rnd",
                      config=GameConfig(max_plies=6, move_timeout_s=5))

    async def agent() -> None:
        rnd = RandomPlayer(PlayerSpec(id="x", name="x", kind=PlayerKind.RANDOM, config={"seed": 1}))
        answered = 0
        while answered < 3:
            r = await hub.wait_for_request("agent", 1)
            if r is None:
                continue
            if answered == 0 and r.attempt == 1:
                hub.submit("agent", r.request_id, MoveResponse(move="e2e5"))  # illegal: runner re-asks
                continue
            hub.submit("agent", r.request_id, await rnd.get_move(r))
            answered += 1

    agent_task = asyncio.create_task(agent())
    rnd = RandomPlayer(PlayerSpec(id="rnd", name="Rnd", kind=PlayerKind.RANDOM, config={"seed": 2}))
    await play_game(game, RemotePlayer(spec, hub), rnd)
    await agent_task
    assert game.termination == Termination.MAX_PLIES
    assert len(game.moves[0].illegal_attempts) == 1
    notes = [await hub.next_notification("agent", 0.1) for _ in range(2)]
    assert [n["type"] for n in notes] == ["game_started", "game_ended"]


async def test_remote_timeout_cleans_up():
    hub = AgentHub()
    spec = PlayerSpec(id="agent", name="Agent", kind=PlayerKind.REMOTE)
    game = GameRecord(id="g-to", white_id="agent", black_id="rnd", config=GameConfig(move_timeout_s=0.05))
    rnd = RandomPlayer(PlayerSpec(id="rnd", name="Rnd", kind=PlayerKind.RANDOM))
    await play_game(game, RemotePlayer(spec, hub), rnd)
    assert game.termination == Termination.TIMEOUT
    assert hub.pending_requests("agent") == []
