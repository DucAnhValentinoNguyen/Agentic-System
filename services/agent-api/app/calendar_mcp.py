"""Calendar MCP server (stdio). Two tools: get_free_slots, create_booking.

Runs as a subprocess of agent-api. Scope is calendar.events only, so availability is computed
from the event list rather than the free/busy API. create_booking is idempotent: the event id is
derived from (email, start), so a retry after a timeout hits HTTP 409 and is reconciled instead of
creating a second event.
"""

import base64
import datetime as dt
import hashlib
import json
import os
import re
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

TZ = ZoneInfo("Europe/Berlin")
WORK_START, WORK_END = 10, 17      # local hours; slots are 30 minutes
MIN_NOTICE = dt.timedelta(hours=24)
HORIZON_DAYS = 8
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")

mcp = FastMCP("calendar")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "anh.nguyen1@campus.lmu.de")
_svc = None
_gmail = None
_creds = None


def credentials():
    global _creds
    if _creds is None:
        raw = os.environ.get("GOOGLE_CALENDAR_TOKEN")
        info = json.loads(raw) if raw else json.loads(Path("token_calendar.json").read_text())
        _creds = Credentials.from_authorized_user_info(info)
        _creds.refresh(Request())
    return _creds


def service():
    global _svc
    if _svc is None:
        _svc = build("calendar", "v3", credentials=credentials(), cache_discovery=False)
    return _svc


def gmail_service():
    global _gmail
    if _gmail is None:
        _gmail = build("gmail", "v1", credentials=credentials(), cache_discovery=False)
    return _gmail


def _busy(start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    items = service().events().list(
        calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=250).execute().get("items", [])
    out = []
    for e in items:
        if e.get("transparency") == "transparent" or e.get("status") == "cancelled":
            continue
        s, en = e["start"], e["end"]
        if "dateTime" in s:
            out.append((dt.datetime.fromisoformat(s["dateTime"]), dt.datetime.fromisoformat(en["dateTime"])))
        else:  # all-day event blocks the whole day
            d0 = dt.datetime.fromisoformat(s["date"]).replace(tzinfo=TZ)
            d1 = dt.datetime.fromisoformat(en["date"]).replace(tzinfo=TZ)
            out.append((d0, d1))
    return out


def _free_slots() -> list[dt.datetime]:
    now = dt.datetime.now(TZ)
    earliest = now + MIN_NOTICE
    end = now + dt.timedelta(days=HORIZON_DAYS + 1)
    busy = _busy(now, end)
    slots: list[dt.datetime] = []
    day = earliest.date()
    while day <= (now + dt.timedelta(days=HORIZON_DAYS)).date():
        if dt.date.weekday(day) < 5:
            t = dt.datetime(day.year, day.month, day.day, WORK_START, tzinfo=TZ)
            stop = dt.datetime(day.year, day.month, day.day, WORK_END, tzinfo=TZ)
            while t < stop:
                t2 = t + dt.timedelta(minutes=30)
                if t >= earliest and not any(t < b1 and t2 > b0 for b0, b1 in busy):
                    slots.append(t)
                t = t2
        day += dt.timedelta(days=1)
    return slots


def _label(t: dt.datetime) -> str:
    return t.strftime("%a %d %b, %H:%M") + " (Berlin time)"


@mcp.tool()
def get_free_slots() -> str:
    """Return up to 6 free 30-minute slots in the next week, spread over several days (JSON)."""
    slots = _free_slots()
    per_day: dict[dt.date, list[dt.datetime]] = {}
    for s in slots:
        per_day.setdefault(s.date(), []).append(s)
    picked: list[dt.datetime] = []
    for day_slots in per_day.values():  # morning slot and an afternoon slot per day
        picked.append(day_slots[0])
        afternoon = [s for s in day_slots if s.hour >= 14]
        if afternoon:
            picked.append(afternoon[0])
    picked = picked[:6]
    return json.dumps([{"start": s.isoformat(), "label": _label(s)} for s in picked])


def event_id(email: str, start_iso: str) -> str:
    return hashlib.sha1(f"{email.lower()}|{start_iso}".encode()).hexdigest()[:32]  # a-f0-9


@mcp.tool()
def create_booking(name: str, email: str, topic: str, start: str) -> str:
    """Create one 30-minute event. Idempotent. Returns JSON {status, label}."""
    name, topic = " ".join(name.split())[:80], " ".join(topic.split())[:300]
    if not EMAIL.match(email) or not name or not topic:
        return json.dumps({"status": "invalid"})
    start_dt = dt.datetime.fromisoformat(start)
    svc = service()
    eid = event_id(email, start)
    # Reconcile first: if this exact booking already exists, a retry must report success,
    # not "slot taken" (the slot is busy precisely because of our own event).
    try:
        found = svc.events().get(calendarId="primary", eventId=eid).execute()
        if found.get("status") != "cancelled":
            return json.dumps({"status": "already_created", "label": _label(start_dt)})
    except HttpError as e:
        if e.status_code != 404:
            return json.dumps({"status": "failed"})
    # Only offer what is actually free right now: never an arbitrary visitor-chosen time.
    if start_dt not in _free_slots():
        return json.dumps({"status": "slot_taken"})
    existing = svc.events().list(
        calendarId="primary", timeMin=dt.datetime.now(TZ).isoformat(), singleEvents=True,
        privateExtendedProperty=f"twin_email={email.lower()}", maxResults=5).execute().get("items", [])
    if any(e["id"] != eid for e in existing):
        return json.dumps({"status": "limit"})  # one upcoming booking per email
    body = {
        "id": eid, "summary": f"Call: {name} (via site assistant)",
        "description": f"Booked through the Twin assistant.\nName: {name}\nEmail: {email}\nTopic: {topic}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Berlin"},
        "end": {"dateTime": (start_dt + dt.timedelta(minutes=30)).isoformat(), "timeZone": "Europe/Berlin"},
        "extendedProperties": {"private": {"twin_email": email.lower()}},
    }
    for attempt in range(2):
        try:
            svc.events().insert(calendarId="primary", body=body).execute()
            return json.dumps({"status": "created", "label": _label(start_dt)})
        except HttpError as e:
            if e.status_code == 409:  # id exists: a prior attempt landed, or it was deleted earlier
                prev = svc.events().get(calendarId="primary", eventId=eid).execute()
                if prev.get("status") == "cancelled":  # Google keeps deleted ids; revive it
                    svc.events().update(calendarId="primary", eventId=eid,
                                        body={**body, "status": "confirmed"}).execute()
                    return json.dumps({"status": "created", "label": _label(start_dt)})
                return json.dumps({"status": "already_created", "label": _label(start_dt)})
            if e.status_code >= 500 and attempt == 0:
                continue
            return json.dumps({"status": "failed"})
        except Exception:  # noqa: BLE001 - timeout etc: the insert may or may not have landed
            try:
                svc.events().get(calendarId="primary", eventId=eid).execute()
                return json.dumps({"status": "already_created", "label": _label(start_dt)})
            except Exception:  # noqa: BLE001
                if attempt == 1:
                    return json.dumps({"status": "failed"})
    return json.dumps({"status": "failed"})


@mcp.tool()
def send_message(sender_email: str, message: str, name: str = "") -> str:
    """Email a visitor's message to Duc-Anh. Returns JSON {status}: sent | invalid | failed."""
    message = "".join(ch for ch in message if ch == "\n" or ch >= " ").strip()
    if not EMAIL.match(sender_email) or not 1 <= len(message) <= 2000:
        return json.dumps({"status": "invalid"})
    name = " ".join(name.split())[:80]
    mail = EmailMessage()
    mail["To"] = OWNER_EMAIL
    mail["Reply-To"] = sender_email  # validated: no whitespace or newlines, so no header injection
    mail["Subject"] = f"[Twin] Message from {sender_email}"
    mail.set_content(
        "A visitor left you a message through the assistant on ducanhvalentinonguyen.com.\n"
        f"From: {name + ' ' if name else ''}<{sender_email}> (typed by the visitor, not verified)\n"
        "Reply to this email to answer them.\n\n---\n" + message + "\n")
    try:
        raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        gmail_service().users().messages().send(userId="me", body={"raw": raw}).execute()
        return json.dumps({"status": "sent"})
    except Exception:  # noqa: BLE001 - no automatic retry: a duplicate email is worse than a clear failure
        return json.dumps({"status": "failed"})


if __name__ == "__main__":
    mcp.run(transport="stdio")
