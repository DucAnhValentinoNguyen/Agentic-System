"""The reranker keeps what the model picks, never drops the retrieval's top 2, and fails open."""

import json

from app.graph.build import rerank_chunks

CHUNKS = [{"anchor": f"a{i}", "title": f"T{i}", "text": f"text {i}"} for i in range(1, 8)]


class Router:
    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error

    async def complete(self, tier, messages, records, **kw):
        if self.error:
            raise self.error
        return json.dumps({"order": self.reply})


async def test_keeps_the_models_picks_in_its_order_and_the_top_two():
    out = await rerank_chunks(Router([5, 3]), "q", CHUNKS, 5, [])
    assert [c["anchor"] for c in out] == ["a5", "a3", "a1", "a2"]


async def test_ignores_bad_numbers_and_duplicates():
    out = await rerank_chunks(Router([9, 0, 4, 4, -1]), "q", CHUNKS, 5, [])
    assert [c["anchor"] for c in out] == ["a4", "a1", "a2"]


async def test_an_empty_answer_falls_back_to_the_original_order():
    out = await rerank_chunks(Router([]), "q", CHUNKS, 5, [])
    assert [c["anchor"] for c in out] == ["a1", "a2"]


async def test_a_provider_failure_returns_the_original_top_k():
    from app.gateway.router import ProviderError
    out = await rerank_chunks(Router(error=ProviderError("down")), "q", CHUNKS, 3, [])
    assert [c["anchor"] for c in out] == ["a1", "a2", "a3"]


async def test_never_returns_more_than_keep():
    out = await rerank_chunks(Router([7, 6, 5, 4, 3]), "q", CHUNKS, 4, [])
    assert len(out) == 4
