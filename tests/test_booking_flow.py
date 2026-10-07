"""The booking conversation end to end through the real graph: scripted model, fake calendar."""

import json
import uuid

import pytest
from app.graph.build import build_graph
from langgraph.types import Command

START = "2026-10-06T10:00:00+02:00"
DIRECT_FREE = "2026-10-08T15:00:00+02:00"
DIRECT_BUSY = "2026-10-08T09:00:00+02:00"
SLOTS = [{"start": START, "label": "Tue 06 Oct, 10:00 (Berlin time)", "max_slots": 3},
         {"start": "2026-10-07T12:30:00+02:00", "label": "Wed 07 Oct, 12:30 (Berlin time)", "max_slots": 2}]


class ScriptedRouter:
    """Answers the classifier like a real one and the extractor from a per-turn script."""

    def __init__(self):
        self.extract, self.leave = {}, {}

    async def complete(self, tier, messages, records, **kw):
        system = messages[0]["content"]
        if "Extract booking details" in system:
            return json.dumps(self.extract)
        if "what a visitor wants to send" in system:
            return json.dumps(self.leave)
        return json.dumps({"intent": "booking", "search_query": "", "complex": False, "followup": False})


class FakeCal:
    enabled = True

    def __init__(self):
        self.created, self.extended, self.cancelled, self.held, self.room = [], [], [], 0, 3

    async def free_slots(self):
        return SLOTS

    async def allowance(self, email):
        return {"status": "ok", "held": self.held, "remaining": 3 - self.held}

    async def create(self, name, email, topic, start, slots=1):
        self.created.append((name, email, topic, start, slots))
        self.held += slots
        return {"status": "created", "label": f"label x{slots}", "slots": slots, "remaining": 3 - self.held}

    async def extension_limit(self, email, start):
        return {"status": "ok", "current": self.held, "max_total": min(3, self.held + self.room)}

    async def extend(self, email, start, total):
        self.extended.append((email, start, total))
        self.held = total
        return {"status": "extended", "label": f"label x{total}", "slots": total, "remaining": 3 - self.held}

    async def send_message(self, email, message, name="", kind="", reference=""):
        self.sent = (email, message, kind, reference)
        return {"status": "sent"}

    async def cancel(self, email, start):
        self.cancelled.append((email, start))
        self.held = 0
        return {"status": "cancelled", "label": "label"}

    async def check_time(self, start, slots=1):
        if start == DIRECT_FREE:
            return {"status": "free", "label": "Thu 08 Oct, 15:00 (Berlin time)", "max_slots": 2}
        if start == DIRECT_BUSY:
            return {"status": "busy", "nearby": [SLOTS[0]]}
        return {"status": "busy", "nearby": []}


class NoRetriever:
    async def search(self, q, k=5):
        return []


@pytest.fixture
def chat():
    router, cal = ScriptedRouter(), FakeCal()
    graph = build_graph(router, NoRetriever(), cal)
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}

    async def turn(text, extract=None, leave=None):
        router.extract, router.leave = extract or {}, leave or {}
        snap = await graph.aget_state(cfg)
        inp = (Command(resume=text, update={"question": text}) if snap.next
               else {"question": text, "records": [], "session_id": "sess-42"})
        final = {}
        async for mode, ev in graph.astream(inp, cfg, stream_mode=["custom", "updates"]):
            if mode == "updates":
                for node, upd in ev.items():
                    if node != "__interrupt__":
                        final.update({k: v for k, v in (upd or {}).items() if k != "records"})
        return final

    return turn, cal


DETAILS = {"name": "Ann", "email": "ann@example.com", "topic": "internship"}


async def book_one(turn, length_chip=None, slot=SLOTS[0]["label"]):
    out = await turn("book a call")
    assert out["choices"] == ["Book it for me here"]
    out = await turn("Book it for me here", DETAILS)
    assert out["choices"] == [s["label"] for s in SLOTS]
    out = await turn(slot)
    return out


async def test_the_visitor_is_asked_how_long_and_gets_that_much(chat):
    turn, cal = chat
    out = await book_one(turn)
    assert out["choices"] == ["30 min", "60 min", "90 min"]               # all three fit at 10:00 on a Tuesday
    out = await turn("60 min")
    assert "60-minute" in out["answer"] and out["choices"] == ["Yes, book it", "No, cancel"]
    out = await turn("Yes, book it")
    assert cal.created == [("Ann", "ann@example.com", "internship", START, 2)]
    assert "Booked!" in out["answer"] and "30 more minutes" in out["answer"]
    assert out["choices"] == ["Extend to 90 min", "Book another time"]


async def test_the_length_offered_never_exceeds_what_fits_after_the_start_time(chat):
    turn, _ = chat
    out = await book_one(turn, slot=SLOTS[1]["label"])                     # Wednesday 12:30: the 13:00 block follows
    assert out["choices"] == ["30 min", "60 min"]


async def test_asking_for_the_length_up_front_skips_the_question(chat):
    turn, _ = chat
    await turn("book a call")
    await turn("Book it for me here", {**DETAILS, "minutes": 60})
    out = await turn(SLOTS[0]["label"])
    assert "60-minute" in out["answer"] and out["choices"] == ["Yes, book it", "No, cancel"]


async def test_extend_the_call_just_booked_without_asking_for_the_details_again(chat):
    # The conversation from the screenshot: "can I extend the meeting to 1 hour" after a 30-minute booking.
    turn, cal = chat
    await book_one(turn)
    await turn("30 min")
    out = await turn("Yes, book it")
    assert "Booked!" in out["answer"] and "Extend to 60 min" in out["choices"]
    out = await turn("can I extend the meeting to 1 hour", {"extend_to_minutes": 60})
    assert "extend your call" in out["answer"] and "60 minutes" in out["answer"]
    assert "Could you tell me" not in out["answer"]                         # it must not ask for the details again
    out = await turn("Yes, extend it")
    assert cal.extended == [("ann@example.com", START, 2)]
    assert "now 60 minutes" in out["answer"] and "Extend to 90 min" in out["choices"]


async def test_the_extend_chip_works_and_ninety_minutes_is_the_limit(chat):
    turn, cal = chat
    await book_one(turn)
    await turn("30 min")
    await turn("Yes, book it")
    await turn("Extend to 90 min")
    out = await turn("Yes, extend it")
    assert cal.extended == [("ann@example.com", START, 3)]
    assert out["choices"] == []                                            # nothing left to add
    out = await turn("extend it to 2 hours", {"extend_to_minutes": 120})
    assert "up to 90 minutes" in out["answer"]


async def test_extension_that_cannot_fit_says_why_and_offers_another_time(chat):
    turn, cal = chat
    await book_one(turn)
    await turn("30 min")
    await turn("Yes, book it")
    cal.room = 0                                                           # the next slot is busy
    out = await turn("extend to 1 hour", {"extend_to_minutes": 60})
    assert "can't extend" in out["answer"] and out["choices"] == ["Book another time"]


async def test_booking_another_time_reuses_the_details(chat):
    turn, _ = chat
    await book_one(turn)
    await turn("30 min")
    await turn("Yes, book it")
    out = await turn("Book another time")
    assert out["choices"] == [s["label"] for s in SLOTS]                   # straight to the times, no questions
    out = await turn(SLOTS[0]["label"])
    assert out["choices"] == ["30 min", "60 min"]                          # 1 slot used, so at most 2 more


async def test_nothing_is_changed_without_a_clear_yes(chat):
    turn, cal = chat
    await book_one(turn)
    await turn("30 min")
    out = await turn("hmm maybe later")
    assert cal.created == [] and "haven't changed anything" in out["answer"]


async def test_a_visitor_typed_time_that_is_free_is_booked_directly(chat):
    # "Thursday 3pm" never appears in the offered SLOTS list, but the backend checks it for real.
    turn, cal = chat
    await turn("book a call")
    out = await turn("Book it for me here", {**DETAILS, "requested_start": DIRECT_FREE})
    assert out["choices"] == ["30 min", "60 min"]                           # check_time's own max_slots, not a chip
    out = await turn("30 min")
    assert out["choices"] == ["Yes, book it", "No, cancel"]
    out = await turn("Yes, book it")
    assert cal.created == [("Ann", "ann@example.com", "internship", DIRECT_FREE, 1)]


async def test_a_visitor_typed_time_that_is_taken_offers_nearby_times(chat):
    turn, _ = chat
    await turn("book a call")
    out = await turn("Book it for me here", {**DETAILS, "requested_start": DIRECT_BUSY})
    assert "isn't available" in out["answer"] and out["choices"] == [SLOTS[0]["label"]]
    out = await turn(SLOTS[0]["label"])                                    # still bookable from the fallback list
    assert out["choices"] == ["30 min", "60 min", "90 min"]


async def test_after_declining_the_visitor_is_not_asked_who_they_are_again(chat):
    # The conversation from the screenshot: details given, call proposed, "No, cancel", then "book a call" again.
    turn, cal = chat
    await book_one(turn)
    await turn("30 min")
    out = await turn("No, cancel")
    assert cal.created == [] and "haven't changed anything" in out["answer"]
    out = await turn("book a call")
    assert out["choices"] == ["Book it for me here"]
    out = await turn("Book it for me here")                                # no details in this message
    assert "Could you tell me" not in out["answer"]
    assert "ann@example.com" in out["answer"] and out["choices"] == [s["label"] for s in SLOTS]
    await turn(SLOTS[0]["label"])
    await turn("30 min")
    await turn("Yes, book it")
    assert cal.created == [("Ann", "ann@example.com", "internship", START, 1)]


async def test_a_complaint_is_not_a_cancel(chat):
    turn, _ = chat
    await turn("book a call")
    await turn("Book it for me here", DETAILS)
    out = await turn("you dont remember my name, I just said it", {"cancel": False})
    assert "dropped the booking" not in out["answer"] and out["choices"] == [s["label"] for s in SLOTS]


async def test_cancelling_a_booked_call_really_cancels_it_after_a_confirmation(chat):
    # The screenshot: booked 90 minutes, "please cancel it" was answered "dropped" while the event stayed on the calendar.
    turn, cal = chat
    await book_one(turn)
    await turn("90 min")
    out = await turn("Yes, book it")
    assert "Booked!" in out["answer"] and cal.held == 3
    out = await turn("oh shoot I have schedule conflict, please cancel it", {"cancel": True})
    assert "To confirm: cancel your call" in out["answer"] and out["choices"] == ["Yes, cancel it", "No, keep it"]
    assert cal.cancelled == []                                              # nothing happens before the confirmation
    out = await turn("Yes, cancel it")
    assert cal.cancelled == [("ann@example.com", START)] and "is cancelled" in out["answer"]
    await turn("book a call")                                               # and they can book again, details remembered
    out = await turn("Book it for me here")
    assert "ann@example.com" in out["answer"]
    out = await turn(SLOTS[0]["label"])
    assert out["choices"] == ["30 min", "60 min", "90 min"]


async def test_declining_the_cancellation_keeps_the_call(chat):
    turn, cal = chat
    await book_one(turn)
    await turn("30 min")
    await turn("Yes, book it")
    await turn("cancel my call", {"cancel": True})
    out = await turn("No, keep it")
    assert cal.cancelled == [] and "stays as it is" in out["answer"]


async def test_stopping_an_unfinished_booking_never_claims_a_cancellation(chat):
    turn, cal = chat
    await turn("book a call")
    await turn("Book it for me here", DETAILS)
    out = await turn("never mind, cancel", {"cancel": True})
    assert "nothing new was booked" in out["answer"] and cal.cancelled == []


async def test_asking_when_the_call_is_gets_an_answer_not_a_new_booking(chat):
    turn, _ = chat
    await book_one(turn)
    await turn("30 min")
    await turn("Yes, book it")
    out = await turn("when is my appointment?", {"ask_existing": True})
    assert "Your call with Duc-Anh is on" in out["answer"] and "Cancel that call" in out["choices"]


async def test_at_the_limit_the_visitor_is_told_which_call_and_can_cancel_it(chat):
    turn, cal = chat
    await book_one(turn)
    await turn("90 min")
    await turn("Yes, book it")
    await turn("book another call")
    out = await turn(SLOTS[1]["label"])
    assert "already hold 3" in out["answer"] and out["choices"] == ["Cancel that call"]
    out = await turn("Cancel that call")
    assert "To confirm: cancel your call" in out["answer"]
    await turn("Yes, cancel it")
    assert cal.cancelled == [("ann@example.com", START)]


REPORT = "Report an issue with this chatbot to Duc-Anh"


async def test_the_report_button_sends_an_issue_report_with_the_chat_reference(chat):
    turn, cal = chat
    out = await turn(REPORT)
    assert "what went wrong" in out["answer"] and "email address" in out["answer"]
    out = await turn("v@example.com, it said my call was cancelled but it was not",
                     leave={"email": "v@example.com", "message": "it said my call was cancelled but it was not"})
    assert "issue report" in out["answer"] and out["choices"] == ["Yes, send it", "No, cancel"]
    out = await turn("Yes, send it")
    assert cal.sent == ("v@example.com", "it said my call was cancelled but it was not", "issue", "sess-42")
    assert "report is on its way" in out["answer"]


async def test_the_report_reuses_an_email_already_given_in_the_chat(chat):
    turn, cal = chat
    await turn("book a call")
    await turn("Book it for me here", DETAILS)
    out = await turn(REPORT)
    assert "what went wrong" in out["answer"] and "email address" not in out["answer"]
    await turn("the times shown were wrong", leave={"message": "the times shown were wrong"})
    await turn("Yes, send it")
    assert cal.sent[0] == "ann@example.com" and cal.sent[2] == "issue"
