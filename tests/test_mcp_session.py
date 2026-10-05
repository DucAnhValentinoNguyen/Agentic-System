"""The calendar client keeps one long-lived MCP session and restarts it only if it looks dead."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from app import booking as bk


class FakeTool:
    def __init__(self, name, impl):
        self.name, self.impl = name, impl

    async def ainvoke(self, args):
        return self.impl(args)


class FakeClient:
    def __init__(self, impl):
        self.sessions_opened, self.impl = 0, impl

    @asynccontextmanager
    async def session(self, name):
        self.sessions_opened += 1
        yield SimpleNamespace(tools=[FakeTool("get_free_slots", self.impl)])


def make(impl, monkeypatch):
    client = FakeClient(impl)

    async def fake_load(session):
        return session.tools
    monkeypatch.setattr(bk, "load_mcp_tools", fake_load)
    cal = bk.Calendar.__new__(bk.Calendar)
    cal.client, cal._queue, cal._task = client, asyncio.Queue(), None
    return cal, client


async def test_many_calls_share_one_session(monkeypatch):
    cal, client = make(lambda a: json.dumps([{"start": "x"}]), monkeypatch)
    for _ in range(5):
        assert await cal._call("get_free_slots", {}) == [{"start": "x"}]
    assert client.sessions_opened == 1            # not one process per call
    await cal.close()


async def test_text_content_blocks_are_parsed_too(monkeypatch):
    cal, _ = make(lambda a: [{"type": "text", "text": json.dumps({"status": "ok"})}], monkeypatch)
    assert await cal._call("get_free_slots", {}) == {"status": "ok"}
    await cal.close()


async def test_a_tool_error_reaches_the_caller_without_killing_the_session(monkeypatch):
    calls = {"n": 0}

    def impl(a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("calendar hiccup")
        return json.dumps({"status": "ok"})
    cal, client = make(impl, monkeypatch)
    with pytest.raises(RuntimeError):
        await cal._call("get_free_slots", {})
    assert await cal._call("get_free_slots", {}) == {"status": "ok"}
    assert client.sessions_opened == 1
    await cal.close()


async def test_two_failures_in_a_row_restart_the_session(monkeypatch):
    state = {"broken": True}

    def impl(a):
        if state["broken"]:
            raise RuntimeError("pipe closed")
        return json.dumps({"status": "ok"})
    cal, client = make(impl, monkeypatch)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cal._call("get_free_slots", {})
    state["broken"] = False
    assert await cal._call("get_free_slots", {}) == {"status": "ok"}
    assert client.sessions_opened == 2            # a fresh session replaced the dead one
    await cal.close()
