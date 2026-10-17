"""Does stemming and a stop-word list improve BM25 retrieval over the site corpus? (Experiment 6)

Retrieval only: no answer generation, no judge. For every answerable question with known gold sections, build the index with each analyzer
and measure, at k = 5, the share of gold sections retrieved (recall) and the reciprocal rank of the first gold section (MRR).
Questions: "primary" = the 12 multi-hop cases plus the single-fact cases of the 40-case fact set whose gold facts can be located in the corpus
(the same cases as evals/rerank_study.py), written before this change; "phrasing" = 16 natural questions with inflections and filler words, written
after the change was proposed (so it can favour the change; the primary set cannot).

  uv run python evals/lexical_study.py            # lexical half only, deterministic, no network
  uv run python evals/lexical_study.py --hybrid   # also the real hybrid (BM25 + Vertex embeddings, rank fusion)
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

from app.retrieval import Index

ANALYZERS = ["plain", "stop", "stem", "stem+stop"]


def load_cases() -> dict[str, list[dict]]:
    corpus = [json.loads(line) for line in (ROOT / "ingest/corpus.jsonl").read_text().splitlines()]
    primary, phrasing = [], []
    for line in (ROOT / "evals/datasets/multihop.jsonl").read_text().splitlines():
        c = json.loads(line)
        primary.append({"id": c["id"], "question": c["question"], "gold": set(c["gold_anchors"])})
    for line in (ROOT / "evals/datasets/facts.jsonl").read_text().splitlines():
        c = json.loads(line)
        if c["answerable"]:
            gold = {ch["anchor"] for g in c["golden"] for ch in corpus if g.lower() in ch["text"].lower()}
            if gold:
                primary.append({"id": c["id"], "question": c["question"], "gold": gold})
    for line in (ROOT / "evals/datasets/retrieval_phrasing.jsonl").read_text().splitlines():
        c = json.loads(line)
        gold = {ch["anchor"] for g in c["gold_contains"] for ch in corpus if g.lower() in ch["text"].lower()}
        assert gold, c["id"]
        phrasing.append({"id": c["id"], "question": c["question"], "gold": gold})
    return {"primary": primary, "phrasing": phrasing}


def recall(anchors: list[str], gold: set[str]) -> float:
    return len(gold & set(anchors)) / len(gold)


def mrr(anchors: list[str], gold: set[str]) -> float:
    return next((1 / (i + 1) for i, a in enumerate(anchors) if a in gold), 0.0)


def boot(a: list[float], b: list[float], n: int = 10000) -> tuple[float, float, float]:
    d = [y - x for x, y in zip(a, b, strict=True)]
    rng, means = random.Random(1), []
    for _ in range(n):
        means.append(statistics.fmean(rng.choices(d, k=len(d))))
    means.sort()
    return statistics.fmean(d), means[int(0.025 * n)], means[int(0.975 * n)]


def lexical_ranking(index: Index, question: str) -> list[str]:
    from app.retrieval import tok
    scores = index.bm25.get_scores(tok(question, index.analyzer))
    order = sorted(range(len(index.chunks)), key=lambda i: -scores[i])
    return [index.chunks[i]["anchor"] for i in order]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hybrid", action="store_true")
    a = ap.parse_args()
    cases = load_cases()
    chunks = Index.load().chunks
    token = None
    if a.hybrid:
        from app.gateway.router import build_router
        token = await build_router().providers[0].token()
    results: dict = {}
    for kind, cs in cases.items():
        print(f"\n=== {kind}: {len(cs)} questions ===")
        per: dict[str, dict[str, list[float]]] = {}
        for an in ANALYZERS:
            idx = Index(chunks, analyzer=an)
            if a.hybrid:
                await idx.warm(token)
            rec, rr = [], []
            for c in cs:
                if a.hybrid:
                    anchors = [x["anchor"] for x in await idx.search(c["question"], token, 5)]
                else:
                    anchors = lexical_ranking(idx, c["question"])[:5]
                rec.append(recall(anchors, c["gold"]))
                rr.append(mrr(anchors, c["gold"]))
            per[an] = {"recall": rec, "mrr": rr}
        results[kind] = per
        base = per["plain"]
        print(f"{'analyzer':<10} {'recall@5':>9} {'MRR':>7}   change in recall vs plain (95% interval, paired bootstrap)   questions that changed")
        for an in ANALYZERS:
            d, lo, hi = boot(base["recall"], per[an]["recall"]) if an != "plain" else (0, 0, 0)
            better = sum(y > x for x, y in zip(base["recall"], per[an]["recall"], strict=True))
            worse = sum(y < x for x, y in zip(base["recall"], per[an]["recall"], strict=True))
            print(f"{an:<10} {statistics.fmean(per[an]['recall']):>9.3f} {statistics.fmean(per[an]['mrr']):>7.3f}   "
                  f"{d:+.3f} [{lo:+.3f}, {hi:+.3f}]   better {better}, worse {worse}")
        for c, x, y in zip(cs, base["recall"], per["stem+stop"]["recall"], strict=True):
            if x != y:
                print(f"   {'+' if y > x else '-'} {c['id']}: {x:.2f} -> {y:.2f}  {c['question'][:60]}")
    out = ROOT / "evals/results" / ("lexical_hybrid.json" if a.hybrid else "lexical_bm25.json")
    out.write_text(json.dumps(results, indent=1))
    print("\nwritten", out.relative_to(ROOT))


if __name__ == "__main__":
    asyncio.run(main())
