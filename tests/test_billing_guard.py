"""The hard stop acts only on its own budget, only at the limit, and never on garbage."""

import base64
import json

import pytest
from app import billing_guard as g
from fastapi.testclient import TestClient

NAME = "Twin hard stop"


def msg(**kw):
    note = {"budgetDisplayName": NAME, "costAmount": 10.0, "budgetAmount": 200.0, "currencyCode": "EUR", **kw}
    return {"message": {"data": base64.b64encode(json.dumps(note).encode()).decode()}, "subscription": "s"}


def test_decide_acts_only_at_or_above_the_limit_of_its_own_budget():
    assert g.decide({"budgetDisplayName": NAME, "costAmount": 199.99, "budgetAmount": 200}, NAME)[0] is False
    assert g.decide({"budgetDisplayName": NAME, "costAmount": 200, "budgetAmount": 200}, NAME)[0] is True
    assert g.decide({"budgetDisplayName": NAME, "costAmount": 450.5, "budgetAmount": 200}, NAME)[0] is True
    assert g.decide({"budgetDisplayName": "Another budget", "costAmount": 999, "budgetAmount": 200}, NAME)[0] is False
    assert g.decide({"budgetDisplayName": NAME, "costAmount": 999, "budgetAmount": 200}, "")[0] is False     # no name configured
    for bad in ({}, {"budgetDisplayName": NAME}, {"budgetDisplayName": NAME, "costAmount": "x", "budgetAmount": 200},
                {"budgetDisplayName": NAME, "costAmount": 5, "budgetAmount": 0},
                {"budgetDisplayName": NAME, "costAmount": None, "budgetAmount": 200}):
        assert g.decide(bad, NAME)[0] is False


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def fake_detach(project):
        seen.append(project)
        return "detached"
    monkeypatch.setattr(g, "BUDGET_NAME", NAME)
    monkeypatch.setattr(g, "PROJECT", "the-project")
    monkeypatch.setattr(g, "detach", fake_detach)
    return seen


def test_below_the_limit_nothing_happens(calls, monkeypatch):
    monkeypatch.setattr(g, "DRY_RUN", False)
    r = TestClient(g.app).post("/", json=msg(costAmount=50.0))
    assert r.status_code == 200 and r.json()["action"] == "none" and calls == []


def test_at_the_limit_billing_is_detached_unless_it_is_a_dry_run(calls, monkeypatch):
    monkeypatch.setattr(g, "DRY_RUN", True)
    assert TestClient(g.app).post("/", json=msg(costAmount=200.0)).json()["action"] == "dry_run" and calls == []
    monkeypatch.setattr(g, "DRY_RUN", False)
    assert TestClient(g.app).post("/", json=msg(costAmount=200.0)).json()["action"] == "detached"
    assert calls == ["the-project"]


def test_another_budgets_message_is_ignored_even_when_it_is_huge(calls, monkeypatch):
    monkeypatch.setattr(g, "DRY_RUN", False)
    r = TestClient(g.app).post("/", json=msg(budgetDisplayName="Twin (AgentSystems) monthly budget", costAmount=9999.0))
    assert r.json()["action"] == "none" and calls == []


def test_garbage_is_ignored_with_200_so_it_is_not_redelivered(calls, monkeypatch):
    monkeypatch.setattr(g, "DRY_RUN", False)
    c = TestClient(g.app)
    for body in ({}, {"message": {}}, {"message": {"data": "!!!not base64!!!"}},
                 {"message": {"data": base64.b64encode(b"not json").decode()}}):
        r = c.post("/", json=body)
        assert r.status_code == 200 and r.json()["action"] == "ignored"
    assert calls == []


def test_a_failed_detach_returns_500_so_pubsub_retries(monkeypatch):
    async def boom(project):
        raise RuntimeError("api down")
    monkeypatch.setattr(g, "BUDGET_NAME", NAME)
    monkeypatch.setattr(g, "DRY_RUN", False)
    monkeypatch.setattr(g, "detach", boom)
    assert TestClient(g.app).post("/", json=msg(costAmount=300.0)).status_code == 500


def test_the_default_is_dry_run():
    import importlib
    import os
    os.environ.pop("GUARD_DRY_RUN", None)
    assert importlib.reload(g).DRY_RUN is True
