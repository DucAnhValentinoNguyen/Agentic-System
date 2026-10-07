"""Turns that fall out of the history window are folded into a summary that later steps can see."""

import json
import uuid

from app.config import settings
from app.graph.build import build_graph


class Router:
    def __init__(self):
        self.summaries, self.classifier_systems = 0, []

    async def complete(self, tier, messages, records, **kw):
        system = messages[0]["content"]
        if system.startswith("You keep notes"):
            self.summaries += 1
            return "Visitor Ann (ann@example.com) asked about the projects; wants a call about a thesis."
        if "You route messages" in system:
            self.classifier_systems.append(system)
            return json.dumps({"intent": "smalltalk", "search_query": "", "complex": False, "followup": False})
        raise AssertionError(system[:60])


class NoRetriever:
    async def search(self, q, k=5):
        return []


class Cal:
    enabled = False


async def chat_n(router, n):
    graph = build_graph(router, NoRetriever(), Cal())
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    for i in range(n):
        async for _ in graph.astream({"question": f"hello number {i}", "records": []}, cfg, stream_mode="updates"):
            pass
    return await graph.aget_state(cfg)


async def test_no_summary_while_everything_still_fits_in_the_window():
    r = Router()
    state = await chat_n(r, 4)                       # 8 messages: only 2 are older than the 6-message window
    assert r.summaries == 0 and not state.values.get("summary")


async def test_old_turns_become_a_summary_and_the_next_step_sees_it():
    r = Router()
    state = await chat_n(r, 8)
    assert r.summaries >= 1
    v = state.values
    assert "Visitor Ann" in v["summary"]
    assert v["summary_upto"] == len(v["history"]) - settings.history_window or v["summary_upto"] > 0
    assert any("Earlier in this conversation" in s and "Visitor Ann" in s for s in r.classifier_systems)


async def test_a_failing_summariser_does_not_break_the_chat():
    from app.gateway.router import ProviderError

    class Failing(Router):
        async def complete(self, tier, messages, records, **kw):
            if messages[0]["content"].startswith("You keep notes"):
                raise ProviderError("down")
            return await super().complete(tier, messages, records, **kw)
    state = await chat_n(Failing(), 8)
    assert not state.values.get("summary") and len(state.values["history"]) == 16
