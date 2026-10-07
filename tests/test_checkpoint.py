"""A chat survives an instance change: state is saved in Firestore, not in the memory of one process."""

import json
import sys
import uuid
from pathlib import Path

from app.checkpoint import FirestoreSaver
from app.graph.build import build_graph
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).parent))
from fake_firestore import FakeDB

START = "2026-10-06T10:00:00+02:00"
SLOTS = [{"start": START, "label": "Tue 06 Oct, 10:00 (Berlin time)", "max_slots": 3}]


class Router:
    def __init__(self):
        self.extract = {}

    async def complete(self, tier, messages, records, **kw):
        if "Extract booking details" in messages[0]["content"]:
            return json.dumps(self.extract)
        return json.dumps({"intent": "booking", "search_query": "", "complex": False, "followup": False})


class Cal:
    enabled = True

    def __init__(self):
        self.created = []

    async def free_slots(self):
        return SLOTS

    async def allowance(self, email):
        return {"status": "ok", "held": 0, "remaining": 3}

    async def create(self, name, email, topic, start, slots=1):
        self.created.append((name, email, topic, start, slots))
        return {"status": "created", "label": "label", "slots": slots, "remaining": 2}


class NoRetriever:
    async def search(self, q, k=5):
        return []


def instance(db, cal, router):
    """What each Cloud Run instance does at start-up: its own graph, all sharing one Firestore."""
    return build_graph(router, NoRetriever(), cal, checkpointer=FirestoreSaver(db))


async def say(graph, cfg, text):
    snap = await graph.aget_state(cfg)
    inp = Command(resume=text, update={"question": text}) if snap.next else {"question": text, "records": []}
    out = {}
    async for mode, ev in graph.astream(inp, cfg, stream_mode=["custom", "updates"]):
        if mode == "updates":
            for node, upd in ev.items():
                if node != "__interrupt__":
                    out.update({k: v for k, v in (upd or {}).items() if k != "records"})
    return out


async def test_a_booking_confirmed_on_another_instance_is_created():
    db, cal, router = FakeDB(), Cal(), Router()
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    a, b = instance(db, cal, router), instance(db, cal, router)       # two instances, no shared memory
    await say(a, cfg, "book a call")
    router.extract = {"name": "Ann", "email": "ann@example.com", "topic": "thesis"}
    await say(a, cfg, "Book it for me here")
    out = await say(b, cfg, SLOTS[0]["label"])                         # the next message lands on instance B
    assert out["choices"] == ["30 min", "60 min", "90 min"]
    router.extract = {}
    out = await say(a, cfg, "30 min")                                  # and back on A: the graph is paused at confirm
    assert out["choices"] == ["Yes, book it", "No, cancel"]
    out = await say(b, cfg, "Yes, book it")                            # the confirmation resumes on B
    assert cal.created == [("Ann", "ann@example.com", "thesis", START, 1)] and "Booked!" in out["answer"]


async def test_history_and_visitor_details_follow_the_chat_to_a_restarted_instance():
    db, cal, router = FakeDB(), Cal(), Router()
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    await say(instance(db, cal, router), cfg, "book a call")
    router.extract = {"name": "Ann", "email": "ann@example.com", "topic": "thesis"}
    await say(instance(db, cal, router), cfg, "Book it for me here")
    restarted = instance(db, cal, router)                              # a brand new process
    state = await restarted.aget_state(cfg)
    assert state.values["visitor"]["email"] == "ann@example.com"
    assert state.values["booking"]["stage"] == "choosing"


async def test_two_chats_do_not_see_each_others_state():
    db, cal, router = FakeDB(), Cal(), Router()
    g = instance(db, cal, router)
    one, two = ({"configurable": {"thread_id": uuid.uuid4().hex}} for _ in range(2))
    await say(g, one, "book a call")
    assert (await g.aget_state(two)).values == {}


async def test_deleting_a_thread_removes_all_its_documents():
    db, cal, router = FakeDB(), Cal(), Router()
    saver = FirestoreSaver(db)
    g = build_graph(router, NoRetriever(), cal, checkpointer=saver)
    tid = uuid.uuid4().hex
    await say(g, {"configurable": {"thread_id": tid}}, "book a call")
    assert any(p.startswith(f"sessions/{tid}/") for p in db.docs)
    await saver.adelete_thread(tid)
    assert not any(tid in p for p in db.docs)
