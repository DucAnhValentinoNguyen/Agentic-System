"""Does an LLM reranker help? Candidates are fetched once per question, so baseline and reranked are exactly paired.

Questions: the 12 multi-hop ones (gold anchors given) plus the answerable single-fact ones whose gold facts could be
located in the corpus by substring (gold anchors found automatically; stated in the output).
Baseline = the first 5 passages of the retrieval ranking. Reranked = 10 candidates, the model keeps the ones that help.
Metrics per question: recall of gold anchors in the context, and noise = passages in the context that are not gold.

  uv run python evals/rerank_study.py --reps 2
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-api"))

from app.gateway.router import CallRecord, build_router
from app.graph.build import rerank_chunks
from app.retrieval import Index
from app.retrieval_client import LocalRetriever


def load_cases() -> list[dict]:
    corpus = [json.loads(line) for line in (ROOT / "ingest/corpus.jsonl").read_text().splitlines()]
    cases = []
    for line in (ROOT / "evals/datasets/multihop.jsonl").read_text().splitlines():
        c = json.loads(line)
        cases.append({"id": c["id"], "question": c["question"], "gold": set(c["gold_anchors"]), "kind": "multihop"})
    for line in (ROOT / "evals/datasets/facts.jsonl").read_text().splitlines():
        c = json.loads(line)
        if not c["answerable"]:
            continue
        gold = {ch["anchor"] for g in c["golden"] for ch in corpus if g.lower() in ch["text"].lower()}
        if gold:
            cases.append({"id": c["id"], "question": c["question"], "gold": gold, "kind": "single"})
    return cases


def score(chunks: list[dict], gold: set[str]) -> tuple[float, float]:
    anchors = [c["anchor"] for c in chunks]
    recall = len(gold & set(anchors)) / len(gold)
    noise = sum(a not in gold for a in anchors)
    return recall, noise


def boot(a: list[float], b: list[float], n: int = 10000) -> tuple[float, float, float]:
    d = [y - x for x, y in zip(a, b)]
    rng, means = random.Random(1), []
    for _ in range(n):
        means.append(statistics.fmean(rng.choices(d, k=len(d))))
    means.sort()
    return statistics.fmean(d), means[int(0.025 * n)], means[int(0.975 * n)]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--keep", type=int, default=5)
    ap.add_argument("--pause", type=float, default=1.5)
    a = ap.parse_args()
    cases = load_cases()
    router, index = build_router(), Index.load()
    await index.warm(await router.providers[0].token())
    retriever = LocalRetriever(index, router.providers[0].token)
    rows = []
    for c in cases:
        cand = await retriever.search(c["question"], 10)
        b_rec, b_noise = score(cand[:a.keep], c["gold"])
        for rep in range(a.reps):
            recs: list[CallRecord] = []
            kept = await rerank_chunks(router, c["question"], cand, a.keep, recs)
            r_rec, r_noise = score(kept, c["gold"])
            safe = kept + [x for x in cand[:2] if x["anchor"] not in {k["anchor"] for k in kept}]  # keep the top 2 too
            s_rec, s_noise = score(safe[:a.keep], c["gold"])
            rows.append({"id": c["id"], "kind": c["kind"], "rep": rep, "base_recall": b_rec, "base_noise": b_noise,
                         "rr_recall": r_rec, "rr_noise": r_noise, "n_kept": len(kept),
                         "safe_recall": s_rec, "safe_noise": s_noise, "n_safe": len(safe[:a.keep]),
                         "cost": sum(r.cost_usd for r in recs), "latency_ms": sum(r.latency_ms for r in recs)})
            print(f"{c['id']:<18}{c['kind']:<9} base recall={b_rec:.2f} noise={b_noise} | rerank recall={r_rec:.2f} "
                  f"noise={r_noise} kept={len(kept)}", flush=True)
            await asyncio.sleep(a.pause)
    by = {}
    for r in rows:
        by.setdefault(r["id"], []).append(r)
    mean = lambda k, sel=None: [statistics.fmean(x[k] for x in v) for i, v in by.items() if sel is None or v[0]["kind"] == sel]
    print(f"\n{len(by)} questions ({sum(v[0]['kind']=='multihop' for v in by.values())} multi-hop, "
          f"{sum(v[0]['kind']=='single' for v in by.values())} single-fact, gold anchors for the latter found by substring), "
          f"{a.reps} reranker runs each, context = {a.keep} passages")
    for sel in (None, "multihop", "single"):
        br, rr = mean("base_recall", sel), mean("rr_recall", sel)
        bn, rn = mean("base_noise", sel), mean("rr_noise", sel)
        d, lo, hi = boot(br, rr)
        dn, nlo, nhi = boot(bn, rn)
        sr, sn = mean("safe_recall", sel), mean("safe_noise", sel)
        ds, slo, shi = boot(br, sr)
        dsn, snlo, snhi = boot(bn, sn)
        print(f"  [{sel or 'all':<8}] + keep top-2 : recall {statistics.fmean(br):.3f} -> {statistics.fmean(sr):.3f} "
              f"({ds:+.3f}, CI [{slo:+.3f}, {shi:+.3f}]) | noise {statistics.fmean(bn):.2f} -> {statistics.fmean(sn):.2f} "
              f"({dsn:+.2f}, CI [{snlo:+.2f}, {snhi:+.2f}])")
        print(f"  [{sel or 'all':<8}] rerank only  : recall {statistics.fmean(br):.3f} -> {statistics.fmean(rr):.3f} "
              f"({d:+.3f}, CI [{lo:+.3f}, {hi:+.3f}]) | noise passages {statistics.fmean(bn):.2f} -> "
              f"{statistics.fmean(rn):.2f} ({dn:+.2f}, CI [{nlo:+.2f}, {nhi:+.2f}])")
    print(f"  extra cost per question: ${statistics.fmean(r['cost'] for r in rows):.5f}, "
          f"extra latency: {statistics.fmean(r['latency_ms'] for r in rows) / 1000:.2f} s, "
          f"passages kept on average: {statistics.fmean(r['n_kept'] for r in rows):.2f}")
    (ROOT / "evals/results/rerank_study.json").write_text(json.dumps(rows, indent=1))


asyncio.run(main())
