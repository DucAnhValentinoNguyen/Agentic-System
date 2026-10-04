"""Concurrent WebSocket load test: N sessions x M turns. Reports TTFT/total latency and failures.

  uv run python evals/load_test.py --url wss://HOST/ws/chat --sessions 10 --turns 5
Raise RATE_PER_MINUTE on the target first (per-IP limit), and restore it afterwards.
"""

import argparse
import asyncio
import json
import random
import time
import uuid
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = [json.loads(line)["question"] for line in
             (ROOT / "evals/datasets/facts.jsonl").read_text().splitlines()]


async def session(url: str, turns: int, out: list[dict]) -> None:
    sid, rng = f"load-{uuid.uuid4().hex[:10]}", random.Random()
    try:
        async with websockets.connect(url, origin="https://ducanhvalentinonguyen.com") as ws:
            for _ in range(turns):
                t0, first = time.monotonic(), None
                await ws.send(json.dumps({"session_id": sid, "turn_id": uuid.uuid4().hex,
                                          "text": rng.choice(QUESTIONS)}))
                while True:
                    ev = json.loads(await asyncio.wait_for(ws.recv(), 60))
                    if ev["type"] == "delta" and first is None:
                        first = time.monotonic() - t0
                    if ev["type"] in ("done", "error"):
                        out.append({"ttft": first, "total": time.monotonic() - t0,
                                    "ok": ev["type"] == "done", "degraded": bool(ev.get("degraded")),
                                    "err": ev.get("code")})
                        break
                await asyncio.sleep(rng.uniform(0.5, 2))  # think time
    except Exception as e:  # noqa: BLE001
        out.append({"ttft": None, "total": 0, "ok": False, "degraded": False, "err": type(e).__name__})


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--sessions", type=int, default=10)
    ap.add_argument("--turns", type=int, default=5)
    a = ap.parse_args()
    out: list[dict] = []
    t0 = time.monotonic()
    await asyncio.gather(*[session(a.url, a.turns, out) for _ in range(a.sessions)])
    wall = time.monotonic() - t0
    ok = [r for r in out if r["ok"]]
    ttft = [r["ttft"] for r in ok if r["ttft"] is not None]
    print(f"{a.sessions} concurrent sessions x {a.turns} turns = {len(out)} turns in {wall:.0f}s "
          f"({len(out) / wall:.2f} turns/s)")
    print(f"ok {len(ok)}/{len(out)} | degraded {sum(r['degraded'] for r in ok)} | "
          f"errors { {r['err'] for r in out if not r['ok']} or 'none' }")
    print(f"TTFT   p50 {pct(ttft, .5):.2f}s  p95 {pct(ttft, .95):.2f}s  max {max(ttft):.2f}s")
    tot = [r["total"] for r in ok]
    print(f"total  p50 {pct(tot, .5):.2f}s  p95 {pct(tot, .95):.2f}s  max {max(tot):.2f}s")


asyncio.run(main())
