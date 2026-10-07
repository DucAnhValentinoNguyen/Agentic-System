"""Hard stop: when the project's cost reaches the limit, detach billing from the project.

A budget (infra/terraform/guard.tf) publishes its current cost to a Pub/Sub topic every few minutes. A push subscription
delivers each message here. If it is the guard budget's message and the cost has reached the budget amount, the project is
unlinked from its billing account: paid services stop, nothing more can be charged, and no credential, however stolen, can
spend further. The price is that the service goes down until billing is linked again (documented in README).

It acts on exactly one budget (matched by display name), only at or above the limit, never twice (idempotent), and has a
dry-run mode for testing. It answers 200 to anything it chooses to ignore so Pub/Sub does not redeliver it; it answers 500
only when the detach itself failed, so that Pub/Sub retries.
"""

import base64
import json
import os

import google.auth
import google.auth.transport.requests
import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = structlog.get_logger()
structlog.configure(processors=[structlog.processors.add_log_level,
                                structlog.processors.EventRenamer("message"),
                                structlog.processors.JSONRenderer()])

PROJECT = os.environ.get("GCP_PROJECT", "")
BUDGET_NAME = os.environ.get("GUARD_BUDGET_NAME", "")
DRY_RUN = os.environ.get("GUARD_DRY_RUN", "true").lower() != "false"  # safe by default
API = "https://cloudbilling.googleapis.com/v1"

app = FastAPI(title="Twin billing guard")


def decide(note: dict, budget_name: str) -> tuple[bool, str]:
    """(stop?, reason). Pure, so it can be tested without any cloud."""
    if not budget_name or note.get("budgetDisplayName") != budget_name:
        return False, "not the guard budget"
    try:
        cost, limit = float(note["costAmount"]), float(note["budgetAmount"])
    except (KeyError, TypeError, ValueError):
        return False, "message has no usable cost and budget amounts"
    if limit <= 0:
        return False, "budget amount is not positive"
    if cost < limit:
        return False, f"cost {cost:.2f} is below the limit {limit:.2f}"
    return True, f"cost {cost:.2f} has reached the limit {limit:.2f}"


def token() -> str:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


async def detach(project: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Unlink the project from its billing account. Raises if it is still linked afterwards.

    There is deliberately no "read the current state first" call: the guard's identity may detach but may not read
    (a read returned 403 in testing), and a failing read would have stopped it from ever detaching. The call is
    idempotent: detaching an already detached project returns the same result.
    """
    async with httpx.AsyncClient(timeout=30, transport=transport) as c:
        r = await c.put(f"{API}/projects/{project}/billingInfo", headers={"Authorization": f"Bearer {token()}"},
                        json={"billingAccountName": ""})
    r.raise_for_status()
    if r.json().get("billingEnabled"):
        raise RuntimeError("billing is still enabled after the detach call")
    return "detached"


@app.post("/")
async def receive(request: Request):
    try:
        envelope = await request.json()
        note = json.loads(base64.b64decode(envelope["message"]["data"]))
    except Exception:  # noqa: BLE001 - not a Pub/Sub budget message: nothing to retry
        log.warning("guard_ignored", reason="unreadable message")
        return JSONResponse({"ok": True, "action": "ignored"})
    stop, reason = decide(note, BUDGET_NAME)
    log.info("guard_checked", stop=stop, reason=reason, dry_run=DRY_RUN, cost=note.get("costAmount"),
             limit=note.get("budgetAmount"))
    if not stop:
        return JSONResponse({"ok": True, "action": "none"})
    if DRY_RUN:
        log.error("guard_would_detach_billing", reason=reason, project=PROJECT)
        return JSONResponse({"ok": True, "action": "dry_run"})
    try:
        result = await detach(PROJECT)
    except Exception as e:  # noqa: BLE001
        log.error("guard_detach_failed", error=str(e)[:300] or type(e).__name__, project=PROJECT)
        return JSONResponse({"ok": False}, status_code=500)  # Pub/Sub retries
    log.error("guard_billing_detached", result=result, reason=reason, project=PROJECT)
    return JSONResponse({"ok": True, "action": result})
