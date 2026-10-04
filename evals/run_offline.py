"""Offline A/B experiment: does claim verification (variant B) reduce unsupported claims vs plain RAG (A)?

Runs the real LangGraph in-process on evals/datasets/facts.jsonl, then scores each answer with an
LLM judge from a different model family (the answerer is Gemini, the judge is gpt-oss-120b on Groq).
Reports paired bootstrap 95% CIs. Hypothesis was fixed before building; see docs/experiment.md.

  uv run python evals/run_offline.py --split heldout
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-api"))

import re

from app import judge as judge_mod
from app.gateway.router import build_router
from app.graph.build import build_graph
from app.retrieval import Index
from app.retrieval_client import LocalRetriever

ABSTAIN = re.compile(r"don't have|do not have|do(?:es)? not (?:yet )?(?:contain|have)|doesn't contain|no information|"
                     r"not (?:on|in) the (?:site|sources)|not mentioned|isn't (?:listed|mentioned)", re.IGNORECASE)


class NoCalendar:
    enabled = False


# A = plain RAG, B = + claim verification, R = research mode (plan, parallel search, reflect)
VARIANTS = {"A": {"variant": "A", "research": "off"}, "B": {"variant": "B", "research": "off"},
            "R": {"variant": "A", "research": "force"}}


async def run_case(graph, case: dict, variant: str) -> dict:
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    t0, ttft, final, steps = time.monotonic(), None, {}, 0
    async for mode, ev in graph.astream(
        {"question": case["question"], "records": [], **VARIANTS[variant]}, cfg,
        stream_mode=["custom", "updates"],
    ):
        if mode == "custom" and ev["type"] == "step":
            steps += 1
        if mode == "custom" and ev["type"] == "delta" and ttft is None:
            ttft = time.monotonic() - t0
        elif mode == "updates":
            for upd in ev.values():
                for k, v in (upd or {}).items():
                    if k == "records":
                        final.setdefault("records", []).extend(v)
                    else:
                        final[k] = v
    recs = final.get("records") or []
    return {
        "answer": final.get("answer", ""), "chunks": final.get("chunks") or [],
        "verify": final.get("verify"), "steps": steps, "latency_s": time.monotonic() - t0, "ttft_s": ttft,
        "cost_usd": sum(r.cost_usd for r in recs),
        "providers": sorted({r.provider for r in recs if not r.error}),
        "degraded": bool(final.get("degraded")),
    }


def boot_ci(a: list[float], b: list[float], n: int = 10000, seed: int = 1) -> tuple[float, float, float]:
    """Paired bootstrap over cases: mean(b - a) with a 95% CI."""
    d = [y - x for x, y in zip(a, b)]
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(d, k=len(d))) for _ in range(n))
    return statistics.fmean(d), means[int(0.025 * n)], means[int(0.975 * n)]


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="heldout", choices=["dev", "heldout", "all"])
    ap.add_argument("--variants", default="A,B")
    ap.add_argument("--pause", type=float, default=0.0, help="seconds to wait after each run (quota)")
    ap.add_argument("--dataset", default="facts", choices=["facts", "multihop"])
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--out", default=None)
    ap.add_argument("--reuse-judge", action="store_true", help="with --rejudge: keep saved judge output")
    ap.add_argument("--rejudge", default=None, help="re-score saved answers from this results file")
    a = ap.parse_args()

    cases = [json.loads(line) for line in (ROOT / f"evals/datasets/{a.dataset}.jsonl").read_text().splitlines()]
    if a.split != "all":
        cases = [c for c in cases if c["split"] == a.split]
    router, index = build_router(), Index.load()
    vertex = router.providers[0]
    await index.warm(await vertex.token())
    graph = build_graph(router, LocalRetriever(index, vertex.token), NoCalendar())
    jclient = judge_mod.client()
    sem = asyncio.Semaphore(a.concurrency)
    variants = a.variants.split(",")

    async def one(case, v):
        async with sem:
            r = await run_case(graph, case, v)
            await asyncio.sleep(a.pause)
            r["judge"] = await judge_mod.judge(
                jclient, case["question"], r["answer"], r["chunks"], case["golden"], case["answerable"])
            r.update(id=case["id"], variant=v, answerable=case["answerable"], split=case["split"])
            g = case["golden"]
            r["golden_frac"] = (r["judge"].get("golden_covered", 0) / len(g)) if g else None
            gold = set(case.get("gold_anchors") or [])
            r["recall"] = (len(gold & {c["anchor"] for c in r["chunks"]}) / len(gold)) if gold else None
            r["chunks"] = [c["anchor"] for c in r["chunks"]]
            print(f"{v} {case['id']:<18} claims={r['judge']['claims']} unsupported={r['judge']['unsupported']}"
                  f" {r['latency_s']:.1f}s", flush=True)
            return r

    if a.rejudge:
        saved = json.loads(Path(a.rejudge).read_text())
        by_id = {c["id"]: c for c in cases}
        chunk_text = {}
        for c in index.chunks:
            chunk_text.setdefault(c["anchor"], []).append(c)

        async def again(r):
            if a.reuse_judge:
                return r
            async with sem:
                chunks = [c for anc in r["chunks"] for c in chunk_text.get(anc, [])[:1]]
                c = by_id[r["id"]]
                r["judge"] = await judge_mod.judge(
                    jclient, c["question"], r["answer"], chunks, c["golden"], c["answerable"])
                print(f"{r['variant']} {r['id']:<18} claims={r['judge']['claims']} "
                      f"unsupported={r['judge']['unsupported']}", flush=True)
                return r

        rows = await asyncio.gather(*[again(r) for r in saved if r["id"] in by_id])
    else:
        rows = await asyncio.gather(*[one(c, v) for c in cases for v in variants])
    # Pairs where the judge failed for either variant are excluded from both, and reported.
    bad_ids = {r["id"] for r in rows if r["judge"].get("judge_failed") or r["degraded"]}
    rows = [r for r in rows if r["id"] not in bad_ids]
    cases = [c for c in cases if c["id"] not in bad_ids]
    if bad_ids:
        print(f"EXCLUDED (judge failed or answer degraded by provider errors): {sorted(bad_ids)}")
    by = {v: {r["id"]: r for r in rows if r["variant"] == v} for v in variants}

    def metric(v, fn, only=None):
        return [fn(r) for r in by[v].values() if only is None or r["answerable"] == only]

    print(f"\n== {a.split}: {len(cases)} cases ({sum(c['answerable'] for c in cases)} answerable)")
    for v in variants:
        rs = list(by[v].values())
        claims = sum(r["judge"]["claims"] for r in rs)
        uns = sum(r["judge"]["unsupported"] for r in rs)
        ans = [r for r in rs if r["answerable"]]
        una = [r for r in rs if not r["answerable"]]
        print(f"variant {v}: unsupported claims {uns}/{claims} ({100 * uns / max(claims, 1):.1f}%) | "
              f"answers with >=1 unsupported {sum(r['judge']['unsupported'] > 0 for r in rs)}/{len(rs)} | "
              f"golden coverage {sum(r['judge']['covers_golden'] for r in ans)}/{len(ans)} | "
              f"abstained on unanswerable {sum(bool(ABSTAIN.search(r['answer'])) for r in una)}/{len(una)} (phrase check; "
              f"judge says {sum(r['judge']['abstained'] for r in una)})")
        if rs and rs[0].get("recall") is not None:
            print(f"          evidence recall (gold sections retrieved) "
                  f"{statistics.fmean(r['recall'] for r in rs):.2f} | mean steps "
                  f"{statistics.fmean(r.get('steps', 0) for r in rs):.1f}")
        lat = [r["latency_s"] for r in rs]
        print(f"          latency p50 {pct(lat, .5):.2f}s p95 {pct(lat, .95):.2f}s | "
              f"cost/answer ${statistics.fmean(r['cost_usd'] for r in rs):.5f} | "
              f"degraded {sum(r['degraded'] for r in rs)}")
    if len(variants) == 2:
        v0, v1 = variants
        ids = sorted(by[v0])
        for name, fn in [("unsupported claims per answer", lambda r: r["judge"]["unsupported"]),
                         ("any unsupported (0/1)", lambda r: float(r["judge"]["unsupported"] > 0)),
                         ("golden coverage (0/1, answerable)", lambda r: float(r["judge"]["covers_golden"])),
                         ("evidence recall", lambda r: r.get("recall") or 0.0),
                         ("golden facts covered (fraction)", lambda r: r.get("golden_frac") or 0.0),
                         ("latency (s)", lambda r: r["latency_s"]),
                         ("cost per answer (USD)", lambda r: r["cost_usd"])]:
            sel = [i for i in ids if name.endswith("answerable)") is False or by[v0][i]["answerable"]]
            d, lo, hi = boot_ci([fn(by[v0][i]) for i in sel], [fn(by[v1][i]) for i in sel])
            print(f"  {v1}-{v0} {name}: {d:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]")
    out = Path(a.out or ROOT / f"evals/results/offline_{a.split}.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, indent=1, default=str))
    print("saved", out)


asyncio.run(main())
