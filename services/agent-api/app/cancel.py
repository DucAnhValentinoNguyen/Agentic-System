"""Cancel links: a booking can be cancelled by whoever holds its link, and by nobody else.

The token is the calendar event id plus a signature made with a server secret (HMAC-SHA256), so it cannot be guessed or
forged, carries no personal data, and needs no storage. Opening the link only shows a page; the cancellation itself is a
POST from a button, so link scanners and mail previews that fetch links cannot cancel anything.
"""

import hashlib
import hmac
import html
import re
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

EVENT_ID = re.compile(r"[a-f0-9]{32}")


def event_id(email: str, start_iso: str) -> str:
    """The calendar event id of a booking: the same (email, start) always gives the same id, so retries are idempotent."""
    return hashlib.sha1(f"{email.lower()}|{start_iso}".encode()).hexdigest()[:32]  # a-f0-9


def sign(eid: str, secret: str) -> str:
    mac = hmac.new(secret.encode(), f"cancel|{eid}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{eid}.{mac}"


def verify(token: str, secret: str) -> str | None:
    """The event id if the token is genuine, else None. Constant-time comparison."""
    if not secret or "." not in token or len(token) > 80:
        return None
    eid, _, mac = token.partition(".")
    if not EVENT_ID.fullmatch(eid):
        return None
    expected = sign(eid, secret).partition(".")[2]
    return eid if hmac.compare_digest(mac, expected) else None


def link(eid: str, secret: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/v1/cancel?t={sign(eid, secret)}"


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Cancel a call with Duc-Anh</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#1a1a1a}}
button{{font:inherit;padding:.6rem 1.2rem;border:0;border-radius:.5rem;background:#1a1a1a;color:#fff;cursor:pointer}}
p.s{{color:#555}}</style></head><body><h1>{title}</h1>{body}</body></html>"""
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"}


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body), status_code=status, headers=HEADERS)


def make_router(get_calendar, get_secret, limited) -> APIRouter:
    """get_calendar() -> Calendar, get_secret() -> str, limited(request) -> True if this client is over its rate limit."""
    router = APIRouter()

    async def handle(request: Request, token: str, do_cancel: bool) -> HTMLResponse:
        if limited(request):
            return page("Too many requests", "<p>Please wait a minute and try again.</p>", 429)
        eid = verify(token, get_secret())
        if not eid:
            return page("This link is not valid", "<p class='s'>It may be incomplete. Ask Twin in the chat for a new one.</p>", 404)
        cal = get_calendar()
        try:
            info = await cal.booking_info(eid)
            if info["status"] == "not_found":
                return page("Nothing to cancel", "<p>This call does not exist any more.</p>", 404)
            if info["status"] == "cancelled" or not do_cancel:
                label = html.escape(info.get("label", ""))
                if info["status"] == "cancelled":
                    return page("Already cancelled", f"<p>Your call on {label} is cancelled.</p>")
                return page("Cancel your call?", (
                    f"<p>Your call with Duc-Anh on <b>{label}</b>.</p><form method='post' action='/v1/cancel'>"
                    f"<input type='hidden' name='t' value='{html.escape(token)}'>"
                    "<button type='submit'>Cancel this call</button></form>"
                    "<p class='s'>The time becomes free again. You can book another from the chat.</p>"))
            res = await cal.cancel_id(eid)
        except Exception:  # noqa: BLE001 - never show internals
            return page("Something went wrong", "<p>Please try again in a minute, or email Duc-Anh.</p>", 502)
        if res["status"] in ("cancelled", "already_cancelled"):
            return page("Cancelled", f"<p>Your call on {html.escape(info.get('label', ''))} is cancelled.</p>")
        return page("Something went wrong", "<p>The call could not be cancelled. Please email Duc-Anh.</p>", 502)

    @router.get("/v1/cancel")
    async def show(request: Request, t: str = ""):
        return await handle(request, t, do_cancel=False)

    @router.post("/v1/cancel")
    async def do(request: Request):
        raw = (await request.body())[:2000].decode("utf-8", "replace")
        return await handle(request, parse_qs(raw).get("t", [""])[0], do_cancel=True)

    return router
