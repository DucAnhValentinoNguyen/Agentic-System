"""create_booking idempotency with a fake Google Calendar service."""

import datetime as dt
import json

import pytest
from app import calendar_mcp as m
from googleapiclient.errors import HttpError

START = "2026-10-05T14:00:00+02:00"


class Resp:
    def __init__(self, status):
        self.status, self.reason = status, "x"


def http_error(status):
    return HttpError(Resp(status), b"{}")


class FakeCalendar:
    def __init__(self):
        self.store, self.insert_calls, self.timeout_once = {}, 0, False

    def events_api(self):
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

            def list(self, **kw):
                return Call(lambda: {"items": [
                    e for e in fake.store.values() if e.get("status") != "cancelled"]})

            def update(self, calendarId, eventId, body):
                def f():
                    fake.store[eventId] = {**body, "status": "confirmed"}
                    return fake.store[eventId]
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

    def events(self):
        return self.events_api()


@pytest.fixture
def cal(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(m, "service", lambda: fake)
    monkeypatch.setattr(m, "_free_slots", lambda: [dt.datetime.fromisoformat(START)])
    return fake


def book(email="a@b.co"):
    return json.loads(m.create_booking("Ann", email, "ML", START))


def test_creates_once(cal):
    assert book()["status"] == "created"


def test_retry_reports_already_created_not_slot_taken(cal):
    assert book()["status"] == "created"
    # the slot is now "busy" because of our own event; a retry must still succeed idempotently
    monkey_slots = []
    m._free_slots = lambda: monkey_slots
    assert book()["status"] == "already_created"
    assert cal.insert_calls == 1


def test_timeout_after_insert_does_not_duplicate(cal):
    cal.timeout_once = True
    assert book()["status"] == "already_created"  # reconciled by get(), no second insert
    assert cal.insert_calls == 1


def test_deleted_id_is_revived(cal):
    eid = m.event_id("a@b.co", START)
    cal.store[eid] = {"id": eid, "status": "cancelled"}
    assert book()["status"] == "created"


def test_invalid_email(cal):
    assert json.loads(m.create_booking("Ann", "nope", "ML", START))["status"] == "invalid"
