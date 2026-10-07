"""The classifier is told what the site covers, so a project question that never says 'Duc-Anh' is not off-topic."""

import json

from app.graph.build import build_graph, site_topics

TITLES = ["gnhf — merged fix to a 3.8k-star autonomous coding agent", "Contact", "SurgGround",
          "Teaching Assistant — 2 courses: Logic & Discrete Structures", "About"]


def test_site_topics_keeps_project_names_and_drops_generic_headings():
    assert site_topics(TITLES) == ["gnhf", "SurgGround", "Teaching Assistant"]


class Spy:
    def __init__(self):
        self.systems = []

    async def complete(self, tier, messages, records, **kw):
        self.systems.append(messages[0]["content"])
        return json.dumps({"intent": "off_topic", "search_query": "", "complex": False, "followup": False})


class Cal:
    enabled = False


async def test_the_classifier_prompt_lists_the_site_topics():
    import uuid
    spy = Spy()
    graph = build_graph(spy, None, Cal(), topics=site_topics(TITLES))
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    async for _ in graph.astream({"question": "How badly did the gnhf token cap undercount spend?", "records": []}, cfg):
        pass
    assert "gnhf; SurgGround; Teaching Assistant" in spy.systems[0] and "never off_topic" in spy.systems[0]
