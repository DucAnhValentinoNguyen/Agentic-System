"""Booking rules (pure) and the calendar tools (against a fake Google Calendar)."""

import datetime as dt
import json

import pytest
from app import calendar_mcp as m
from app import slots as sl
from googleapiclient.errors import HttpError

TZ = sl.TZ


def day(y, mo, d, h=0, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=TZ)


# 2026-10-05 is a Monday; 07 Wednesday; 09 Friday.
NOW = day(2026, 10, 3, 9)  # Saturday morning, so the first bookable day is Monday 5th (24 h notice from Sunday)


def starts(free):
    return [t.strftime("%a %H:%M") for t in free]


# ---------------------------------------------------------------- pure rules

def test_window_weekdays_10_to_17_and_nothing_before_notice():
    free = sl.free_slots([], NOW)
    assert free[0] == day(2026, 10, 5, 10)                      # Monday 10:00, first open slot
    assert all(10 <= t.hour < 17 and t.weekday() < 5 for t in free)
    assert day(2026, 10, 5, 16, 30) in free and day(2026, 10, 5, 17) not in free


def test_wednesday_and_friday_13_to_14_are_blocked_but_other_days_are_not():
    free = set(sl.free_slots([], NOW))
    for d in (7, 9):  # Wednesday, Friday
        assert day(2026, 10, d, 13) not in free and day(2026, 10, d, 13, 30) not in free
        assert day(2026, 10, d, 12, 30) in free and day(2026, 10, d, 14) in free
    assert day(2026, 10, 6, 13) in free and day(2026, 10, 8, 13) in free  # Tuesday, Thursday


def test_a_run_may_not_cross_the_block_or_the_end_of_the_day():
    free = set(sl.free_slots([], NOW))
    assert sl.run_ok(free, day(2026, 10, 7, 11), 3)                  # 11:00-12:30, fine
    assert not sl.run_ok(free, day(2026, 10, 7, 12, 30), 2)           # would run into the 13:00 block
    assert sl.run_ok(free, day(2026, 10, 6, 12, 30), 2)               # Tuesday has no block
    assert not sl.run_ok(free, day(2026, 10, 6, 16, 30), 2)           # would pass 17:00
    assert sl.max_run(free, day(2026, 10, 7, 12)) == 2                # 12:00 and 12:30, then the block
    assert sl.max_run(free, day(2026, 10, 6, 10)) == 3                # capped at the allowance
    assert not sl.run_ok(free, day(2026, 10, 6, 10), 4)


def test_calendar_events_and_buffer_block_slots():
    busy = [(day(2026, 10, 6, 11), day(2026, 10, 6, 12))]
    free = set(sl.free_slots(busy, NOW))
    assert day(2026, 10, 6, 11) not in free and day(2026, 10, 6, 11, 30) not in free
    assert day(2026, 10, 6, 10, 30) in free and day(2026, 10, 6, 12) in free
    padded = set(sl.free_slots(busy, NOW, sl.Rules(buffer_min=30)))
    assert day(2026, 10, 6, 10, 30) not in padded and day(2026, 10, 6, 12) not in padded


def test_horizon_and_labels():
    free = sl.free_slots([], NOW)
    assert max(t.date() for t in free) <= (NOW + dt.timedelta(days=8)).date()
    assert sl.label(day(2026, 10, 7, 14, 30)) == "Wed 07 Oct, 14:30 (Berlin time)"
    assert sl.label(day(2026, 10, 7, 14, 30), 2) == "Wed 07 Oct, 14:30-15:30 (Berlin time)"


# ---------------------------------------------------------------- fake Google Calendar

class Resp:
    def __init__(self, status):
        self.status, self.reason = status, "x"


def http_error(status):
    return HttpError(Resp(status), b"{}")


class FakeCalendar:
    def __init__(self):
        self.store, self.insert_calls, self.timeout_once = {}, 0, False

    def events(self):
        fake = self

        class Call:
            def __init__(self, fn):
                self.fn = fn

            def execute(self):
                return self.fn()

        class Events:
            def get(self, calendarId, eventId):
                def f():
                    if eventId not in fake.store:
                        raise http_error(404)
                    return fake.store[eventId]
                return Call(f)

            def list(self, privateExtendedProperty=None, **kw):
                def f():
                    items = [e for e in fake.store.values() if e.get("status") != "cancelled"]
                    if privateExtendedProperty:
                        k, v = privateExtendedProperty.split("=", 1)
                        items = [e for e in items
                                 if ((e.get("extendedProperties") or {}).get("private") or {}).get(k) == v]
                    return {"items": items}
                return Call(f)

            def update(self, calendarId, eventId, body):
                def f():
                    fake.store[eventId] = {**body, "status": "confirmed"}
                    return fake.store[eventId]
                return Call(f)

            def patch(self, calendarId, eventId, body):
                def f():
                    fake.store[eventId] = {**fake.store[eventId], **body}
                    return fake.store[eventId]
                return Call(f)

            def delete(self, calendarId, eventId):
                def f():
                    if eventId not in fake.store:
                        raise http_error(404)
                    fake.store[eventId] = {**fake.store[eventId], "status": "cancelled"}  # Google keeps the id
                    return {}
                return Call(f)

            def insert(self, calendarId, body):
                def f():
                    fake.insert_calls += 1
                    if body["id"] in fake.store:
                        raise http_error(409)
                    fake.store[body["id"]] = {**body, "status": "confirmed"}
                    if fake.timeout_once:  # the insert landed, but the client saw a timeout
                        fake.timeout_once = False
                        raise TimeoutError("timed out")
                    return fake.store[body["id"]]
                return Call(f)

        return Events()


T = day(2026, 10, 6, 10)  # a Tuesday 10:00
ALL = [t for t in sl.free_slots([], NOW)]


@pytest.fixture
def cal(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(m, "service", lambda: fake)
    monkeypatch.setattr(m, "_free", lambda: list(ALL))
    return fake


def book(email="a@b.co", start=T, slots=1):
    return json.loads(m.create_booking("Ann", email, "ML", start.isoformat(), slots))


def test_one_slot_booking_reports_what_is_left(cal):
    r = book()
    assert r["status"] == "created" and r["slots"] == 1 and r["remaining"] == 2


def test_a_longer_meeting_is_one_event_with_the_right_end(cal):
    r = book(slots=3)
    assert r["status"] == "created" and r["remaining"] == 0 and "10:00-11:30" in r["label"]
    (event,) = cal.store.values()
    assert event["end"]["dateTime"] == (T + sl.SLOT * 3).isoformat()
    assert event["extendedProperties"]["private"]["twin_slots"] == "3"


def test_allowance_is_three_slots_in_total_however_they_are_split(cal):
    assert book(slots=2)["status"] == "created"                      # 60 min
    assert book(start=day(2026, 10, 8, 10))["status"] == "created"    # + 30 min on another day = 3 slots
    over = book(start=day(2026, 10, 8, 15))
    assert over["status"] == "limit" and over["remaining"] == 0


def test_three_separate_calls_are_fine_and_a_fourth_is_refused(cal):
    for i in range(3):
        assert book(start=T + dt.timedelta(hours=i))["status"] == "created"
    assert book(start=T + dt.timedelta(hours=5))["status"] == "limit"
    assert book(email="someone@else.co", start=T + dt.timedelta(hours=5))["status"] == "created"  # per visitor


def test_a_run_that_is_not_fully_free_is_refused(cal, monkeypatch):
    monkeypatch.setattr(m, "_free", lambda: [t for t in ALL if t != T + sl.SLOT])  # 10:30 is taken
    assert book(slots=2)["status"] == "slot_taken"
    assert book(slots=1)["status"] == "created"


def test_retries_are_idempotent_including_after_a_timeout(cal, monkeypatch):
    assert book(slots=2)["status"] == "created"
    monkeypatch.setattr(m, "_free", lambda: [t for t in ALL if t not in (T, T + sl.SLOT)])  # now busy: our own event
    again = book(slots=2)
    assert again["status"] == "already_created" and cal.insert_calls == 1
    cal.store.clear()
    monkeypatch.setattr(m, "_free", lambda: list(ALL))
    cal.timeout_once = True
    assert book(start=day(2026, 10, 6, 14))["status"] == "already_created" and cal.insert_calls == 2


def test_deleted_event_ids_can_be_reused(cal):
    eid = m.event_id("a@b.co", T.isoformat())
    cal.store[eid] = {"id": eid, "status": "cancelled"}
    assert book()["status"] == "created"


def test_invalid_input_is_refused(cal):
    assert json.loads(m.create_booking("Ann", "nope", "ML", T.isoformat()))["status"] == "invalid"
    assert book(slots=4)["status"] == "invalid" and book(slots=0)["status"] == "invalid"


def test_allowance_tool(cal):
    book(slots=2)
    r = json.loads(m.get_allowance("a@b.co"))
    assert r["held"] == 2 and r["remaining"] == 1
    assert json.loads(m.get_allowance("x@y.co"))["remaining"] == 3


# ---------------------------------------------------------------- extending afterwards

def extend(total, email="a@b.co", start=T):
    return json.loads(m.extend_booking(email, start.isoformat(), total))


def test_a_booking_can_be_extended_to_an_hour_and_then_ninety_minutes(cal):
    book()
    r = extend(2)
    assert r["status"] == "extended" and "10:00-11:00" in r["label"] and r["remaining"] == 1
    (event,) = cal.store.values()
    assert event["end"]["dateTime"] == (T + sl.SLOT * 2).isoformat()
    assert extend(3)["status"] == "extended"
    assert extend(3)["status"] == "already_extended"           # retries do nothing


def test_extension_respects_the_allowance(cal):
    book(slots=2)
    book(start=day(2026, 10, 8, 10))                          # 3 slots held in total
    r = extend(3)
    assert r["status"] == "limit" and r["remaining"] == 0


def test_extension_is_refused_when_the_next_slot_is_not_free(cal, monkeypatch):
    book()
    monkeypatch.setattr(m, "_free", lambda: [t for t in ALL if t not in (T, T + sl.SLOT)])
    assert extend(2)["status"] == "slot_taken"
    lim = json.loads(m.extension_limit("a@b.co", T.isoformat()))
    assert lim["current"] == 1 and lim["max_total"] == 1


def test_extension_cannot_run_into_the_wednesday_block(cal):
    wed = day(2026, 10, 7, 12)                                 # 12:00-12:30, then the block from 13:00
    book(start=wed)
    assert json.loads(m.extension_limit("a@b.co", wed.isoformat()))["max_total"] == 2
    assert extend(2, start=wed)["status"] == "extended"
    assert extend(3, start=wed)["status"] == "slot_taken"


def test_only_the_owner_can_extend_and_unknown_bookings_are_not_found(cal):
    book()
    assert extend(2, email="other@x.co")["status"] == "not_found"
    assert extend(2, start=day(2026, 10, 6, 15))["status"] == "not_found"
    assert json.loads(m.extension_limit("a@b.co", T.isoformat()))["max_total"] == 3
    assert extend(1)["status"] == "invalid"


def test_a_day_whose_first_free_slot_is_after_14_is_not_listed_twice(cal, monkeypatch):
    late = [x for x in ALL if not (x.date() == T.date() and x.hour < 14)]            # Tuesday: nothing free before 14:00
    monkeypatch.setattr(m, "_free", lambda: late)
    offered = json.loads(m.get_free_slots())
    starts_ = [o["start"] for o in offered]
    assert len(starts_) == len(set(starts_))                                       # no repeats
    assert starts_.count(day(2026, 10, 6, 14).isoformat()) == 1                    # Tuesday 14:00 listed exactly once
    assert all(1 <= o["max_slots"] <= 3 for o in offered)


# ---------------------------------------------------------------- checking a visitor-typed time directly

def test_check_time_reports_free_with_how_long_it_could_run(cal):
    r = json.loads(m.check_time(T.isoformat()))
    assert r["status"] == "free" and r["max_slots"] == 3


def test_check_time_on_a_taken_slot_offers_nearby_free_times(cal, monkeypatch):
    monkeypatch.setattr(m, "_free", lambda: [t for t in ALL if t != T])    # exactly the requested time is gone
    r = json.loads(m.check_time(T.isoformat()))
    assert r["status"] == "busy" and len(r["nearby"]) <= 4
    assert all(x["start"] != T.isoformat() for x in r["nearby"])


def test_check_time_rejects_a_run_that_would_cross_the_wednesday_block(cal):
    wed_1230 = day(2026, 10, 7, 12, 30)
    r = json.loads(m.check_time(wed_1230.isoformat(), slots=2))            # would run into the 13:00 block
    assert r["status"] == "busy"


def test_check_time_invalid_input(cal):
    assert json.loads(m.check_time("not-a-date"))["status"] == "invalid"
    assert json.loads(m.check_time(T.isoformat(), slots=4))["status"] == "invalid"


# ---------------------------------------------------------------- cancelling a booked call

def cancel(email="a@b.co", start=T):
    return json.loads(m.cancel_booking(email, start.isoformat()))


def test_cancelling_removes_the_event_and_gives_the_allowance_back(cal):
    book(slots=3)
    assert json.loads(m.get_allowance("a@b.co"))["remaining"] == 0
    r = cancel()
    assert r["status"] == "cancelled" and "10:00-11:30" in r["label"]
    assert json.loads(m.get_allowance("a@b.co"))["remaining"] == 3
    assert book(start=day(2026, 10, 8, 10))["status"] == "created"      # they can book again


def test_cancel_is_idempotent_and_only_for_the_owner(cal):
    book()
    assert cancel(email="other@x.co")["status"] == "not_found"          # someone else's email cannot cancel it
    assert cancel(start=day(2026, 10, 6, 15))["status"] == "not_found"  # no booking at that time
    assert cancel()["status"] == "cancelled"
    assert cancel()["status"] == "not_found"                             # already gone
    assert cancel(email="nope")["status"] == "invalid"


def test_a_cancelled_time_can_be_booked_again(cal):
    book()
    cancel()
    assert book()["status"] == "created"


# ---------------------------------------------------------------- explaining why a time is not bookable

NOW9 = day(2026, 10, 8, 18)          # Thursday evening
FREE9 = sl.free_slots([], NOW9)      # what the rules allow from that moment


def test_why_not_names_the_rule_that_applies():
    assert sl.why_not(day(2026, 10, 9, 10), NOW9) == "notice"                 # tomorrow morning: under 24 hours away
    assert sl.why_not(day(2026, 10, 10, 11), NOW9) == "weekend"
    assert sl.why_not(day(2026, 10, 12, 8), NOW9) == "hours"                  # Monday 08:00, before the working day
    assert sl.why_not(day(2026, 10, 12, 16, 30), NOW9) == ""                  # the last slot of the day is fine
    assert sl.why_not(day(2026, 10, 12, 17), NOW9) == "hours"
    assert sl.why_not(day(2026, 10, 14, 13), NOW9) == "blocked"               # Wednesday 13:00
    assert sl.why_not(day(2026, 10, 30, 10), NOW9) == "horizon"
    assert sl.why_not(day(2026, 10, 12, 11), NOW9) == ""                      # allowed by the rules: only the calendar can say no
    for code in ("notice", "horizon", "weekend", "hours", "blocked", "taken", "full"):
        assert sl.reason_text(code)


def test_check_time_gives_the_reason(cal, monkeypatch):
    monkeypatch.setattr(m, "_now", lambda: NOW9)
    monkeypatch.setattr(m, "_free", lambda: list(FREE9))
    assert json.loads(m.check_time(day(2026, 10, 9, 10).isoformat()))["reason"] == "notice"
    assert json.loads(m.check_time(day(2026, 10, 10, 10).isoformat()))["reason"] == "weekend"
    monkeypatch.setattr(m, "_free", lambda: [t for t in FREE9 if t != day(2026, 10, 12, 11)])
    r = json.loads(m.check_time(day(2026, 10, 12, 11).isoformat()))
    assert r["reason"] == "taken" and r["why"] == "that time is already taken"


def test_check_day_lists_the_days_free_times_or_says_why_there_are_none(cal, monkeypatch):
    monkeypatch.setattr(m, "_now", lambda: NOW9)
    monkeypatch.setattr(m, "_free", lambda: list(FREE9))
    ok = json.loads(m.check_day("2026-10-13"))
    assert ok["status"] == "ok" and ok["slots"] and ok["label"] == "Tue 13 Oct" and len(ok["slots"]) <= 8
    assert all(s["start"].startswith("2026-10-13") for s in ok["slots"])
    soon = json.loads(m.check_day("2026-10-09"))                               # tomorrow
    assert soon["slots"] == [] and soon["reason"] == "notice" and soon["next"]
    weekend = json.loads(m.check_day("2026-10-10"))
    assert weekend["reason"] == "weekend" and weekend["next"][0]["start"].startswith("2026-10-12")
    assert json.loads(m.check_day("tomorrow"))["status"] == "invalid"
