import json
from types import SimpleNamespace

import anthropic
import chess
import httpx
import pytest

from agentchess.game import play_game
from agentchess.models import GameConfig, GameRecord, MoveRequest, PlayerKind, PlayerSpec, Termination
from agentchess.moves import render_ascii
from agentchess.players.base import InfrastructureError
from agentchess.players.llm import DEFAULT_SYSTEM_PROMPT, LLMConfig, LLMPlayer, build_prompt, trim_comment

try:  # anthropic>=1.0 is built on httpx2
    import httpx2 as sdk_httpx
except ImportError:  # pragma: no cover
    sdk_httpx = httpx


def make_request(board: chess.Board | None = None, **kw) -> MoveRequest:
    board = board or chess.Board()
    legal = list(board.legal_moves)
    fields = dict(
        request_id="r1", game_id="g1", color="white" if board.turn else "black", fen=board.fen(),
        initial_fen=chess.STARTING_FEN, ply=len(board.move_stack), move_number=board.fullmove_number,
        history_san=[], history_uci=[], pgn="", legal_moves_uci=[m.uci() for m in legal],
        legal_moves_san=[board.san(m) for m in legal], opponent_name="Stockfish 1500", time_limit_s=120,
        ascii_board=render_ascii(board),
    )
    fields.update(kw)
    return MoveRequest(**fields)


def llm_spec(**config) -> PlayerSpec:
    return PlayerSpec(id="llm", name="LLM", kind=PlayerKind.LLM, config=config)


# ------------------------------------------------------------------ prompts
def test_build_prompt_contents():
    board = chess.Board()
    for san in ("e4", "e5", "Nf3"):
        board.push_san(san)
    req = make_request(board, history_san=["e4", "e5", "Nf3"], history_uci=["e2e4", "e7e5", "g1f3"],
                       pgn="1. e4 e5 2. Nf3")
    system, user = build_prompt(req)
    assert system == DEFAULT_SYSTEM_PROMPT and "MOVE: <move>" in system
    assert board.fen() in user
    assert "Black" in user and "Stockfish 1500" in user
    assert "1. e4 e5 2. Nf3" in user
    assert "last move: Nf3" in user
    assert "8 | r n b q k b n r |" in user and "3 | . . . . . N . . |" in user
    assert "Nc6" in user and "Legal moves (" in user
    assert "rejected" not in user
    assert "MOVE:" in user


def test_build_prompt_retry_no_legal_moves_and_custom_system():
    req = make_request(legal_moves_uci=[], legal_moves_san=[], attempt=2,
                       previous_error="illegal move e2e5 in this position", ascii_board="")
    system, user = build_prompt(req, system_prompt="Be brief.")
    assert system == "Be brief."
    assert "Legal moves" not in user
    assert "illegal move e2e5" in user and "attempt 2" in user
    assert "first move of the game" in user
    assert "1 | R N B Q K B N R |" in user  # board rendered from FEN when ascii_board is empty


def test_build_prompt_check():
    board = chess.Board("4k3/8/8/8/8/8/8/4R1K1 b - - 0 1")
    _, user = build_prompt(make_request(board))
    assert "in check" in user


def test_trim_comment():
    text = "a" * 3000 + "MIDDLE" + "b" * 3000 + "\nMOVE: e4"
    out = trim_comment(text, 4000)
    assert len(out) <= 4000 and out.endswith("MOVE: e4") and out.startswith("aaa")


def test_config_defaults():
    cfg = LLMConfig.from_dict({})
    assert cfg.provider == "anthropic" and cfg.model == "claude-opus-5-5"
    assert cfg.api_key_env == "ANTHROPIC_API_KEY" and cfg.max_tokens == 16000
    assert LLMConfig.from_dict({"provider": "openai", "model": "gpt"}).api_key_env == "OPENAI_API_KEY"
    with pytest.raises(ValueError):
        LLMConfig.from_dict({"provider": "gemini"})
    priced = LLMConfig.from_dict({"input_cost_per_mtok": 4, "output_cost_per_mtok": 20})
    assert priced.cost(1_000_000, 100_000) == pytest.approx(6.0)
    assert cfg.cost(10, 10) is None


# ---------------------------------------------------------------- anthropic
def message(text: str, stop_reason: str = "end_turn", **extra):
    content = [SimpleNamespace(type="thinking", thinking="", signature="x"),
               SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=200), **extra)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.msg


class FakeAnthropic:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, dict]] = []
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)
        self.closed = False

    def _next(self):
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    async def _create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return self._next()

    def _stream(self, **kwargs):
        self.calls.append(("stream", kwargs))
        return FakeStream(self._next())

    async def close(self):
        self.closed = True


async def test_anthropic_move_and_request_shape():
    client = FakeAnthropic([message("The center matters.\nMOVE: e4")])
    player = LLMPlayer(llm_spec(effort="high", input_cost_per_mtok=4, output_cost_per_mtok=20,
                                extra_body={"metadata": {"user_id": "bench"}}), anthropic_client=client)
    resp = await player.get_move(make_request())
    assert resp.move == "e4"
    assert "center" in resp.comment
    assert resp.usage["input_tokens"] == 1000 and resp.usage["output_tokens"] == 200
    assert resp.usage["cost_usd"] == pytest.approx((1000 * 4 + 200 * 20) / 1e6)
    kind, kwargs = client.calls[0]
    assert kind == "create"
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["max_tokens"] == 16000
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["messages"][0]["role"] == "user" and "FEN" in kwargs["messages"][0]["content"]
    assert kwargs["system"] == DEFAULT_SYSTEM_PROMPT
    assert kwargs["extra_body"] == {"metadata": {"user_id": "bench"}}
    assert "temperature" not in kwargs and "thinking" not in kwargs


async def test_anthropic_streams_for_large_max_tokens_and_fallback_parse():
    client = FakeAnthropic([message("I will push my king pawn: e2e4 looks best")])
    player = LLMPlayer(llm_spec(max_tokens=64000, temperature=0.5), anthropic_client=client)
    resp = await player.get_move(make_request())
    assert client.calls[0][0] == "stream"
    assert "output_config" not in client.calls[0][1] and "temperature" not in client.calls[0][1]
    assert resp.move == "e2e4"  # found by scanning, no MOVE: line


async def test_truncated_reply_is_not_scanned_for_a_move():
    """A reply cut off by max_tokens has no final answer; the fallback scan must not pick a
    move mentioned mid-reasoning (the runner re-asks instead)."""
    cut = message("Candidates: d4 is solid, but after e4 e5 Nf3 the line", stop_reason="max_tokens")
    player = LLMPlayer(llm_spec(), anthropic_client=FakeAnthropic([cut]))
    resp = await player.get_move(make_request())
    assert resp.move == "" and "truncated" in resp.comment
    # an explicit MOVE: line is still honoured even if the reply was cut afterwards
    cut = message("MOVE: e4\nand now some trailing tex", stop_reason="max_tokens")
    player = LLMPlayer(llm_spec(), anthropic_client=FakeAnthropic([cut]))
    assert (await player.get_move(make_request())).move == "e4"


async def test_anthropic_refusal():
    refused = message("", stop_reason="refusal",
                      stop_details=SimpleNamespace(type="refusal", category="cyber", explanation=None))
    player = LLMPlayer(llm_spec(), anthropic_client=FakeAnthropic([refused]))
    resp = await player.get_move(make_request())
    assert resp.move == "" and resp.comment.startswith("model refused") and "cyber" in resp.comment


async def test_anthropic_retries_transient_errors():
    request = sdk_httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    overloaded = anthropic.InternalServerError(
        "overloaded", response=sdk_httpx.Response(529, request=request), body=None)
    conn = anthropic.APIConnectionError(request=request)
    client = FakeAnthropic([overloaded, conn, message("MOVE: d4")])
    player = LLMPlayer(llm_spec(retry_base_delay_s=0), anthropic_client=client)
    resp = await player.get_move(make_request())
    assert resp.move == "d4" and resp.usage["retries"] == 2

    bad = anthropic.BadRequestError("bad", response=sdk_httpx.Response(400, request=request), body=None)
    player = LLMPlayer(llm_spec(retry_base_delay_s=0), anthropic_client=FakeAnthropic([bad]))
    with pytest.raises(InfrastructureError, match="HTTP 400"):
        await player.get_move(make_request())

    player = LLMPlayer(llm_spec(retry_base_delay_s=0), anthropic_client=FakeAnthropic([conn] * 4))
    with pytest.raises(InfrastructureError, match="after 4 attempts"):
        await player.get_move(make_request())


async def test_missing_api_key(monkeypatch):
    monkeypatch.delenv("AGENTCHESS_TEST_KEY", raising=False)
    player = LLMPlayer(llm_spec(api_key_env="AGENTCHESS_TEST_KEY"))
    with pytest.raises(InfrastructureError, match="AGENTCHESS_TEST_KEY"):
        await player.get_move(make_request())


async def test_anthropic_client_constructed_from_env(monkeypatch):
    created = {}

    def factory(**kwargs):
        created.update(kwargs)
        return FakeAnthropic([message("MOVE: Nf3")])

    monkeypatch.setenv("AGENTCHESS_TEST_KEY", "sk-test")
    monkeypatch.setattr(anthropic, "AsyncAnthropic", factory)
    player = LLMPlayer(llm_spec(api_key_env="AGENTCHESS_TEST_KEY"))
    assert (await player.get_move(make_request())).move == "Nf3"
    assert created["api_key"] == "sk-test" and created["max_retries"] == 0
    client = player._anthropic
    await player.close()
    assert client.closed


async def test_illegal_llm_answers_forfeit_in_game():
    client = FakeAnthropic([message("MOVE: Ke2"), message("MOVE: Qh5"), message("no idea")])
    llm = LLMPlayer(llm_spec(), anthropic_client=client)
    other = LLMPlayer(llm_spec(), anthropic_client=FakeAnthropic([]))
    game = GameRecord(id="g", white_id="llm", black_id="other", config=GameConfig(max_illegal_attempts=3))
    await play_game(game, llm, other)
    assert game.termination == Termination.ILLEGAL_MOVES and game.result == "0-1"
    retry_prompt = client.calls[1][1]["messages"][0]["content"]
    assert "illegal move Ke2" in retry_prompt and "attempt 2" in retry_prompt


# ------------------------------------------------------------------- openai
def openai_reply(content, prompt_tokens=50, completion_tokens=7, finish_reason="stop", **msg):
    return {"choices": [{"message": {"role": "assistant", "content": content, **msg},
                         "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


async def test_openai_compatible(monkeypatch):
    seen = []
    responses = [httpx.Response(503, text="busy"), httpx.Response(200, json=openai_reply("Solid.\nMOVE: c4"))]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    monkeypatch.setenv("AGENTCHESS_OAI_KEY", "sk-oai")
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    player = LLMPlayer(llm_spec(provider="openai", model="local-model", base_url="http://localhost:8000/v1/",
                                api_key_env="AGENTCHESS_OAI_KEY", temperature=0.2, max_tokens=500,
                                max_tokens_param="max_completion_tokens", retry_base_delay_s=0,
                                extra_body={"top_p": 0.9}, input_cost_per_mtok=1.0),
                       http_client=http)
    resp = await player.get_move(make_request())
    assert resp.move == "c4"
    assert resp.usage["input_tokens"] == 50 and resp.usage["output_tokens"] == 7
    assert resp.usage["cost_usd"] == pytest.approx(50 / 1e6)
    req = seen[-1]
    assert str(req.url) == "http://localhost:8000/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-oai"
    body = json.loads(req.content)
    assert body["model"] == "local-model" and body["temperature"] == 0.2 and body["top_p"] == 0.9
    assert body["max_completion_tokens"] == 500 and "max_tokens" not in body
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    await http.aclose()


async def test_openai_no_key_refusal_and_errors(monkeypatch):
    monkeypatch.delenv("AGENTCHESS_NO_KEY", raising=False)
    replies = [httpx.Response(200, json=openai_reply(None, refusal="I can't help with that")),
               httpx.Response(401, text="unauthorized")]
    seen = []

    def handler(request):
        seen.append(request)
        return replies.pop(0)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    player = LLMPlayer(llm_spec(provider="openai", model="m", api_key_env="AGENTCHESS_NO_KEY",
                                base_url="http://localhost:11434/v1"), http_client=http)
    resp = await player.get_move(make_request())
    assert resp.move == "" and "refused" in resp.comment
    assert "authorization" not in seen[0].headers
    assert json.loads(seen[0].content)["max_tokens"] == 16000 and "temperature" not in json.loads(seen[0].content)
    with pytest.raises(RuntimeError, match="401"):
        await player.get_move(make_request())
    await http.aclose()


async def test_openai_hosted_endpoint_requires_key(monkeypatch):
    monkeypatch.delenv("AGENTCHESS_KILO_TEST", raising=False)
    player = LLMPlayer(llm_spec(provider="openai", model="openai/gpt-5.5", api_key_env="AGENTCHESS_KILO_TEST",
                                base_url="https://api.kilo.ai/api/gateway"))
    with pytest.raises(InfrastructureError, match="AGENTCHESS_KILO_TEST is not set"):
        await player.get_move(make_request())
