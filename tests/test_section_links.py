"""When a visitor asks for more than a section summarises, Twin says so and hands over that section's links (Option 1)."""

import json
import uuid

from app.graph.build import SUMMARY_ONLY, build_graph

CHUNK = {"anchor": "cloud-innovation-challenge", "title": "Cloud Innovation Challenge", "url": "https://site.example/#cloud",
         "part": 0, "text": "Scored case-study report. Case-study report (PDF) Interactive demo (synthetic data)"}
LINKS = {"cloud-innovation-challenge": [{"label": "Case-study report (PDF)", "url": "https://site.example/report.pdf"},
                                        {"label": "Interactive demo (synthetic data)", "url": "https://site.example/demo/"}],
         "other": [{"label": "Elsewhere", "url": "https://site.example/other"}]}


class Router:
    def __init__(self, answer):
        self.answer, self.system = answer, ""

    async def complete(self, tier, messages, records, **kw):
        return json.dumps({"intent": "question", "search_query": "report", "complex": False, "followup": False})

    async def stream(self, tier, messages, records, **kw):
        self.system = messages[0]["content"]
        yield {"type": "delta", "text": self.answer}


class Retriever:
    async def search(self, q, k=5):
        return [CHUNK]


class Cal:
    enabled = False


async def ask(answer):
    router = Router(answer)
    graph = build_graph(router, Retriever(), Cal(), section_links=LINKS)
    final = {}
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    async for ev in graph.astream({"question": "what does the report say?", "records": [], "variant": "A"}, cfg, stream_mode="updates"):
        for upd in ev.values():
            final.update(upd if isinstance(upd, dict) else {})
    return final, router


async def test_the_prompt_lists_the_link_labels_but_not_the_urls():
    _, router = await ask("anything [1]")
    assert "Linked from this section (not read): Case-study report (PDF); Interactive demo (synthetic data)" in router.system
    assert "report.pdf" not in router.system and SUMMARY_ONLY in router.system


async def test_the_marker_answer_carries_the_sections_links_as_buttons():
    final, _ = await ask(f"{SUMMARY_ONLY} It is a scored architecture proposal [1]. The report itself holds the details.")
    assert [x["url"] for x in final["links"]] == ["https://site.example/report.pdf", "https://site.example/demo/"]
    assert [c["anchor"] for c in final["citations"]] == ["cloud-innovation-challenge"]


async def test_an_ordinary_answer_gets_no_buttons():
    final, _ = await ask("He designed an AWS architecture for a fictional Formula 1 team [1].")
    assert not final.get("links")


async def test_the_corpus_builder_keeps_each_sections_outgoing_links():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "ingest"))
    from build_corpus import build
    html = ('<section id="a"><h2>A</h2><p>Text <a href="/x.pdf">Report (PDF) →</a> <a href="#top">top</a> '
            '<a href="mailto:a@b.c">mail</a> <a href="https://e.org/y">Demo</a></p></section>')
    [chunk] = build(html, "https://site.example/")
    assert chunk["links"] == [{"label": "Report (PDF)", "url": "https://site.example/x.pdf"}, {"label": "Demo", "url": "https://e.org/y"}]


async def test_links_to_pages_that_were_ingested_are_not_listed_as_unread():
    router = Router("anything [1]")
    links = {"cloud-innovation-challenge": [{"label": "Case-study report (PDF)", "url": "u1", "read": True},
                                            {"label": "Hackathon page", "url": "u2"}]}
    graph = build_graph(router, Retriever(), Cal(), section_links=links)
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    async for _ in graph.astream({"question": "q", "records": [], "variant": "A"}, cfg, stream_mode="updates"):
        pass
    note = next(line for line in router.system.splitlines() if line.startswith("Linked from this section"))
    assert note == "Linked from this section (not read): Hackathon page"


async def test_retrieval_merges_the_rewritten_query_with_the_visitors_own_words():
    from app.config import settings
    seen = []

    class TwoWay:
        async def search(self, q, k=5):
            seen.append(q)
            return [{"anchor": "from-rewrite" if q == "rewritten query" else "from-question", "title": "t", "url": "u", "part": 0, "text": "x"}]

    class R(Router):
        async def complete(self, tier, messages, records, **kw):
            return json.dumps({"intent": "question", "search_query": "rewritten query", "complex": False, "followup": False})

    router = R("ok [1] [2]")
    graph = build_graph(router, TwoWay(), Cal())
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    final = {}
    async for ev in graph.astream({"question": "what about the pilot?", "records": [], "variant": "A"}, cfg, stream_mode="updates"):
        for upd in ev.values():
            final.update(upd if isinstance(upd, dict) else {})
    assert sorted(seen) == ["rewritten query", "what about the pilot?"]
    assert sorted(c["anchor"] for c in final["chunks"]) == ["from-question", "from-rewrite"]
    settings.retrieve_raw_too = False
    try:
        seen.clear()
        async for _ in graph.astream({"question": "again?", "records": [], "variant": "A"}, {"configurable": {"thread_id": uuid.uuid4().hex}}, stream_mode="updates"):
            pass
        assert seen == ["rewritten query"]
    finally:
        settings.retrieve_raw_too = True
