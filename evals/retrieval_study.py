"""Retrieval-only study: does research mode (plan, parallel search, reflect) retrieve the right sections?

Runs the real graph but stops before the `answer` node (`interrupt_before`), so there is no answer
generation and no judge: only evidence recall (share of the gold sections that were retrieved) is
measured, deterministically, with repeats to expose run-to-run variance from the LLM-written queries.

  uv run python evals/retrieval_study.py --reps 3
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-api"))

from app.gateway.router import build_router
from app.graph.build import build_graph
from app.retrieval import Index
from app.retrieval_client import LocalRetriever


class NoCalendar:
    enabled = False


async def evidence(graph, question: str, research: str) -> tuple[list[dict], str]:
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    final: dict = {}
    async for ev in graph.astream({"question": question, "records": [], "variant": "A", "research": research},
                                  cfg, stream_mode="updates", interrupt_before=["answer"]):
        for upd in ev.values():
            final.update(upd if isinstance(upd, dict) else {})
    return final.get("chunks") or [], final.get("search_query", "")


def recall(chunks: list[dict], gold: set[str]) -> float:
    return len(gold & {c["anchor"] for c in chunks}) / len(gold)


def boot(a: list[float], b: list[float], n: int = 10000) -> tuple[float, float, float]:
    d = [y - x for x, y in zip(a, b)]
    rng, means = random.Random(1), []
    for _ in range(n):
        means.append(statistics.fmean(rng.choices(d, k=len(d))))
    means.sort()
    return statistics.fmean(d), means[int(0.025 * n)], means[int(0.975 * n)]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--pause", type=float, default=2.0)
    a = ap.parse_args()
    cases = [json.loads(line) for line in (ROOT / "evals/datasets/multihop.jsonl").read_text().splitlines()]
    router, index = build_router(), Index.load()
    vertex = router.providers[0]
    await index.warm(await vertex.token())
    retriever = LocalRetriever(index, vertex.token)
    graph = build_graph(router, retriever, NoCalendar())
    rows = []
    for c in cases:
        gold = set(c["gold_anchors"])
        raw = recall(await retriever.search(c["question"], 5), gold)
        for rep in range(a.reps):
            ch_a, q_a = await evidence(graph, c["question"], "off")
            await asyncio.sleep(a.pause)
            ch_r, _ = await evidence(graph, c["question"], "force")
            await asyncio.sleep(a.pause)
            rows.append({"id": c["id"], "rep": rep, "raw_question": raw, "plain": recall(ch_a, gold),
                         "research": recall(ch_r, gold), "n_plain": len(ch_a), "n_research": len(ch_r),
                         "rewritten_query": q_a})
            print(f"{c['id']:<15} rep{rep} raw={raw:.2f} plain={rows[-1]['plain']:.2f} "
                  f"research={rows[-1]['research']:.2f}  query={q_a!r}", flush=True)
    by_case = {}
    for r in rows:
        by_case.setdefault(r["id"], []).append(r)
    mean = lambda k: [statistics.fmean(x[k] for x in v) for v in by_case.values()]
    raw, plain, res = mean("raw_question"), mean("plain"), mean("research")
    print(f"\n{len(cases)} questions x {a.reps} repeats, evidence recall (mean over questions)")
    print(f"  raw question as the query (no LLM) : {statistics.fmean(raw):.3f}")
    print(f"  plain RAG (LLM-rewritten query)    : {statistics.fmean(plain):.3f}")
    print(f"  research mode                      : {statistics.fmean(res):.3f}")
    for name, x, y in [("plain - raw", raw, plain), ("research - plain", plain, res),
                       ("research - raw", raw, res)]:
        d, lo, hi = boot(x, y)
        print(f"  {name:<18}: {d:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]")
    spread = [max(r["plain"] for r in v) - min(r["plain"] for r in v) for v in by_case.values()]
    spread_r = [max(r["research"] for r in v) - min(r["research"] for r in v) for v in by_case.values()]
    print(f"  run-to-run recall range (mean over questions): plain {statistics.fmean(spread):.2f}, "
          f"research {statistics.fmean(spread_r):.2f}")
    Path(ROOT / "evals/results").mkdir(exist_ok=True)
    (ROOT / "evals/results/retrieval_study.json").write_text(json.dumps(rows, indent=1))


asyncio.run(main())
