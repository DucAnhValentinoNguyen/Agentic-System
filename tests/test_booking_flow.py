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
        self.found = None

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

    async def check_day(self, day):
        if day == "2026-10-06":
            return {"status": "ok", "label": "Tue 06 Oct", "slots": SLOTS, "reason": "", "why": "", "next": []}
        if day == "2026-10-09":                                  # tomorrow: inside the notice period
            return {"status": "ok", "label": "Fri 09 Oct", "slots": [], "reason": "notice",
                    "why": "Duc-Anh needs at least 24 hours' notice", "next": [SLOTS[1]]}
        return {"status": "ok", "label": "Sat 10 Oct", "slots": [], "reason": "weekend", "why": "he does not take calls at the weekend", "next": []}

    async def find_slots(self, day="", after="", before=""):
        self.found = (day, after, before)
        label = {"2026-10-06": "Tue 06 Oct", "2026-10-09": "Fri 09 Oct", "2026-10-10": "Sat 10 Oct"}.get(day, day)
        window = f" after {after}" if after else ""
        if after == "17:00" or day == "2026-10-10":
            why = "he does not take calls at the weekend" if day == "2026-10-10" else "he takes calls between 10:00 and 17:00 (Berlin time)"
            return {"status": "ok", "label": f"{label}{window}", "slots": [], "ranges": [], "reason": "x", "why": why, "next": []}
        if day == "2026-10-09":
            return {"status": "ok", "label": label, "slots": [], "ranges": [], "reason": "notice",
                    "why": "Duc-Anh needs at least 24 hours' notice", "next": [SLOTS[1]]}
        ranges = ["10:00\u201312:00", "14:00\u201317:00"] if not after else ["16:00\u201317:00"]
        return {"status": "ok", "label": f"{label}{window}".strip(), "slots": [SLOTS[0]], "ranges": ranges, "reason": "", "why": "", "next": []}

    async def check_time(self, start, slots=1):
        for x in SLOTS:
            if start == x["start"]:
                return {"status": "free", "label": x["label"], "max_slots": x["max_slots"]}
        if start == DIRECT_FREE:
            return {"status": "free", "label": "Thu 08 Oct, 15:00 (Berlin time)", "max_slots": 2}
        if start == DIRECT_BUSY:
            return {"status": "busy", "reason": "notice", "why": "Duc-Anh needs at least 24 hours' notice", "nearby": [SLOTS[0]]}
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


async def pick(turn, slot=SLOTS[0]):
    """The visitor types a day and time; the model reads it, the calendar says it is free."""
    return await turn("how about that time", {"requested_start": slot["start"]})


async def book_one(turn, length_chip=None, slot=SLOTS[0]):
    out = await turn("book a call")
    assert out["choices"] == ["Book it for me here"]
    out = await turn("Book it for me here", DETAILS)
    assert out["choices"] == [] and "Which day and time" in out["answer"]       # no time chips
    return await pick(turn, slot)


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
    out = await book_one(turn, slot=SLOTS[1])                     # Wednesday 12:30: the 13:00 block follows
    assert out["choices"] == ["30 min", "60 min"]


async def test_asking_for_the_length_up_front_skips_the_question(chat):
    turn, _ = chat
    await turn("book a call")
    await turn("Book it for me here", {**DETAILS, "minutes": 60})
    out = await pick(turn)
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
    assert out["choices"] == [] and "Which day and time" in out["answer"]  # straight to the question, no details asked
    out = await pick(turn)
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
    assert "isn't available" in out["answer"] and out["choices"] == []
    assert "free 10:00\u201312:00 and 14:00\u201317:00" in out["answer"]     # what is free that day, in words
    out = await pick(turn)                                    # and another time can be typed
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
    assert "ann@example.com" in out["answer"] and out["choices"] == []
    await pick(turn)
    await turn("30 min")
    await turn("Yes, book it")
    assert cal.created == [("Ann", "ann@example.com", "internship", START, 1)]


async def test_a_complaint_is_not_a_cancel(chat):
    turn, _ = chat
    await turn("book a call")
    await turn("Book it for me here", DETAILS)
    out = await turn("you dont remember my name, I just said it", {"cancel": False})
    assert "dropped the booking" not in out["answer"] and "Tell me a day and time" in out["answer"]


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
    out = await pick(turn)
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
    out = await pick(turn, SLOTS[1])
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


async def test_after_a_cancellation_asking_for_the_appointment_says_there_is_none(chat):
    turn, _ = chat
    await book_one(turn)
    await turn("90 min")
    await turn("Yes, book it")
    await turn("please cancel it", {"cancel": True})
    await turn("Yes, cancel it")
    out = await turn("when is my appointment?")
    assert "don't have a call booked" in out["answer"] and "booking page" not in out["answer"]


def test_the_appointment_question_pattern_does_not_swallow_booking_requests():
    from app.graph.build import ASK_EXISTING
    for q in ("when is my appointment?", "What time is my call?", "do I have a call booked?", "did I book a meeting"):
        assert ASK_EXISTING.search(q), q
    for q in ("when can I book a call?", "book a call", "How do I schedule a meeting with him?", "when is he free"):
        assert not ASK_EXISTING.search(q), q


async def test_a_confirmed_booking_comes_with_a_cancel_link_for_exactly_that_booking(chat, monkeypatch):
    from app import cancel as cx
    from app.config import settings
    monkeypatch.setattr(settings, "cancel_secret", "test-secret")
    monkeypatch.setattr(settings, "public_api_url", "https://api.example")
    turn, _ = chat
    await book_one(turn)
    await turn("30 min")
    out = await turn("Yes, book it")
    (link,) = out["links"]
    assert link["label"] == "Cancel this call" and link["url"].startswith("https://api.example/v1/cancel?t=")
    token = link["url"].split("t=")[1]
    assert cx.verify(token, "test-secret") == cx.event_id("ann@example.com", START)
    assert link["until"].startswith("2026-10-06T10:30:00")                      # the call ends 30 minutes after 10:00


async def test_no_cancel_link_when_no_secret_is_configured(chat, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "cancel_secret", "")
    turn, _ = chat
    await book_one(turn)
    await turn("30 min")
    out = await turn("Yes, book it")
    assert not out.get("links")


async def test_a_day_with_free_times_lists_them_even_before_the_visitor_gave_any_details(chat):
    turn, _ = chat
    await turn("book a call")
    out = await turn("what is free on Tuesday?", {"requested_date": "2026-10-06"})        # no name or email yet
    assert out["choices"] == []                                                          # words, no chips
    assert "free 10:00\u201312:00 and 14:00\u201317:00" in out["answer"] and "tell me your name" in out["answer"]


async def test_a_day_that_is_too_soon_says_why_instead_of_repeating_the_list(chat):
    turn, _ = chat
    await book_one_details(turn)
    out = await turn("I want to meet him tomorrow", {"requested_date": "2026-10-09"})
    assert "Nothing is free on Fri 09 Oct" in out["answer"] and "24 hours' notice" in out["answer"]
    assert out["choices"] == [] and "Try another day" in out["answer"]


async def test_a_weekend_with_nothing_after_it_sends_the_visitor_to_email(chat):
    turn, _ = chat
    await book_one_details(turn)
    out = await turn("what about Saturday?", {"requested_date": "2026-10-10"})
    assert "weekend" in out["answer"] and not out["choices"]


async def test_an_unavailable_time_explains_the_reason(chat):
    turn, _ = chat
    await book_one_details(turn)
    out = await turn("how about Friday at 8am", {"requested_start": DIRECT_BUSY})
    assert "isn't available: Duc-Anh needs at least 24 hours' notice" in out["answer"]


async def test_a_time_picked_before_the_details_is_kept_while_they_are_collected(chat):
    turn, cal = chat
    await turn("book a call")
    await turn("what is free on Tuesday?", {"requested_date": "2026-10-06"})
    out = await turn("10 please", {"requested_start": START})
    assert "on Tue 06 Oct, 10:00" in out["answer"] and "Could you tell me" in out["answer"]
    out = await turn("Ann, ann@example.com, internship", DETAILS)                         # now it carries on from the chosen time
    assert out["choices"] == ["30 min", "60 min", "90 min"]
    await turn("30 min")
    await turn("Yes, book it")
    assert cal.created == [("Ann", "ann@example.com", "internship", START, 1)]


async def book_one_details(turn):
    await turn("book a call")
    return await turn("Book it for me here", DETAILS)


async def test_tomorrow_is_understood_even_when_the_model_extracts_nothing(chat):
    turn, _ = chat
    await book_one_details(turn)
    out = await turn("so no more slot tomorrow?")                                          # the extractor returns nothing here
    assert "Nothing is free on" in out["answer"] and out["choices"] == []                  # whatever tomorrow is on the clock
    assert "Tell me a day and time" not in out["answer"]                                   # not the generic repeat


async def test_a_time_window_without_a_day_asks_which_day_instead_of_listing(chat):
    turn, cal = chat
    await book_one_details(turn)
    out = await turn("anything after 4pm")
    assert cal.found is None and "Which day" in out["answer"] and out["choices"] == []


async def test_a_weekday_with_a_time_window_answers_for_that_one_day(chat):
    turn, cal = chat
    await book_one_details(turn)
    out = await turn("how about tuesday after 4pm")
    assert cal.found[0] != "" and cal.found[1] == "16:00"
    assert "free 16:00\u201317:00" in out["answer"] and out["choices"] == []


async def test_after_5pm_says_he_is_not_available_then(chat):
    turn, _ = chat
    await book_one_details(turn)
    out = await turn("monday after 5pm")
    assert "10:00 and 17:00" in out["answer"] and "Try another day" in out["answer"]


async def test_a_time_alone_after_a_day_was_discussed_is_checked_on_that_day(chat):
    turn, _ = chat
    await book_one_details(turn)
    await turn("what is free on Tuesday?", {"requested_date": "2026-10-06"})
    out = await turn("10am?")                                                              # the model extracts nothing
    assert out["choices"] == ["30 min", "60 min", "90 min"]


async def test_a_question_about_the_wider_week_hands_over_the_calendar(chat, monkeypatch):
    monkeypatch.setattr(settings, "booking_page_url", PAGE_URL)
    turn, cal = chat
    await book_one_details(turn)
    out = await turn("how about other days of next week, anything after 3pm is good")
    assert "For a better overview of his week please check his calendar" in out["answer"]
    assert out["links"][0]["url"] == PAGE_URL and out["choices"] == [] and cal.found is None


async def test_an_email_typed_in_the_message_replaces_a_remembered_one(chat):
    turn, _ = chat
    await book_one_details(turn)
    out = await turn("Hung Dang hd@dojostack.ai", {"name": "Hung Dang"})                  # the model missed the email
    assert out["booking"]["email"] == "hd@dojostack.ai"


def test_time_window_readings():
    from app.graph.build import time_window
    assert time_window("anything after 4pm") == ("16:00", "")
    assert time_window("after 16:30") == ("16:30", "")
    assert time_window("before noon") == ("", "12:00")
    assert time_window("in the afternoon") == ("12:00", "")
    assert time_window("Tuesday at 4 is good") == ("", "")


async def test_a_refused_time_window_keeps_the_details_for_the_next_try(chat):
    turn, _ = chat
    await book_one_details(turn)
    await turn("monday after 5pm")
    out = await turn("how about tuesday after 4pm")
    assert "Which of these" not in out["answer"] and "booking page" not in out["answer"]
    assert out["booking"]["email"] == "ann@example.com"


async def test_the_models_stale_window_is_ignored_when_the_message_has_no_time(chat):
    turn, cal = chat
    await book_one_details(turn)
    cal.found = None
    await turn("how about the one on wednesday", {"after_time": "16:00"})
    assert cal.found is None


from app.config import settings

PAGE_URL = "https://calendar.app.google/uUFu1xoy2RZR3Rrh6"


async def test_the_booking_link_is_given_whenever_it_is_asked_for_even_mid_booking(chat, monkeypatch):
    monkeypatch.setattr(settings, "booking_page_url", PAGE_URL)
    turn, _ = chat
    await book_one_details(turn)                                    # now choosing from a list of times
    out = await turn("How do I schedule a meeting with him?")
    assert out["links"] == [{"label": "Open the booking page", "url": PAGE_URL}]
    out = await turn("Give me the booking link")
    assert out["links"][0]["url"] == PAGE_URL


async def test_after_five_failed_tries_twin_admits_it_and_offers_the_link_once(chat, monkeypatch):
    monkeypatch.setattr(settings, "booking_page_url", PAGE_URL)
    turn, _ = chat
    await book_one_details(turn)
    for i in range(4):
        out = await turn(f"hmm not sure {i}")
        assert "terrible at this" not in out["answer"]
    out = await turn("not that either")
    assert "Oh dear, I am terrible at this" in out["answer"]
    assert out["links"][0]["url"] == PAGE_URL
    out = await turn("still no")
    assert "terrible at this" not in out["answer"]                  # said once, not on every turn


async def test_a_normal_booking_never_triggers_the_apology(chat, monkeypatch):
    monkeypatch.setattr(settings, "booking_page_url", PAGE_URL)
    turn, _ = chat
    await book_one_details(turn)
    out = await turn(SLOTS[0]["label"], {"slot_choice": 1})
    await turn("30 min")
    out = await turn("Yes, book it")
    assert "terrible" not in out["answer"]


async def test_naming_another_time_instead_of_a_length_replaces_the_picked_time(chat):
    turn, _ = chat
    await book_one(turn)                                            # 10:00 picked, now asked how long
    out = await turn("actually tuesday after 4pm", {"requested_date": "2026-10-06"})
    assert "free 16:00–17:00" in out["answer"] and out["choices"] == []
    await pick(turn, SLOTS[1])
    out = await turn("30 min")
    assert "Wed 07 Oct, 12:30" in out["answer"]


async def test_correcting_the_name_or_email_at_the_confirm_step_updates_it_and_asks_again(chat):
    turn, cal = chat
    await book_one(turn)
    out = await turn("30 min")
    assert "Ann <ann@example.com>" in out["answer"]
    out = await turn("no under andy at andy@lmu.com", {"name": "Andy", "email": "andy@lmu.com"})
    assert "Andy <andy@lmu.com>" in out["answer"] and out["choices"] == ["Yes, book it", "No, cancel"]
    assert cal.created == []                                                    # nothing booked on the old details
    await turn("Yes, book it")
    assert cal.created == [("Andy", "andy@lmu.com", "internship", START, 1)]


async def test_a_plain_no_at_the_confirm_step_still_declines(chat):
    turn, cal = chat
    await book_one(turn)
    await turn("30 min")
    out = await turn("No, cancel")
    assert cal.created == [] and "haven't changed anything" in out["answer"]


async def test_anytime_on_one_day_is_not_a_whole_week_question(chat, monkeypatch):
    monkeypatch.setattr(settings, "booking_page_url", PAGE_URL)
    turn, cal = chat
    await book_one_details(turn)
    out = await turn("anytime on tuesday after 2pm is good")
    assert cal.found is not None and "free" in out["answer"] and "overview of his week" not in out["answer"]
    assert PAGE_URL not in out["answer"]                                  # the button carries the link, the text does not


async def test_no_reply_pastes_the_calendar_link_into_the_text(chat, monkeypatch):
    monkeypatch.setattr(settings, "booking_page_url", PAGE_URL)
    turn, _ = chat
    await turn("book a call")
    out = await turn("Book it for me here", DETAILS)
    assert PAGE_URL not in out["answer"] and out["links"][0]["url"] == PAGE_URL
    out = await turn("how about next week")
    assert PAGE_URL not in out["answer"] and out["links"][0]["url"] == PAGE_URL
