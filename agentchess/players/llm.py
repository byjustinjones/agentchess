"""LLMPlayer: asks a language model for each move.

Providers:
- ``anthropic``: the official SDK (``anthropic.AsyncAnthropic``).
- ``openai``: any OpenAI-compatible ``/chat/completions`` endpoint over httpx
  (OpenAI, OpenRouter, vLLM, Ollama, llama.cpp ...).

Every request is stateless and contains the full position (see ``build_prompt``).
The model ends its reply with ``MOVE: <move>``; parsing/validation is left to the
game runner, which re-asks on illegal answers. There are deliberately no model
fallbacks: the benchmark must measure exactly the configured model.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, TypeVar

import anthropic
import chess
import httpx

from agentchess.models import MoveRequest, MoveResponse, PlayerSpec
from agentchess.moves import extract_move_text, find_move_in_text, render_ascii
from agentchess.players.base import InfrastructureError, Player

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
MAX_COMMENT_CHARS = 4000
STREAM_THRESHOLD_TOKENS = 16000
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}

DEFAULT_SYSTEM_PROMPT = """\
You are playing a rated game of chess against another AI agent or a chess engine. Play to win.

How this works:
- Each message gives you the complete current position: FEN, a board diagram, the moves played \
so far, and usually the list of legal moves. You have no memory of earlier messages, so rely only \
on the position you are given.
- Your answer must be one legal move for the side to move. An illegal or unreadable answer is \
rejected and you are asked again; too many rejected answers on one move forfeit the game.

How to answer:
1. Think briefly and concretely: note the opponent's last move and what it threatens, then check \
all checks, captures and threats for both sides, and make sure your king and pieces stay safe.
2. Before committing, verify your move is legal: the piece is really on its starting square, the \
path is clear, and your king is not left in check. If a legal move list is given, pick from it.
3. Finish your reply with a final line of exactly this form, and nothing after it:
MOVE: <move>
where <move> is in SAN (e.g. Nf3, exd5, O-O, e8=Q) or UCI (e.g. g1f3, e5d6, e1g1, e7e8q).\
"""


# --------------------------------------------------------------------- prompt
def build_prompt(req: MoveRequest, system_prompt: Optional[str] = None) -> tuple[str, str]:
    """Return ``(system, user)`` prompts for a move request. Pure; no I/O."""
    board = chess.Board(req.fen)
    side = "White" if req.color == "white" else "Black"
    other = "Black" if side == "White" else "White"
    diagram = req.ascii_board or render_ascii(board)

    lines = [
        f"You are playing {side} against {req.opponent_name or 'your opponent'}.",
        f"It is move {req.move_number}, {side} to move.",
        "",
        f"FEN: {req.fen}",
        "",
        "Board (White at the bottom; uppercase = White, lowercase = Black, '.' = empty):",
        diagram,
        "",
    ]
    if req.pgn.strip():
        lines.append(f"Moves so far (PGN): {req.pgn.strip()}")
    else:
        lines.append("Moves so far: none (this is the first move of the game).")
    if req.history_san:
        lines.append(f"{other}'s last move: {req.history_san[-1]}")
    if board.is_check():
        lines.append(f"You ({side}) are in check.")
    lines.append("")
    if req.legal_moves_san:
        lines.append(f"Legal moves ({len(req.legal_moves_san)}): {' '.join(req.legal_moves_san)}")
        lines.append("")
    if req.attempt > 1:
        lines.append(f"Attention: your previous answer was rejected ({req.previous_error or 'illegal move'}). "
                     f"This is attempt {req.attempt}; answer with a legal move.")
        lines.append("")
    if req.time_limit_s:
        lines.append(f"Time limit for this move: {req.time_limit_s:.0f} seconds.")
    lines.append("Reason briefly, then end with the line `MOVE: <your move>` (SAN or UCI).")
    return (system_prompt or DEFAULT_SYSTEM_PROMPT), "\n".join(lines)


def trim_comment(text: str, limit: int = MAX_COMMENT_CHARS) -> str:
    """Keep the head and the tail (where the decision is) of a long reply."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head - 5
    return text[:head] + "\n[…]\n" + text[-tail:]


# --------------------------------------------------------------------- config
@dataclass
class LLMConfig:
    provider: str = "anthropic"
    model: str = DEFAULT_ANTHROPIC_MODEL
    api_key_env: str = ""
    base_url: Optional[str] = None
    max_tokens: int = 16000
    max_tokens_param: str = "max_tokens"      # openai: set to "max_completion_tokens" for o-series/gpt-5 style APIs
    effort: Optional[str] = None
    temperature: Optional[float] = None
    system_prompt: Optional[str] = None
    extra_body: dict[str, Any] = field(default_factory=dict)
    input_cost_per_mtok: Optional[float] = None
    output_cost_per_mtok: Optional[float] = None
    request_timeout_s: float = 600.0
    max_retries: int = 3                      # transient errors (429 / 5xx / connection) per move
    retry_base_delay_s: float = 1.0
    fallback_parse: bool = True               # scan the reply for a legal move when no MOVE: line

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LLMConfig":
        d = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__ and v is not None}
        cfg = cls(**d)
        cfg.provider = cfg.provider.lower()
        if cfg.provider not in ("anthropic", "openai"):
            raise ValueError(f"unknown LLM provider {cfg.provider!r} (expected 'anthropic' or 'openai')")
        if not cfg.api_key_env:
            cfg.api_key_env = "ANTHROPIC_API_KEY" if cfg.provider == "anthropic" else "OPENAI_API_KEY"
        cfg.extra_body = dict(cfg.extra_body or {})
        return cfg

    def cost(self, input_tokens: int, output_tokens: int) -> Optional[float]:
        if self.input_cost_per_mtok is None and self.output_cost_per_mtok is None:
            return None
        return (input_tokens * (self.input_cost_per_mtok or 0.0)
                + output_tokens * (self.output_cost_per_mtok or 0.0)) / 1_000_000


@dataclass
class LLMReply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    refusal: Optional[str] = None     # set when the provider declined to answer
    stop_reason: Optional[str] = None


def _is_local_url(url: str) -> bool:
    host = (httpx.URL(url).host or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".local")


class TransientError(Exception):
    """A retryable provider failure (rate limit, overload, network)."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# --------------------------------------------------------------------- player
class LLMPlayer(Player):
    def __init__(self, spec: PlayerSpec, *, anthropic_client: Any = None,
                 http_client: Optional[httpx.AsyncClient] = None) -> None:
        super().__init__(spec)
        self.config = LLMConfig.from_dict(spec.config)
        self._anthropic = anthropic_client
        self._http = http_client
        self._owned: list[Any] = []   # clients created here (closed in close())

    # -------------------------------------------------------------- lifecycle
    async def get_move(self, request: MoveRequest) -> MoveResponse:
        system, user = build_prompt(request, self.config.system_prompt)
        call = self._call_anthropic if self.config.provider == "anthropic" else self._call_openai
        reply, retries = await self._with_retries(lambda: call(system, user))

        usage: dict[str, Any] = {"input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens}
        cost = self.config.cost(reply.input_tokens, reply.output_tokens)
        if cost is not None:
            usage["cost_usd"] = cost
        if retries:
            usage["retries"] = retries

        if reply.refusal is not None:
            comment = f"model refused: {reply.refusal}"
            if reply.text.strip():
                comment += "\n" + reply.text
            return MoveResponse(move="", comment=trim_comment(comment), usage=usage)

        move = extract_move_text(reply.text)
        truncated = reply.stop_reason in ("max_tokens", "length", "model_context_window_exceeded")
        # A reply cut off mid-reasoning has no final answer: scanning it would play whatever
        # move the model happened to mention last (a candidate, an opponent reply...).
        if move is None and self.config.fallback_parse and not truncated:
            found = find_move_in_text(chess.Board(request.fen), reply.text)
            move = found.uci() if found else None
        comment = reply.text
        if truncated:
            comment += f"\n[reply truncated: {reply.stop_reason}]"
        return MoveResponse(move=move or "", comment=trim_comment(comment), usage=usage)

    async def close(self) -> None:
        owned, self._owned = self._owned, []
        for client in owned:
            try:
                await (client.aclose() if isinstance(client, httpx.AsyncClient) else client.close())
            except Exception:  # noqa: BLE001
                pass
            if client is self._anthropic:
                self._anthropic = None
            if client is self._http:
                self._http = None

    # ---------------------------------------------------------------- retries
    async def _with_retries(self, fn: Callable[[], Awaitable[T]]) -> tuple[T, int]:
        attempt = 0
        while True:
            try:
                return await fn(), attempt
            except TransientError as e:
                if attempt >= self.config.max_retries:
                    cause = e.__cause__ or e
                    raise InfrastructureError(f"{self.config.provider} API error after {attempt + 1} attempts: {cause}") from cause
                retry_after = e.retry_after
                delay = self.config.retry_base_delay_s * (2 ** attempt) * (1 + random.random() * 0.25)
                if retry_after:
                    delay = max(delay, min(float(retry_after), 60.0))
                attempt += 1
                log.warning("%s: transient %s error (%s); retry %d/%d in %.1fs", self.id,
                            self.config.provider, e, attempt, self.config.max_retries, delay)
                await asyncio.sleep(delay)

    # -------------------------------------------------------------- anthropic
    def _anthropic_client(self) -> Any:
        if self._anthropic is None:
            key = os.environ.get(self.config.api_key_env)
            if not key:
                raise InfrastructureError(f"environment variable {self.config.api_key_env} is not set")
            self._anthropic = anthropic.AsyncAnthropic(api_key=key, max_retries=0,
                                                       timeout=self.config.request_timeout_s)
            self._owned.append(self._anthropic)
        return self._anthropic

    async def _call_anthropic(self, system: str, user: str) -> LLMReply:
        cfg = self.config
        client = self._anthropic_client()
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if cfg.effort:
            kwargs["output_config"] = {"effort": cfg.effort}
        if cfg.extra_body:
            kwargs["extra_body"] = cfg.extra_body
        try:
            if cfg.max_tokens > STREAM_THRESHOLD_TOKENS:
                async with client.messages.stream(**kwargs) as stream:
                    msg = await stream.get_final_message()
            else:
                msg = await client.messages.create(**kwargs)
        except anthropic.APIConnectionError as e:          # includes APITimeoutError
            raise TransientError(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code in TRANSIENT_STATUS or e.status_code >= 500:
                headers = e.response.headers if e.response is not None else None
                raise TransientError(f"HTTP {e.status_code}: {e.message}", _retry_after(headers)) from e
            # 400/401/403/404...: bad key, unknown model or rejected parameters — a setup problem.
            raise InfrastructureError(f"anthropic HTTP {e.status_code}: {e.message}") from e

        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text")
        usage = getattr(msg, "usage", None)
        reply = LLMReply(
            text=text,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            stop_reason=getattr(msg, "stop_reason", None),
        )
        if reply.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            explanation = getattr(details, "explanation", None) if details is not None else None
            reply.refusal = ", ".join(str(x) for x in (category, explanation) if x) or "refusal"
        return reply

    # ----------------------------------------------------------------- openai
    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(self.config.request_timeout_s, connect=30.0))
            self._owned.append(self._http)
        return self._http

    async def _call_openai(self, system: str, user: str) -> LLMReply:
        cfg = self.config
        body: dict[str, Any] = {
            "model": cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            cfg.max_tokens_param: cfg.max_tokens,
        }
        if cfg.temperature is not None:
            body["temperature"] = cfg.temperature
        body.update(cfg.extra_body)
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(cfg.api_key_env)
        url = (cfg.base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/") + "/chat/completions"
        if key:
            headers["Authorization"] = f"Bearer {key}"
        elif not _is_local_url(url):
            # Local servers (Ollama, vLLM) may run without auth; hosted endpoints never do.
            raise InfrastructureError(f"environment variable {cfg.api_key_env} is not set")
        try:
            resp = await self._http_client().post(url, json=body, headers=headers)
        except httpx.TransportError as e:
            raise TransientError(f"{type(e).__name__}: {e}") from e
        if resp.status_code in TRANSIENT_STATUS or resp.status_code >= 500:
            raise TransientError(f"HTTP {resp.status_code}: {resp.text[:300]}", _retry_after(resp.headers))
        if resp.status_code >= 400:
            raise InfrastructureError(f"HTTP {resp.status_code} from {url}: {resp.text[:500]}")
        try:
            data = resp.json()
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise InfrastructureError(f"unexpected response from {url}: {resp.text[:500]}") from e

        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):   # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage = data.get("usage") or {}
        reply = LLMReply(
            text=content or "",
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            stop_reason=choice.get("finish_reason"),
        )
        if message.get("refusal"):
            reply.refusal = str(message["refusal"])
        elif reply.stop_reason == "content_filter" and not reply.text.strip():
            reply.refusal = "content_filter"
        return reply


def _retry_after(headers: Any) -> Optional[float]:
    if not headers:
        return None
    value = headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
