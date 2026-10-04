"""send_message MCP tool: MIME construction, validation, header-injection safety, dedupe."""

import base64
import email
import json

from app import calendar_mcp as m
from app.booking import Calendar


class FakeGmail:
    def __init__(self):
        self.sent = []

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        outer = self

        class Call:
            def execute(self):
                outer.sent.append(email.message_from_bytes(base64.urlsafe_b64decode(body["raw"])))
        return Call()


def test_sends_to_owner_with_reply_to(monkeypatch):
    g = FakeGmail()
    monkeypatch.setattr(m, "gmail_service", lambda: g)
    res = json.loads(m.send_message("visitor@example.com", "Hello, let's collaborate.", "Ann"))
    assert res["status"] == "sent"
    msg = g.sent[0]
    assert msg["To"] == m.OWNER_EMAIL and msg["Reply-To"] == "visitor@example.com"
    assert "let's collaborate" in msg.get_payload(decode=True).decode()


def test_rejects_invalid_input(monkeypatch):
    monkeypatch.setattr(m, "gmail_service", lambda: FakeGmail())
    assert json.loads(m.send_message("not-an-email", "hi"))["status"] == "invalid"
    assert json.loads(m.send_message("a@b.co", "   "))["status"] == "invalid"
    assert json.loads(m.send_message("a@b.co", "x" * 2001))["status"] == "invalid"


def test_header_injection_is_rejected(monkeypatch):
    g = FakeGmail()
    monkeypatch.setattr(m, "gmail_service", lambda: g)
    evil = "a@b.co\nBcc: attacker@evil.test"
    assert json.loads(m.send_message(evil, "hi"))["status"] == "invalid" and not g.sent


def test_gmail_failure_is_reported_not_retried(monkeypatch):
    class Boom(FakeGmail):
        def send(self, userId, body):
            raise RuntimeError("quota")
    monkeypatch.setattr(m, "gmail_service", lambda: Boom())
    assert json.loads(m.send_message("a@b.co", "hi"))["status"] == "failed"


async def test_client_dedupes_and_caps(monkeypatch):
    cal = Calendar.__new__(Calendar)
    cal.mem, cal.store, cal.sent_hashes, calls = {"bookings": 0, "messages": 0}, None, set(), []

    async def fake_call(name, args):
        calls.append(args)
        return {"status": "sent"}
    cal._call = fake_call
    assert (await cal.send_message("a@b.co", "hello"))["status"] == "sent"
    assert (await cal.send_message("a@b.co", "hello"))["status"] == "already_sent"
    assert len(calls) == 1
    cal.mem["messages"] = 20
    assert (await cal.send_message("c@d.co", "other"))["status"] == "daily_cap"


async def test_daily_cap_is_durable_when_store_has_a_count():
    class FakeStore:
        def __init__(self):
            self.n, self.audits = 20, []

        async def get_count(self, name):
            return self.n

        async def bump(self, name):
            self.n += 1

        async def audit(self, kind, status):
            self.audits.append((kind, status))

    cal = Calendar.__new__(Calendar)
    cal.mem, cal.sent_hashes, cal.store = {"bookings": 0, "messages": 0}, set(), FakeStore()
    cal._call = lambda *a, **k: None
    # the in-memory count is 0 (fresh process after a restart) but the durable count says 20
    assert (await cal.send_message("a@b.co", "hi"))["status"] == "daily_cap"
