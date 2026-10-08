"""One fixed sentence gets one of two fixed replies, without any model call."""

import json
import uuid

import pytest
from app.config import settings
from app.graph import build as gb
from app.graph.build import EASTER, EASTER_TEXT, build_graph


class Router:
    def __init__(self):
        self.calls = 0

    async def complete(self, tier, messages, records, **kw):
        self.calls += 1
        return json.dumps({"intent": "smalltalk", "search_query": "", "complex": False, "followup": False})


class NoRetriever:
    async def search(self, q, k=5):
        return []


class Cal:
    enabled = False


async def ask(text):
    router = Router()
    graph = build_graph(router, NoRetriever(), Cal())
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    final = {}
    async for mode, ev in graph.astream({"question": text, "records": []}, cfg, stream_mode=["custom", "updates"]):
        if mode == "updates":
            for upd in ev.values():
                final.update({k: v for k, v in (upd or {}).items() if k != "records"})
    return final, router


def test_only_that_sentence_matches():
    for q in ("I underestimated you", "i underestimated you.", "  I UNDERESTIMATED YOU!! ", "I underestimated  you..."):
        assert EASTER.fullmatch(q.strip()), q
    for q in ("I underestimated you yesterday", "you underestimated me", "I underestimated your project", ""):
        assert not EASTER.fullmatch(q), q


async def test_the_text_reply(monkeypatch):
    monkeypatch.setattr(gb.random, "random", lambda: 0.1)
    final, router = await ask("I underestimated you")
    assert final["answer"] == EASTER_TEXT and not final.get("image") and router.calls == 0


async def test_the_picture_reply_carries_the_credit_and_a_text_fallback(monkeypatch):
    monkeypatch.setattr(gb.random, "random", lambda: 0.9)
    monkeypatch.setattr(settings, "easter_image_url", "https://site.example/assets/estimate-me.jpg")
    final, router = await ask("i underestimated you.")
    assert router.calls == 0
    assert final["image"]["url"] == "https://site.example/assets/estimate-me.jpg"
    assert final["image"]["fallback"] == "Yeah, well, maybe next time you will estimate me."                    # shown if the picture cannot be loaded
    assert final["links"][0]["url"].startswith("https://www.reddit.com/r/DunderMifflin/")


async def test_both_replies_happen(monkeypatch):
    seen = set()
    for r in (0.1, 0.9):
        monkeypatch.setattr(gb.random, "random", lambda r=r: r)
        final, _ = await ask("I underestimated you")
        seen.add("image" if final.get("image") else "text")
    assert seen == {"image", "text"}


async def test_a_similar_sentence_goes_through_the_normal_path():
    final, router = await ask("I underestimated your project")
    assert router.calls >= 1 and not final.get("image")


@pytest.mark.parametrize("q", ["I underestimated you"])
async def test_the_next_turn_is_unaffected(q):
    final, _ = await ask(q)
    assert "citations" in final or "answer" in final
