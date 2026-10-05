"""Booking: MCP client + the booking state machine.

States: collecting -> choosing -> proposed -> (confirmed -> created | failed) | cancelled.
Nothing is created without an explicit confirmation turn.
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import structlog
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel

log = structlog.get_logger()
EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}")
YES = re.compile(r"^\s*(y|yes|yep|yeah|ok|okay|sure|confirm|confirmed|book it|ja|klar|passt)\b", re.IGNORECASE)

MAX_BOOKINGS_PER_DAY = 5
MAX_MESSAGES_PER_DAY = 20


class BookingFields(BaseModel):
    name: str | None = None
    email: str | None = None
    topic: str | None = None
    slot_choice: int | None = None  # 1-based index into the slots that were shown
    minutes: int | None = None  # length asked for a new meeting: 30, 60 or 90
    extend_to_minutes: int | None = None  # 60 or 90 when they ask to lengthen an existing booked meeting
    cancel: bool | None = False  # models sometimes emit null


class LeaveFields(BaseModel):
    email: str | None = None
    message: str | None = None  # the visitor's own words, verbatim
    name: str | None = None
    cancel: bool | None = False


class Calendar:
    """Thin wrapper over the MCP stdio server. Tools are discovered, not hard-coded."""

    def __init__(self, store=None) -> None:
        self.store = store  # Firestore-backed daily counters and audit trail (optional)
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "GOOGLE_CALENDAR_TOKEN")}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])  # so `-m app.calendar_mcp` resolves
        if "GOOGLE_CALENDAR_TOKEN" not in env and Path("token_calendar.json").exists():
            env["GOOGLE_CALENDAR_TOKEN"] = Path("token_calendar.json").read_text()
        self.client = MultiServerMCPClient({"calendar": {
            "command": sys.executable, "args": ["-m", "app.calendar_mcp"],
            "transport": "stdio", "env": env,
        }})
        self.mem = {"bookings": 0, "messages": 0}  # per-process fallback when the store is off
        self.sent_hashes: set[str] = set()  # in-process dedupe: a retry never sends twice
        self.enabled = "GOOGLE_CALENDAR_TOKEN" in env

    async def _today(self, name: str) -> int:
        n = await self.store.get_count(name) if self.store else None
        return self.mem[name] if n is None else n

    async def _record(self, name: str, kind: str, status: str) -> None:
        if status in ("created", "sent"):
            self.mem[name] += 1
            if self.store:
                await self.store.bump(name)
        if self.store:
            await self.store.audit(kind, status)

    async def _call(self, name: str, args: dict) -> dict | list:
        tools = {t.name: t for t in await self.client.get_tools()}
        out = await tools[name].ainvoke(args)
        text = out if isinstance(out, str) else "".join(b.get("text", "") for b in out)
        return json.loads(text)

    async def free_slots(self) -> list[dict]:
        return await self._call("get_free_slots", {})  # type: ignore[return-value]

    async def allowance(self, email: str) -> dict:
        return await self._call("get_allowance", {"email": email})  # type: ignore[return-value]

    async def create(self, name: str, email: str, topic: str, start: str, slots: int = 1) -> dict:
        if await self._today("bookings") >= MAX_BOOKINGS_PER_DAY:
            return {"status": "daily_cap"}
        res = await self._call("create_booking", {
            "name": name, "email": email, "topic": topic, "start": start, "slots": slots})
        await self._record("bookings", "booking", res["status"])  # type: ignore[index]
        return res  # type: ignore[return-value]

    async def extension_limit(self, email: str, start: str) -> dict:
        return await self._call("extension_limit", {"email": email, "start": start})  # type: ignore[return-value]

    async def extend(self, email: str, start: str, total_slots: int) -> dict:
        res = await self._call("extend_booking", {"email": email, "start": start, "total_slots": total_slots})
        await self._record("bookings", "extend", res["status"])  # type: ignore[index]
        return res  # type: ignore[return-value]

    async def send_message(self, email: str, message: str, name: str = "") -> dict:
        key = hashlib.sha256(f"{email.lower()}|{message}".encode()).hexdigest()
        if key in self.sent_hashes:
            return {"status": "already_sent"}
        if await self._today("messages") >= MAX_MESSAGES_PER_DAY:
            return {"status": "daily_cap"}
        res = await self._call("send_message", {"sender_email": email, "message": message, "name": name})
        if res["status"] == "sent":  # type: ignore[index]
            self.sent_hashes.add(key)
        await self._record("messages", "message", res["status"])  # type: ignore[index]
        return res  # type: ignore[return-value]
