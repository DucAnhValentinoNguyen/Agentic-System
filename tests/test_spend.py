"""The daily and monthly spend caps are shared and durable; a restart must not reset them."""

import asyncio

from app.config import settings
from app.gateway.router import Router


class FakeStore:
    """Stands in for the Firestore counters of one project, shared by every Router that uses it."""

    def __init__(self):
        self.day, self.month = 0.0, 0.0

    async def add_spend(self, usd):
        self.day += usd
        self.month += usd
        return True

    async def get_spend(self):
        return self.day, self.month


def spend(router, usd):
    router.spent_today_usd += usd
    router.pending_usd += usd


def test_a_restart_does_not_reset_the_days_spend(monkeypatch):
    monkeypatch.setattr(settings, "daily_budget_usd", 2.0)
    store = FakeStore()
    first = Router([])
    spend(first, 2.5)
    asyncio.run(first.sync_spend(store))
    assert first.over_budget()
    restarted = Router([])                              # new process, memory empty
    assert not restarted.over_budget()                  # before it reads the shared total...
    asyncio.run(restarted.sync_spend(store))
    assert restarted.over_budget()                      # ...and blocked once it has


def test_two_instances_share_one_budget(monkeypatch):
    monkeypatch.setattr(settings, "daily_budget_usd", 2.0)
    store, a, b = FakeStore(), Router([]), Router([])
    spend(a, 1.2)
    spend(b, 1.2)
    asyncio.run(a.sync_spend(store))
    asyncio.run(b.sync_spend(store))
    assert b.over_budget() and not a.over_budget()      # A learns of B's spend at its next sync (every 10 s)
    asyncio.run(a.sync_spend(store))
    assert a.over_budget()                              # 2.4 in total, though each instance saw only 1.2


def test_the_monthly_cap_holds_even_when_the_day_is_cheap(monkeypatch):
    monkeypatch.setattr(settings, "daily_budget_usd", 2.0)
    monkeypatch.setattr(settings, "monthly_budget_usd", 12.0)
    store = FakeStore()
    store.month = 11.9
    r = Router([])
    spend(r, 0.2)
    asyncio.run(r.sync_spend(store))
    assert r.over_budget() and r.shared_day < 2.0


def test_a_failed_write_keeps_the_pending_spend(monkeypatch):
    class Down(FakeStore):
        async def add_spend(self, usd):
            return False
    r = Router([])
    spend(r, 0.3)
    asyncio.run(r.sync_spend(Down()))
    assert r.pending_usd == 0.3                          # retried on the next sync, not lost
