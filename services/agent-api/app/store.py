"""Firestore persistence. Every method is best-effort: storage must never break a conversation.

Schema (schema_version 1), see docs/SCHEMA.md:
  turns/{trace_id}   one document per answered turn (question, answer, sources, cost, feedback, judge)
  audit/{auto}       one document per booking or message attempt (kind, status): no PII
  counters/{date}    durable daily caps (bookings, messages) and the day's model spend (spend_usd)
  counters/{month}   the month's model spend (spend_usd)
  ipdays/{day_hash}  turns per visitor and day; the key is a salted daily hash, never the address
All collections carry expire_at (a Firestore TTL policy deletes them after 30 days).
"""

import datetime as dt

import structlog

from .config import settings

log = structlog.get_logger()
SCHEMA_VERSION = 1
RETENTION = dt.timedelta(days=30)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Store:
    def __init__(self) -> None:
        self.enabled = settings.firestore_enabled
        self._db = None

    def db(self):
        if self._db is None:
            from google.cloud import firestore
            self._db = firestore.AsyncClient(project=settings.gcp_project)
        return self._db

    async def save_turn(self, trace_id: str, doc: dict) -> None:
        if not self.enabled:
            return
        try:
            await self.db().collection("turns").document(trace_id).set({
                **doc, "schema_version": SCHEMA_VERSION, "ts": _now(), "expire_at": _now() + RETENTION,
                "feedback": None, "judge": None,
                "judge_pending": bool(doc.get("intent") == "question" and not doc.get("degraded")
                                      and doc.get("sources"))})
        except Exception as e:  # noqa: BLE001
            log.warning("store_failed", op="save_turn", error=str(e)[:200])

    async def set_feedback(self, trace_id: str, value: int) -> bool:
        if not self.enabled:
            return False
        try:
            ref = self.db().collection("turns").document(trace_id)
            if not (await ref.get()).exists:
                return False
            await ref.update({"feedback": value, "feedback_ts": _now()})
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("store_failed", op="set_feedback", error=str(e)[:200])
            return False

    async def audit(self, kind: str, status: str) -> None:
        if not self.enabled:
            return
        try:
            await self.db().collection("audit").add({
                "kind": kind, "status": status, "ts": _now(), "expire_at": _now() + RETENTION,
                "schema_version": SCHEMA_VERSION})
        except Exception as e:  # noqa: BLE001
            log.warning("store_failed", op="audit", error=str(e)[:200])

    async def get_count(self, name: str) -> int | None:
        """Today's counter, or None if unavailable (callers fall back to their in-memory count)."""
        if not self.enabled:
            return None
        try:
            snap = await self.db().collection("counters").document(_now().strftime("%Y-%m-%d")).get()
            return int((snap.to_dict() or {}).get(name, 0))
        except Exception as e:  # noqa: BLE001
            log.warning("store_failed", op="get_count", error=str(e)[:200])
            return None

    async def bump(self, name: str) -> None:
        if not self.enabled:
            return
        try:
            from google.cloud import firestore
            await self.db().collection("counters").document(_now().strftime("%Y-%m-%d")).set(
                {name: firestore.Increment(1), "expire_at": _now() + RETENTION}, merge=True)
        except Exception as e:  # noqa: BLE001
            log.warning("store_failed", op="bump", error=str(e)[:200])

    async def get_spend(self) -> tuple[float, float] | None:
        """(today's, this month's) model spend in USD as persisted by all instances, or None if unavailable."""
        if not self.enabled:
            return None
        try:
            now = _now()
            day = await self.db().collection("counters").document(now.strftime("%Y-%m-%d")).get()
            month = await self.db().collection("counters").document(now.strftime("%Y-%m")).get()
            return (float((day.to_dict() or {}).get("spend_usd", 0.0)),
                    float((month.to_dict() or {}).get("spend_usd", 0.0)))
        except Exception as e:  # noqa: BLE001
            log.warning("store_failed", op="get_spend", error=str(e)[:200])
            return None

    async def add_spend(self, usd: float) -> bool:
        if not self.enabled:
            return True
        try:
            from google.cloud import firestore
            now = _now()
            await self.db().collection("counters").document(now.strftime("%Y-%m-%d")).set(
                {"spend_usd": firestore.Increment(usd), "expire_at": now + RETENTION}, merge=True)
            await self.db().collection("counters").document(now.strftime("%Y-%m")).set(
                {"spend_usd": firestore.Increment(usd), "expire_at": now + 2 * RETENTION}, merge=True)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("store_failed", op="add_spend", error=str(e)[:200])
            return False

    async def get_turns(self) -> int | None:
        """Turns served today by all instances, or None if unavailable."""
        if not self.enabled:
            return None
        try:
            snap = await self.db().collection("counters").document(_now().strftime("%Y-%m-%d")).get()
            return int((snap.to_dict() or {}).get("turns", 0))
        except Exception as e:  # noqa: BLE001
            log.warning("store_failed", op="get_turns", error=str(e)[:200])
            return None

    async def add_turns(self, n: int) -> bool:
        if not self.enabled:
            return True
        try:
            from google.cloud import firestore
            now = _now()
            await self.db().collection("counters").document(now.strftime("%Y-%m-%d")).set(
                {"turns": firestore.Increment(n), "expire_at": now + RETENTION}, merge=True)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("store_failed", op="add_turns", error=str(e)[:200])
            return False

    async def get_ip_turns(self, key: str) -> int | None:
        if not self.enabled:
            return None
        try:
            snap = await self.db().collection("ipdays").document(key).get()
            return int((snap.to_dict() or {}).get("n", 0))
        except Exception as e:  # noqa: BLE001
            log.warning("store_failed", op="get_ip_turns", error=str(e)[:200])
            return None

    async def bump_ip(self, key: str) -> None:
        if not self.enabled:
            return
        try:
            from google.cloud import firestore
            await self.db().collection("ipdays").document(key).set(
                {"n": firestore.Increment(1), "expire_at": _now() + dt.timedelta(days=2)}, merge=True)
        except Exception as e:  # noqa: BLE001
            log.warning("store_failed", op="bump_ip", error=str(e)[:200])

    async def pending_judgments(self, limit: int) -> list[tuple[str, dict]]:
        q = (self.db().collection("turns").where("judge_pending", "==", True)
             .order_by("ts", direction="DESCENDING").limit(limit))
        return [(d.id, d.to_dict()) async for d in q.stream()]

    async def set_judge(self, trace_id: str, judge: dict) -> None:
        await self.db().collection("turns").document(trace_id).update(
            {"judge": judge, "judge_pending": False, "judge_ts": _now()})
