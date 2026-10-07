"""Limits that bound what a visitor can cost: shared daily capacity, idle connections, connections per address."""

import time
from collections import defaultdict

import pytest
from app import main
from app.config import settings
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


class Store:
    """Firestore stand-in: one shared counter, like the real counters document."""

    def __init__(self, turns=0):
        self.turns = turns

    async def add_turns(self, n):
        self.turns += n
        return True

    async def get_turns(self):
        return self.turns

    async def get_ip_turns(self, key):
        return 0

    async def bump_ip(self, key):
        return None


@pytest.fixture
def client(monkeypatch):
    s = main.app.state
    s.store, s.hits, s.turns, s.done_turns = Store(), defaultdict(), defaultdict(int), {}
    s.ip_day, s.conns, s.tasks = {}, defaultdict(int), set()
    s.turns_shared, s.turns_pending = 0, 0
    s.hits = defaultdict(__import__("collections").deque)
    return TestClient(main.app)


def connect(c):
    return c.websocket_connect("/ws/chat", headers={"origin": main.ORIGINS[0]})


def send(ws, text="hello there", turn="t1"):
    ws.send_json({"session_id": "s" * 8, "turn_id": turn, "text": text})
    return ws.receive_json()


def test_when_the_shared_daily_capacity_is_used_up_a_turn_is_refused_and_nothing_runs(client, monkeypatch):
    monkeypatch.setattr(settings, "daily_turns_total", 3)
    main.app.state.store.turns = 3                                  # three turns already served today, by any instance
    asyncio_run(main.sync_turns())
    with connect(client) as ws:
        ev = send(ws)
    assert ev["type"] == "error" and ev["code"] == "daily_capacity"


def test_an_admitted_turn_counts_towards_the_shared_total(client, monkeypatch):
    monkeypatch.setattr(settings, "daily_turns_total", 2)
    main.app.state.turns_pending = 1
    assert not main.capacity_reached()
    main.app.state.turns_pending = 2
    assert main.capacity_reached()                                  # pending turns count before they are written
    asyncio_run(main.sync_turns())
    assert main.app.state.store.turns == 2 and main.app.state.turns_pending == 0 and main.capacity_reached()


def test_a_second_instance_sees_what_the_first_one_served(client, monkeypatch):
    monkeypatch.setattr(settings, "daily_turns_total", 5)
    main.app.state.turns_pending = 3
    asyncio_run(main.sync_turns())                                  # instance A writes its 3
    main.app.state.turns_shared, main.app.state.turns_pending = 0, 2   # instance B knows only its own 2
    asyncio_run(main.sync_turns())
    assert main.app.state.turns_shared == 5 and main.capacity_reached()


def test_an_idle_connection_is_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "idle_timeout_s", 0.3)
    with connect(client) as ws:
        t0 = time.monotonic()
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()                                       # the server hangs up; we never sent anything
        assert time.monotonic() - t0 < 5
    assert not main.app.state.conns                                 # and the slot is released


def test_too_many_connections_from_one_address_are_refused(client, monkeypatch):
    monkeypatch.setattr(settings, "max_connections_per_ip", 2)
    with connect(client), connect(client), pytest.raises(WebSocketDisconnect), connect(client) as third:
        third.receive_json()
    assert not main.app.state.conns                                 # all released when they close


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
