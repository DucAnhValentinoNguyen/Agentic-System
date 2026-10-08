"""Cancel links: genuine ones work once for the right booking; forged, altered or foreign ones do nothing."""

import json
import sys
from pathlib import Path

import pytest
from app import calendar_mcp as m
from app import cancel as cx
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from test_booking import (
    FakeCalendar,
    T,
    book,
)

SECRET = "s3cret-for-tests"


def test_a_genuine_token_verifies_and_a_changed_one_does_not():
    eid = cx.event_id("a@b.co", "2026-10-06T10:00:00+02:00")
    token = cx.sign(eid, SECRET)
    assert cx.verify(token, SECRET) == eid
    assert cx.verify(token, "another secret") is None                       # wrong key
    assert cx.verify(token[:-1] + ("0" if token[-1] != "0" else "1"), SECRET) is None   # one character changed
    assert cx.verify(cx.sign("0" * 32, SECRET), SECRET) == "0" * 32          # any well-formed id signs for its own id only
    assert cx.verify(eid + "." + cx.sign("1" * 32, SECRET).partition(".")[2], SECRET) is None   # signature of a different id
    for bad in ("", "x", "no-dot", "g" * 32 + ".abc", "a" * 200, ".", eid + "."):
        assert cx.verify(bad, SECRET) is None
    assert cx.verify(token, "") is None                                      # no secret configured: links are off


def test_the_token_carries_no_personal_data():
    token = cx.sign(cx.event_id("someone@example.com", "2026-10-06T10:00:00+02:00"), SECRET)
    assert "someone" not in token and "@" not in token and len(token) < 80


@pytest.fixture
def cal(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(m, "service", lambda: fake)
    monkeypatch.setattr(m, "_free", lambda: [t for t in m.sl.free_slots([], m.dt.datetime(2026, 10, 3, 9, tzinfo=m.TZ))])
    return fake


def info(eid):
    return json.loads(m.booking_info(eid))


def test_booking_info_and_cancel_by_id_work_only_for_bookings_this_assistant_made(cal):
    book(slots=2)
    eid = cx.event_id("a@b.co", T.isoformat())
    assert info(eid)["status"] == "ok" and "10:00-11:00" in info(eid)["label"]
    cal.store["f" * 32] = {"id": "f" * 32, "status": "confirmed", "start": {"dateTime": T.isoformat()},
                           "end": {"dateTime": T.isoformat()}}              # an event of Duc-Anh's own, not made here
    assert info("f" * 32)["status"] == "not_found"
    assert json.loads(m.cancel_by_id("f" * 32))["status"] == "not_found"
    assert "f" * 32 in cal.store and cal.store["f" * 32]["status"] == "confirmed"      # untouched
    assert json.loads(m.cancel_by_id("not-an-id"))["status"] == "not_found"
    assert json.loads(m.cancel_by_id(eid))["status"] == "cancelled"
    assert json.loads(m.cancel_by_id(eid))["status"] == "already_cancelled"          # idempotent
    assert info(eid)["status"] == "cancelled"


class Cal:
    """What the web page needs from the calendar client."""
    async def booking_info(self, eid):
        return json.loads(m.booking_info(eid))

    async def cancel_id(self, eid):
        return json.loads(m.cancel_by_id(eid))


@pytest.fixture
def client(cal):
    app = FastAPI()
    app.include_router(cx.make_router(lambda: Cal(), lambda: SECRET, lambda request: False))
    return TestClient(app)


def test_opening_the_link_shows_a_page_and_changes_nothing(client, cal):
    book()
    token = cx.sign(cx.event_id("a@b.co", T.isoformat()), SECRET)
    r = client.get("/v1/cancel", params={"t": token})
    assert r.status_code == 200 and "Cancel this call" in r.text and "Tue 06 Oct" in r.text
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-frame-options"] == "DENY"
    assert info(cx.event_id("a@b.co", T.isoformat()))["status"] == "ok"      # a link preview must not cancel


def test_the_button_cancels_and_a_second_press_says_so(client):
    book()
    token = cx.sign(cx.event_id("a@b.co", T.isoformat()), SECRET)
    r = client.post("/v1/cancel", content=f"t={token}", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and "is cancelled" in r.text
    again = client.get("/v1/cancel", params={"t": token})
    assert "Already cancelled" in again.text


def test_forged_and_foreign_links_get_nothing(client, cal):
    book()
    eid = cx.event_id("a@b.co", T.isoformat())
    forged = client.post("/v1/cancel", content=f"t={eid}.{'0' * 32}", headers={"content-type": "application/x-www-form-urlencoded"})
    assert forged.status_code == 404 and info(eid)["status"] == "ok"
    assert client.get("/v1/cancel").status_code == 404                        # no token at all
    other = cx.sign("e" * 32, SECRET)                                         # genuine signature, but no such booking
    assert client.get("/v1/cancel", params={"t": other}).status_code == 404


def test_rate_limited_clients_are_turned_away(cal):
    app = FastAPI()
    app.include_router(cx.make_router(lambda: Cal(), lambda: SECRET, lambda request: True))
    assert TestClient(app).get("/v1/cancel", params={"t": "x"}).status_code == 429


def test_status_says_whether_the_call_is_still_booked(client):
    book()
    token = cx.sign(cx.event_id("a@b.co", T.isoformat()), SECRET)
    assert client.get("/v1/cancel/status", params={"t": token}).json() == {"active": True}
    client.post("/v1/cancel", content=f"t={token}", headers={"content-type": "application/x-www-form-urlencoded"})
    assert client.get("/v1/cancel/status", params={"t": token}).json() == {"active": False}
    assert client.get("/v1/cancel/status", params={"t": "forged"}).json() == {"active": False}   # a made-up link is not a call
    assert client.get("/v1/cancel/status", params={"t": token}).headers["cache-control"] == "no-store"
