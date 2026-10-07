"""Router behaviour with fake providers: fallback, mid-stream retraction, breaker."""

import time
from types import SimpleNamespace

import pytest
from app.config import settings
from app.gateway.router import Provider, ProviderError, Router


class FakeStream:
    def __init__(self, texts, fail_after=None):
        self.texts, self.fail_after, self.i = texts, fail_after, 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.fail_after is not None and self.i == self.fail_after:
            raise RuntimeError("connection dropped")
        if self.i >= len(self.texts):
            raise StopAsyncIteration
        t = self.texts[self.i]
        self.i += 1
        last = self.i == len(self.texts)
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(
            delta=SimpleNamespace(content=t),
            finish_reason="length" if last and getattr(self, "truncate", False) else None)])


class FakeProvider(Provider):
    def __init__(self, name, texts=None, fail_after=None, error=None):
        super().__init__(name, {"fast": name, "strong": name})
        self.texts, self.fail_after, self.error, self.n = texts or [], fail_after, error, 0

    async def client(self):
        async def create(**kw):
            self.n += 1
            if self.error:
                raise self.error
            return FakeStream(self.texts, self.fail_after)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def collect(router, recs):
    return [ev async for ev in router.stream("strong", [], recs)]


async def test_falls_back_when_primary_errors():
    recs = []
    r = Router([FakeProvider("a", error=RuntimeError("429")), FakeProvider("b", ["hi"])])
    assert await collect(r, recs) == [{"type": "delta", "text": "hi"}]
    assert recs[-1].provider == "b" and recs[-1].fallback_from == "a"


async def test_midstream_failure_retracts_and_regenerates():
    recs = []
    r = Router([FakeProvider("a", ["par", "tial"], fail_after=1), FakeProvider("b", ["whole"])])
    evs = await collect(r, recs)
    assert [e["type"] for e in evs] == ["delta", "retract", "delta"]
    assert await r.complete("strong", [], []) == "whole"  # never spliced


async def test_all_fail_raises():
    r = Router([FakeProvider("a", error=RuntimeError("x"))])
    with pytest.raises(ProviderError):
        await collect(r, [])


async def test_breaker_opens_and_skips_provider():
    a = FakeProvider("a", error=RuntimeError("x"))
    r = Router([a, FakeProvider("b", ["ok"])])
    for _ in range(settings.breaker_failures + 2):
        await collect(r, [])
    assert a.n == settings.breaker_failures  # skipped once open


async def test_429_before_output_is_retried_once(monkeypatch):
    import httpx
    from openai import RateLimitError

    monkeypatch.setattr(settings, "retry_backoff_s", 0)
    resp = httpx.Response(429, request=httpx.Request("POST", "http://x"))

    class Flaky(FakeProvider):
        async def client(self):
            self.error = RateLimitError("429", response=resp, body=None) if self.n == 0 else None
            return await super().client()

    a, recs = Flaky("a", ["ok"]), []
    r = Router([a])
    assert await collect(r, recs) == [{"type": "delta", "text": "ok"}]
    assert a.n == 2 and a.breaker.failures == 0 and recs[0].error


async def test_429_triggers_cooldown_and_next_requests_skip_the_provider(monkeypatch):
    import httpx
    from openai import RateLimitError

    monkeypatch.setattr(settings, "retry_backoff_s", 0)
    resp = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    a = FakeProvider("a", error=RateLimitError("429", response=resp, body=None))
    b = FakeProvider("b", ["ok"])
    r = Router([a, b])
    await collect(r, [])  # a: 429, one retry, 429 again -> falls to b
    calls_after_first = a.n
    await collect(r, [])  # within the cooldown: a is skipped entirely, no wasted retries
    assert a.n == calls_after_first and b.n == 2


async def test_last_provider_is_tried_even_when_throttled():
    a = FakeProvider("a", ["ok"])
    r = Router([a])
    a.throttled_until = 1e18
    assert await collect(r, []) == [{"type": "delta", "text": "ok"}]


async def test_length_finish_is_reported_as_truncated():
    a = FakeProvider("a", ["Hello there. Partial sen"])
    orig = a.client

    async def client():
        c = await orig()
        create = c.chat.completions.create

        async def wrapped(**kw):
            st = await create(**kw)
            st.truncate = True
            return st
        c.chat.completions.create = wrapped
        return c
    a.client = client
    evs = await collect(Router([a]), [])
    assert evs[-1] == {"type": "truncated"}


def test_trim_to_sentence_keeps_whole_sentences_and_citations():
    from app.graph.build import trim_to_sentence
    assert trim_to_sentence("He builds models [4]. He works on multimodal VLMs for surgical video in") \
        == "He builds models [4]."
    assert trim_to_sentence("No sentence end here") == "No sentence end here"
    assert trim_to_sentence("One. Two [1, 2]. Thre") == "One. Two [1, 2]."


async def test_complete_returns_the_text_so_far_when_the_reply_was_truncated():
    # Regression: complete() raised KeyError('text') on the "truncated" event, crashing any structured call that hit its cap.
    a = FakeProvider("a", ["Hello there. Partial sen"])
    orig = a.client

    async def client():
        c = await orig()
        create = c.chat.completions.create

        async def wrapped(**kw):
            st = await create(**kw)
            st.truncate = True
            return st
        c.chat.completions.create = wrapped
        return c
    a.client = client
    assert await Router([a]).complete("strong", [], []) == "Hello there. Partial sen"


async def test_when_the_paid_budget_is_used_up_only_the_free_provider_answers(monkeypatch):
    paid, free = FakeProvider("vertex", ["from vertex"]), FakeProvider("groq", ["from groq"])
    free.paid = False
    r = Router([paid, free])
    monkeypatch.setattr(settings, "daily_budget_usd", 1.0)
    r.spent_today_usd = 2.0                                          # the paid budget is gone
    assert r.over_budget()
    assert await r.complete("strong", [], []) == "from groq"
    assert paid.n == 0                                               # the paid provider was never called


async def test_with_budget_left_the_paid_provider_comes_first(monkeypatch):
    paid, free = FakeProvider("vertex", ["from vertex"]), FakeProvider("groq", ["from groq"])
    free.paid = False
    monkeypatch.setattr(settings, "daily_budget_usd", 100.0)
    assert await Router([paid, free]).complete("strong", [], []) == "from vertex"


async def test_with_no_free_provider_an_exhausted_budget_refuses(monkeypatch):
    monkeypatch.setattr(settings, "daily_budget_usd", 1.0)
    r = Router([FakeProvider("vertex", ["x"])])
    r.spent_today_usd = 5.0
    with pytest.raises(ProviderError):
        await r.complete("strong", [], [])


def test_only_paid_calls_count_as_spend():
    from app.gateway.router import CallRecord
    paid, free = FakeProvider("vertex", ["x"]), FakeProvider("groq", ["x"])
    free.paid = False
    r = Router([paid, free])
    usage = SimpleNamespace(prompt_tokens=1_000_000, total_tokens=1_100_000)
    for name, model in (("vertex", "google/gemini-2.5-flash"), ("groq", "openai/gpt-oss-120b")):
        rec = CallRecord(provider=name, model=model, tier="strong")
        r._finish(rec, usage, time.monotonic())
    assert r.pending_usd == pytest.approx(0.30 + 0.1 * 2.5)          # only the Vertex call: input 1M tokens + output 0.1M
