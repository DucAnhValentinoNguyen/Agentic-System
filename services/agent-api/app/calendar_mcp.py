"""Calendar MCP server (stdio): free slots, allowance, booking, extending a booking, and sending a message.

Runs as a subprocess of agent-api. Scope is calendar.events only, so availability is computed from the event list
rather than the free/busy API. The scheduling rules live in `slots.py` (pure and tested).

Booking is idempotent: the event id derives from (email, start), so a retry after a timeout hits HTTP 409 and is
reconciled instead of creating a second event. A visitor (identified by email) can hold at most
`MAX_SLOTS_PER_VISITOR` upcoming 30-minute slots in total, as separate calls or as one longer meeting.
"""

import base64
import datetime as dt
import hashlib
import json
import os
import re
from email.message import EmailMessage
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from . import slots as sl

TZ = sl.TZ
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


def _free() -> list[dt.datetime]:
    now = dt.datetime.now(TZ)
    return sl.free_slots(_busy(now, now + dt.timedelta(days=sl.RULES.horizon_days + 1)), now)


def event_id(email: str, start_iso: str) -> str:
    return hashlib.sha1(f"{email.lower()}|{start_iso}".encode()).hexdigest()[:32]  # a-f0-9


def _event_slots(e: dict) -> int:
    """How many 30-minute slots a booked event covers (older events without the property: from their length)."""
    props = (e.get("extendedProperties") or {}).get("private") or {}
    try:
        return max(1, int(props.get("twin_slots", "")))
    except ValueError:
        s, en = (dt.datetime.fromisoformat(e[k]["dateTime"]) for k in ("start", "end"))
        return max(1, round((en - s) / sl.SLOT))


def _held(svc, email: str, exclude_id: str | None = None) -> int:
    """Upcoming slots this visitor already holds (the calendar is the source of truth, so it survives restarts)."""
    items = svc.events().list(
        calendarId="primary", timeMin=dt.datetime.now(TZ).isoformat(), singleEvents=True,
        privateExtendedProperty=f"twin_email={email.lower()}", maxResults=50).execute().get("items", [])
    return sum(_event_slots(e) for e in items if e["id"] != exclude_id and e.get("status") != "cancelled")


def _out(**kw) -> str:
    return json.dumps(kw)


@mcp.tool()
def get_free_slots() -> str:
    """Return up to 6 free start times in the next week, spread over several days (JSON).
    Each has max_slots: how many consecutive 30-minute slots are free from that start (1-3)."""
    free = _free()
    fs = set(free)
    per_day: dict[dt.date, list[dt.datetime]] = {}
    for t in free:
        per_day.setdefault(t.date(), []).append(t)
    picked: list[dt.datetime] = []
    for day_slots in per_day.values():  # a morning start and an afternoon start per day
        picked.append(day_slots[0])
        afternoon = [t for t in day_slots if t.hour >= 14]
        if afternoon:
            picked.append(afternoon[0])
    picked = list(dict.fromkeys(picked))[:6]  # a day whose first free slot is after 14:00 would list it twice
    return json.dumps([{"start": t.isoformat(), "label": sl.label(t), "max_slots": sl.max_run(fs, t)} for t in picked])


@mcp.tool()
def check_time(start: str, slots: int = 1) -> str:
    """Check whether `slots` consecutive 30-minute slots starting at `start` (ISO 8601, Europe/Berlin) are
    bookable right now. Returns JSON: {status: "free", label, max_slots} when it is, or {status: "busy",
    nearby: [{start, label, max_slots}, ...]} with up to 4 free times close to the request when it is not
    (outside the work window, within the notice period, or already taken). {status: "invalid"} for bad input."""
    try:
        start_dt = dt.datetime.fromisoformat(start)
    except ValueError:
        return _out(status="invalid")
    if not 1 <= slots <= sl.MAX_SLOTS_PER_VISITOR:
        return _out(status="invalid")
    free = set(_free())
    if sl.run_ok(free, start_dt, slots):
        return _out(status="free", label=sl.label(start_dt, slots), max_slots=sl.max_run(free, start_dt))
    nearby = sorted(free, key=lambda t: abs((t - start_dt).total_seconds()))[:4]
    nearby.sort()
    return _out(status="busy", nearby=[
        {"start": t.isoformat(), "label": sl.label(t), "max_slots": sl.max_run(free, t)} for t in nearby])


@mcp.tool()
def get_allowance(email: str) -> str:
    """How many of the visitor's 3 half-hour slots are still available. Returns JSON {held, remaining}."""
    if not EMAIL.match(email):
        return _out(status="invalid")
    held = _held(service(), email)
    return _out(status="ok", held=held, remaining=max(0, sl.MAX_SLOTS_PER_VISITOR - held))


@mcp.tool()
def create_booking(name: str, email: str, topic: str, start: str, slots: int = 1) -> str:
    """Create one event of `slots` consecutive 30-minute slots (1-3). Idempotent. Returns JSON {status, label, ...}."""
    name, topic = " ".join(name.split())[:80], " ".join(topic.split())[:300]
    if not EMAIL.match(email) or not name or not topic or not 1 <= slots <= sl.MAX_SLOTS_PER_VISITOR:
        return _out(status="invalid")
    start_dt = dt.datetime.fromisoformat(start)
    svc = service()
    eid = event_id(email, start_dt.isoformat())
    # Reconcile first: if this exact booking already exists, a retry must report success,
    # not "slot taken" (the slot is busy precisely because of our own event).
    try:
        found = svc.events().get(calendarId="primary", eventId=eid).execute()
        if found.get("status") != "cancelled":
            return _out(status="already_created", label=sl.label(start_dt, _event_slots(found)))
    except HttpError as e:
        if e.status_code != 404:
            return _out(status="failed")
    # Only offer what is actually free right now: never an arbitrary visitor-chosen time or run.
    if not sl.run_ok(set(_free()), start_dt, slots):
        return _out(status="slot_taken")
    held = _held(svc, email)
    if held + slots > sl.MAX_SLOTS_PER_VISITOR:
        return _out(status="limit", remaining=max(0, sl.MAX_SLOTS_PER_VISITOR - held))
    body = {
        "id": eid, "summary": f"Call: {name} (via site assistant)",
        "description": f"Booked through the Twin assistant.\nName: {name}\nEmail: {email}\nTopic: {topic}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Berlin"},
        "end": {"dateTime": (start_dt + sl.SLOT * slots).isoformat(), "timeZone": "Europe/Berlin"},
        "extendedProperties": {"private": {"twin_email": email.lower(), "twin_slots": str(slots)}},
    }
    done = {"label": sl.label(start_dt, slots), "slots": slots,
            "remaining": sl.MAX_SLOTS_PER_VISITOR - held - slots}
    for attempt in range(2):
        try:
            svc.events().insert(calendarId="primary", body=body).execute()
            return _out(status="created", **done)
        except HttpError as e:
            if e.status_code == 409:  # id exists: a prior attempt landed, or it was deleted earlier
                prev = svc.events().get(calendarId="primary", eventId=eid).execute()
                if prev.get("status") == "cancelled":  # Google keeps deleted ids; revive it
                    svc.events().update(calendarId="primary", eventId=eid,
                                        body={**body, "status": "confirmed"}).execute()
                    return _out(status="created", **done)
                return _out(status="already_created", **done)
            if e.status_code >= 500 and attempt == 0:
                continue
            return _out(status="failed")
        except Exception:  # noqa: BLE001 - timeout etc: the insert may or may not have landed
            try:
                svc.events().get(calendarId="primary", eventId=eid).execute()
                return _out(status="already_created", **done)
            except Exception:  # noqa: BLE001
                if attempt == 1:
                    return _out(status="failed")
    return _out(status="failed")


def _own_event(svc, email: str, start_dt: dt.datetime):
    """The visitor's own booked event starting at start_dt, or None."""
    try:
        e = svc.events().get(calendarId="primary", eventId=event_id(email, start_dt.isoformat())).execute()
    except HttpError:
        return None
    props = (e.get("extendedProperties") or {}).get("private") or {}
    if e.get("status") == "cancelled" or props.get("twin_email") != email.lower():
        return None
    return e


@mcp.tool()
def extension_limit(email: str, start: str) -> str:
    """How long can this visitor's booking starting at `start` be made? Returns JSON {current, max_total}."""
    if not EMAIL.match(email):
        return _out(status="invalid")
    start_dt = dt.datetime.fromisoformat(start)
    svc = service()
    e = _own_event(svc, email, start_dt)
    if e is None:
        return _out(status="not_found")
    n = _event_slots(e)
    free = set(_free())
    room = 0  # free slots directly after the event
    while n + room < sl.MAX_SLOTS_PER_VISITOR and start_dt + sl.SLOT * (n + room) in free:
        room += 1
    allowance = sl.MAX_SLOTS_PER_VISITOR - _held(svc, email)
    return _out(status="ok", current=n, max_total=n + min(room, max(0, allowance)))


@mcp.tool()
def extend_booking(email: str, start: str, total_slots: int) -> str:
    """Make the visitor's booking `total_slots` long (it only grows, into the slots right after it). Idempotent."""
    if not EMAIL.match(email) or not 2 <= total_slots <= sl.MAX_SLOTS_PER_VISITOR:
        return _out(status="invalid")
    start_dt = dt.datetime.fromisoformat(start)
    svc = service()
    e = _own_event(svc, email, start_dt)
    if e is None:
        return _out(status="not_found")
    n = _event_slots(e)
    if n >= total_slots:
        return _out(status="already_extended", label=sl.label(start_dt, n), slots=n)
    free = set(_free())
    if not all(start_dt + sl.SLOT * i in free for i in range(n, total_slots)):
        return _out(status="slot_taken")
    held = _held(svc, email, exclude_id=e["id"])
    if held + total_slots > sl.MAX_SLOTS_PER_VISITOR:
        return _out(status="limit", remaining=max(0, sl.MAX_SLOTS_PER_VISITOR - held - n))
    props = {**((e.get("extendedProperties") or {}).get("private") or {}), "twin_slots": str(total_slots)}
    try:
        svc.events().patch(calendarId="primary", eventId=e["id"], body={
            "end": {"dateTime": (start_dt + sl.SLOT * total_slots).isoformat(), "timeZone": "Europe/Berlin"},
            "extendedProperties": {"private": props}}).execute()
    except Exception:  # noqa: BLE001
        return _out(status="failed")
    return _out(status="extended", label=sl.label(start_dt, total_slots), slots=total_slots,
                remaining=sl.MAX_SLOTS_PER_VISITOR - held - total_slots)


@mcp.tool()
def cancel_booking(email: str, start: str) -> str:
    """Cancel the visitor's own booking that starts at `start`. Only an event this assistant created for that email
    can be cancelled. Idempotent. Returns JSON {status}: cancelled | not_found | invalid | failed."""
    if not EMAIL.match(email):
        return _out(status="invalid")
    try:
        start_dt = dt.datetime.fromisoformat(start)
    except ValueError:
        return _out(status="invalid")
    svc = service()
    e = _own_event(svc, email, start_dt)
    if e is None:
        return _out(status="not_found")
    try:
        svc.events().delete(calendarId="primary", eventId=e["id"]).execute()
    except HttpError as err:
        return _out(status="cancelled" if err.status_code in (404, 410) else "failed")
    return _out(status="cancelled", label=sl.label(start_dt, _event_slots(e)))


@mcp.tool()
def send_message(sender_email: str, message: str, name: str = "", kind: str = "", reference: str = "") -> str:
    """Email a visitor's message to Duc-Anh. kind="issue" marks a problem report about the assistant; `reference`
    is the chat's session id so the conversation can be looked up. Returns JSON {status}: sent | invalid | failed."""
    message = "".join(ch for ch in message if ch == "\n" or ch >= " ").strip()
    if not EMAIL.match(sender_email) or not 1 <= len(message) <= 2000:
        return json.dumps({"status": "invalid"})
    name = " ".join(name.split())[:80]
    mail = EmailMessage()
    mail["To"] = OWNER_EMAIL
    mail["Reply-To"] = sender_email  # validated: no whitespace or newlines, so no header injection
    issue = kind == "issue"
    reference = re.sub(r"[^A-Za-z0-9_-]", "", reference)[:64]
    mail["Subject"] = f"[Twin issue] Report from {sender_email}" if issue else f"[Twin] Message from {sender_email}"
    intro = ("A visitor reported a problem with the assistant on ducanhvalentinonguyen.com.\n" if issue else
             "A visitor left you a message through the assistant on ducanhvalentinonguyen.com.\n")
    ref = f"Chat reference (session id, to find the conversation in the logs): {reference}\n" if reference else ""
    mail.set_content(
        intro + f"From: {name + ' ' if name else ''}<{sender_email}> (typed by the visitor, not verified)\n"
        + ref + "Reply to this email to answer them.\n\n---\n" + message + "\n")
    try:
        raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        gmail_service().users().messages().send(userId="me", body={"raw": raw}).execute()
        return json.dumps({"status": "sent"})
    except Exception:  # noqa: BLE001 - no automatic retry: a duplicate email is worse than a clear failure
        return json.dumps({"status": "failed"})


if __name__ == "__main__":
    mcp.run(transport="stdio")
