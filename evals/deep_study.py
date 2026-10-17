"""Does reading the pages the site links to help or hurt retrieval? (Experiment 7)

Compares two corpora with the same retriever (hybrid BM25 + Vertex embeddings, stem+stop, k = 5):
  home  = evals/datasets/corpus_home_only.jsonl, the 28 sections of the home page (the corpus before this experiment)
  deep  = ingest/corpus.jsonl, the home page plus the same-site pages it links to, read one level deep (HTML by heading, PDF by page)
Question sets: primary (35) and phrasing (16) have their gold sections on the home page, so they test for harm (noise from 73 extra chunks);
deep (13) asks about facts that exist only in the linked pages (checked below: no home chunk contains the gold phrase), so it tests for benefit.

  uv run python evals/deep_study.py
"""

import asyncio
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-api"))
sys.path.insert(0, str(ROOT / "evals"))

from app.gateway.router import build_router
from app.retrieval import Index
from lexical_study import boot, load_cases, mrr, recall


def read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def main() -> None:
    home = read(ROOT / "evals/datasets/corpus_home_only.jsonl")
    deep = read(ROOT / "ingest/corpus.jsonl")
    cases = load_cases("evals/datasets/corpus_home_only.jsonl")      # gold sections of the home-page questions stay on the home page
    deep_cases = []
    for c in read(ROOT / "evals/datasets/deep_facts.jsonl"):
        phrases = [p.lower() for p in c["gold_contains"]]
        assert not any(p in ch["text"].lower() for p in phrases for ch in home), f"{c['id']}: the fact is on the home page"
        gold = {ch["anchor"] for ch in deep for p in phrases if p in ch["text"].lower()}
        assert gold, f"{c['id']}: gold phrase not found in any linked chunk"
        deep_cases.append({"id": c["id"], "question": c["question"], "gold": gold})
    cases["deep"] = deep_cases
    token = await build_router().providers[0].token()
    indexes = {}
    for name, chunks in (("home", home), ("deep", deep)):
        idx = Index(chunks)
        await idx.warm(token)
        indexes[name] = idx
    print(f"corpus chunks: home {len(home)}, deep {len(deep)} (linked: {sum(c.get('source') == 'linked' for c in deep)})")
    results = {}
    for kind, cs in cases.items():
        per = {}
        for name, idx in indexes.items():
            rec, rr = [], []
            for c in cs:
                anchors = [x["anchor"] for x in await idx.search(c["question"], token, 5)]
                rec.append(recall(anchors, c["gold"]))
                rr.append(mrr(anchors, c["gold"]))
            per[name] = {"recall": rec, "mrr": rr}
        results[kind] = per
        d, lo, hi = boot(per["home"]["recall"], per["deep"]["recall"])
        better = sum(y > x for x, y in zip(per["home"]["recall"], per["deep"]["recall"], strict=True))
        worse = sum(y < x for x, y in zip(per["home"]["recall"], per["deep"]["recall"], strict=True))
        print(f"\n{kind} (n = {len(cs)}): recall@5 home {statistics.fmean(per['home']['recall']):.3f} -> deep "
              f"{statistics.fmean(per['deep']['recall']):.3f}  change {d:+.3f} [{lo:+.3f}, {hi:+.3f}]  better {better}, worse {worse};  "
              f"MRR {statistics.fmean(per['home']['mrr']):.3f} -> {statistics.fmean(per['deep']['mrr']):.3f}")
        for c, x, y in zip(cs, per["home"]["recall"], per["deep"]["recall"], strict=True):
            if x != y:
                print(f"   {'+' if y > x else '-'} {c['id']}: {x:.2f} -> {y:.2f}  {c['question'][:70]}")
    (ROOT / "evals/results/deep_study.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
