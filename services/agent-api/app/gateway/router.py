"""Model router: ordered providers per tier, circuit breaker, TTFT timeout, cost accounting.

Streaming contract: a provider that fails mid-stream is never spliced with the next one.
The caller gets a `retract` event and the fallback regenerates the whole answer.
"""

import asyncio
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import google.auth
import google.auth.transport.requests
import httpx
import structlog
from langfuse.openai import AsyncOpenAI  # drop-in client that records generations in Langfuse
from openai import AsyncOpenAI as PlainOpenAI  # no Langfuse wrapper: never uploads request media
from openai import RateLimitError

from ..config import settings

log = structlog.get_logger()

# USD per 1M tokens (input, output). Documented assumption; update when prices change.
PRICES = {
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "local": (0.0, 0.0),  # self-hosted: marginal cost is electricity, estimated in evals/model_bench.py
    "openai/gpt-oss-120b": (0.15, 0.60),
}


class ProviderError(Exception):
    pass


def injected(provider: str) -> bool:
    """FAULT_INJECT="vertex_429" fails every call; "vertex_429@0.4" fails 40% of them."""
    for spec in filter(None, settings.fault_inject.split(",")):
        name, _, prob = spec.partition("@")
        if name == f"{provider}_429" and random.random() < float(prob or 1):
            return True
    return False


def headroom(kw: dict, p) -> dict:
    return {"max_tokens": kw["max_tokens"] + p.token_headroom} if "max_tokens" in kw else {}


@dataclass
class CallRecord:
    provider: str
    model: str
    tier: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    ttft_ms: float | None = None
    latency_ms: float = 0.0
    fallback_from: str | None = None
    error: str | None = None


@dataclass
class Breaker:
    failures: int = 0
    opened_at: float = 0.0

    def available(self) -> bool:
        if self.failures < settings.breaker_failures:
            return True
        # Half-open after the cool-down: let one call through.
        return time.monotonic() - self.opened_at > settings.breaker_open_s

    def record(self, ok: bool) -> None:
        if ok:
            self.failures = 0
        else:
            self.failures += 1
            if self.failures >= settings.breaker_failures:
                self.opened_at = time.monotonic()


@dataclass
class Provider:
    name: str
    models: dict[str, str]  # tier -> model id
    breaker: Breaker = field(default_factory=Breaker)
    extra: dict = field(default_factory=dict)  # provider-specific request params
    throttled_until: float = 0.0  # monotonic time until which a recent 429 makes us skip this provider
    token_headroom: int = 0  # reasoning models spend max_tokens on thinking; add room
    paid: bool = True  # False: costs this project nothing (Groq's free tier, a local model), so the budget does not apply

    async def client(self, trace: bool = True) -> AsyncOpenAI:
        raise NotImplementedError


class VertexProvider(Provider):
    _creds = None

    async def client(self, trace: bool = True) -> AsyncOpenAI:
        if self._creds is None:
            self._creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        if not self._creds.valid:
            await asyncio.to_thread(
                self._creds.refresh, google.auth.transport.requests.Request()
            )
        loc = settings.vertex_location
        host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
        base = (
            f"https://{host}/v1/projects/{settings.gcp_project}"
            f"/locations/{loc}/endpoints/openapi"
        )
        factory = AsyncOpenAI if trace else PlainOpenAI
        return factory(base_url=base, api_key=self._creds.token, max_retries=0)

    async def token(self) -> str:
        await self.client()
        return self._creds.token


class OpenAICompatProvider(Provider):
    def __init__(self, name: str, models: dict[str, str], base_url: str, api_key: str):
        super().__init__(name, models)
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=0)

    async def client(self, trace: bool = True) -> AsyncOpenAI:
        return self._client


class Router:
    def __init__(self, providers: list[Provider]):
        self.providers = providers
        self.spent_today_usd = 0.0  # this process only; the durable total is shared_* plus pending_usd
        self.shared_day = self.shared_month = 0.0  # last persisted totals (all instances)
        self.pending_usd = 0.0  # spent here since the last flush to the shared total
        self._day = time.strftime("%Y-%m-%d")
        self.calls: list[CallRecord] = []  # recent calls, for /v1/status
        self.paid_names = {p.name for p in providers if p.paid}

    def over_budget(self) -> bool:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self.spent_today_usd = today, 0.0
        day_total = max(self.spent_today_usd, self.shared_day + self.pending_usd)
        return (day_total >= settings.daily_budget_usd
                or self.shared_month + self.pending_usd >= settings.monthly_budget_usd)

    async def sync_spend(self, store) -> None:
        """Write what this instance spent since the last call to the shared total, then read the total back.

        The in-memory counter alone reset to zero on every restart or scale-to-zero, so a visitor could outlast it.
        """
        pending, self.pending_usd = self.pending_usd, 0.0
        if pending > 0 and not await store.add_spend(pending):
            self.pending_usd += pending  # keep it for the next attempt
        got = await store.get_spend()
        if got:
            self.shared_day, self.shared_month = got

    def _finish(self, rec: CallRecord, usage, t0: float) -> None:
        if usage:
            rec.tokens_in = usage.prompt_tokens or 0
            rec.tokens_out = (usage.total_tokens or 0) - rec.tokens_in
            pin, pout = PRICES.get(rec.model, PRICES.get(rec.provider, (0, 0)))
            rec.cost_usd = (rec.tokens_in * pin + rec.tokens_out * pout) / 1e6
            if rec.provider in self.paid_names:  # free providers are priced for display only, never counted as spend
                self.spent_today_usd += rec.cost_usd
                self.pending_usd += rec.cost_usd
        rec.latency_ms = (time.monotonic() - t0) * 1000
        self.calls = (self.calls + [rec])[-200:]
        log.info("llm_call", **rec.__dict__)

    async def stream(
        self, tier: str, messages: list[dict], records: list[CallRecord],
        only: tuple[str, ...] | None = None, trace: bool = True, **kw
    ) -> AsyncIterator[dict]:
        """Yield {"type": "delta"|"retract", ...}. Raise ProviderError if all providers fail.

        only: restrict to these provider names (e.g. audio-capable ones); trace=False uses an
        untraced client so request media (voice) is never uploaded to the tracing backend.
        """
        failed_from = None
        providers = [p for p in self.providers if only is None or p.name in only]
        if self.over_budget():  # the paid budget is used up: only providers that cost nothing may answer
            providers = [p for p in providers if not p.paid]
            if not providers:
                raise ProviderError("model budget used up")
        for i, p in enumerate(providers):
            if not p.breaker.available():
                failed_from = failed_from or p.name
                continue
            # Load shedding: after a 429, skip this provider for a short cooldown instead of
            # making every request wait out its own retry. The last provider is always tried.
            throttled = time.monotonic() < p.throttled_until
            if throttled and i < len(providers) - 1:
                failed_from = failed_from or p.name
                continue
            # A 429 before any output gets one short retry; anything else moves on.
            for attempt in range(2):
                rec = CallRecord(p.name, p.models[tier], tier, fallback_from=failed_from)
                t0 = time.monotonic()
                emitted = False
                usage = None
                try:
                    if injected(p.name):
                        # Fault-injection exercise: a real RateLimitError, so retry/cooldown paths run.
                        raise RateLimitError(
                            "injected 429 (fault-injection exercise)",
                            response=httpx.Response(429, request=httpx.Request("POST", "http://injected")),
                            body=None)
                    client = await (p.client() if trace else p.client(trace=False))
                    stream = await asyncio.wait_for(
                        client.chat.completions.create(
                            name=f"{p.name}:{tier}", model=rec.model, messages=messages,
                            stream=True,
                            stream_options={"include_usage": True},
                            timeout=settings.call_timeout_s, **{**kw, **headroom(kw, p)}, **p.extra,
                        ),
                        settings.ttft_timeout_s,
                    )
                    it = stream.__aiter__()
                    cut_off = False
                    while True:
                        # TTFT timeout until the first token; afterwards the call timeout.
                        limit = settings.call_timeout_s if emitted else settings.ttft_timeout_s
                        try:
                            chunk = await asyncio.wait_for(it.__anext__(), limit)
                        except StopAsyncIteration:
                            break
                        if chunk.usage:
                            usage = chunk.usage
                        if chunk.choices and getattr(chunk.choices[0], "finish_reason", None) == "length":
                            cut_off = True
                        text = chunk.choices[0].delta.content if chunk.choices else None
                        if text:
                            if not emitted:
                                rec.ttft_ms = (time.monotonic() - t0) * 1000
                                emitted = True
                            yield {"type": "delta", "text": text}
                    if not emitted:
                        raise ProviderError("empty completion")
                    p.breaker.record(True)
                    self._finish(rec, usage, t0)
                    records.append(rec)
                    if cut_off:  # hit max_tokens: tell the caller so it never shows half a sentence
                        log.warning("answer_truncated", provider=p.name, model=rec.model)
                        yield {"type": "truncated"}
                    return
                except Exception as e:  # noqa: BLE001 — any failure means "try the next option"
                    rec.error = f"{type(e).__name__}: {e}"[:300]
                    self._finish(rec, usage, t0)
                    records.append(rec)
                    if emitted:
                        yield {"type": "retract"}
                    if isinstance(e, RateLimitError):
                        p.throttled_until = time.monotonic() + settings.throttle_cooldown_s
                        if not emitted and attempt == 0 and not throttled:
                            await asyncio.sleep(settings.retry_backoff_s)
                            continue
                    p.breaker.record(False)
                    failed_from = p.name
                    break
        raise ProviderError("all providers failed")

    async def complete(
        self, tier: str, messages: list[dict], records: list[CallRecord],
        only: tuple[str, ...] | None = None, trace: bool = True, **kw
    ) -> str:
        out: list[str] = []
        async for ev in self.stream(tier, messages, records, only=only, trace=trace, **kw):
            if ev["type"] == "retract":
                out.clear()
            elif ev["type"] == "delta":  # a "truncated" event carries no text: keep what was produced so far
                out.append(ev["text"])
        return "".join(out)


def build_router() -> Router:
    providers: list[Provider] = [
        VertexProvider(
            "vertex", {"fast": settings.fast_model, "strong": settings.strong_model},
            # Thinking off: this is grounded Q&A, and it would add seconds to TTFT.
            extra={"reasoning_effort": "minimal"},
        )
    ]
    # Same vendor, other model: separate quota, so a throttled model does not end the turn.
    providers.append(VertexProvider(
        "vertex_alt", {"fast": settings.strong_model, "strong": settings.fast_model},
        extra={"reasoning_effort": "minimal"},
    ))
    if settings.groq_api_key:
        g = OpenAICompatProvider(
            "groq", {"fast": settings.groq_fast_model, "strong": settings.groq_strong_model},
            settings.groq_base_url, settings.groq_api_key,
        )
        g.extra = {"reasoning_effort": "low"}
        g.token_headroom = 400
        g.paid = False  # free tier: keeps Twin answering when the paid budget is used up
        providers.append(g)
    if settings.local_base_url and settings.local_model:
        loc = OpenAICompatProvider("local", {"fast": settings.local_model, "strong": settings.local_model},
                                   settings.local_base_url, "ollama")
        loc.token_headroom = settings.local_token_headroom
        loc.paid = False
        if "gpt-oss" in settings.local_model:
            loc.extra = {"reasoning_effort": "low"}
        providers.append(loc)
    if settings.providers:  # evals: run against exactly these providers, in this order
        by_name = {p.name: p for p in providers}
        providers = [by_name[n] for n in settings.providers.split(",") if n in by_name]
    return Router(providers)
