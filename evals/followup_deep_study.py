"""Does searching with the visitor's own words as well as the classifier's rewrite help follow-up questions? (Experiment 8)

Real classifier (it writes the standalone search query from the conversation), real hybrid retrieval, no answer generation. Each case is a
follow-up whose answer is only in a linked page; the earlier turn is seeded into the history, so the classifier has context to rewrite from.
Compares retrieve_raw_too False (rewrite only, the behaviour before) and True (rewrite and the raw question merged by rank), 3 repeats per
case because the rewrite varies from run to run.

  uv run python evals/followup_deep_study.py [--reps 3]
"""

import argparse
import asyncio
import json
import statistics
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-api"))
sys.path.insert(0, str(ROOT / "evals"))

from app.config import settings
from app.gateway.router import build_router
from app.graph.build import build_graph
from app.retrieval import Index
from app.retrieval_client import LocalRetriever
from lexical_study import boot

AWS = "Tell me about the AWS challenge."
AWS_A = "He took part in the Cloud Innovation Challenge (LMU x AWS) and designed an AWS architecture for a fictional Formula 1 team, Velocity Racing, delivered as a scored report and an interactive demo with synthetic data."
WILD = "Tell me about the wildboar project."
WILD_A = "It is a statistical consulting analysis of a field survey about wildlife compensation and local tolerance towards wild boars, using ordinal models, Bayesian checks and PLS-SEM."
ZEISS = "What is he doing in the ZEISS collaboration?"
ZEISS_A = "He builds vision-language models for surgical video, including a self-supervised pretraining benchmark on endoscopy images and evaluation harnesses."
CASES = [
    (AWS, AWS_A, "what recovery objectives does the pilot propose?", ["RTO 15 min"]),
    (AWS, AWS_A, "how much is the pilot capped at?", ["capped at €100k"]),
    (AWS, AWS_A, "how does the fan platform stop a second scan of the same pass?", ["atomic conditional write"]),
    (AWS, AWS_A, "what runs at the track edge in the telemetry design?", ["AWS IoT Greengrass"]),
    (AWS, AWS_A, "how many championship positions does the break-even need?", ["0.13 of one championship position"]),
    (WILD, WILD_A, "what did the study conclude about causation?", ["qualified association"]),
    (WILD, WILD_A, "why can it not make causal claims?", ["cross-sectional, nonrandom"]),
    (ZEISS, ZEISS_A, "why were three classes dropped from the 20-way task?", ["fewer than 12 labeled images"]),
    (ZEISS, ZEISS_A, "which protocol evaluated the frozen features?", ["weighted-k-NN protocol"]),
]


class NoCalendar:
    enabled = False


async def evidence(graph, history: list[dict], question: str) -> list[dict]:
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    final: dict = {}
    inp = {"question": question, "records": [], "variant": "A", "history": history}
    async for ev in graph.astream(inp, cfg, stream_mode="updates", interrupt_before=["answer"]):
        for upd in ev.values():
            final.update(upd if isinstance(upd, dict) else {})
    return final.get("chunks") or []


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    a = ap.parse_args()
    chunks = [json.loads(line) for line in (ROOT / "ingest/corpus.jsonl").read_text().splitlines()]
    router = build_router()
    index = Index(chunks)
    token = await router.providers[0].token()
    await index.warm(token)
    graph = build_graph(router, LocalRetriever(index, router.providers[0].token), NoCalendar())
    per: dict[bool, list[float]] = {False: [], True: []}
    for prime, answer, follow, phrases in CASES:
        gold = {c["anchor"] for c in chunks if any(p.lower() in c["text"].lower() for p in phrases)}
        assert gold, follow
        history = [{"role": "user", "content": prime}, {"role": "assistant", "content": answer}]
        row = {}
        for flag in (False, True):
            settings.retrieve_raw_too = flag
            hits = []
            for _ in range(a.reps):
                got = {c["anchor"] for c in await evidence(graph, history, follow)}
                hits.append(len(gold & got) / len(gold))
                await asyncio.sleep(1)
            per[flag].append(statistics.fmean(hits))
            row[flag] = statistics.fmean(hits)
        print(f"{row[False]:.2f} -> {row[True]:.2f}   {follow}", flush=True)
    d, lo, hi = boot(per[False], per[True])
    print(f"\nrecall of the answering section in the top 5 (n = {len(CASES)} follow-ups x {a.reps}):  rewrite only {statistics.fmean(per[False]):.3f}"
          f"  ->  rewrite + raw {statistics.fmean(per[True]):.3f}   change {d:+.3f} [{lo:+.3f}, {hi:+.3f}]")
    (ROOT / "evals/results/followup_deep.json").write_text(json.dumps({"rewrite_only": per[False], "rewrite_plus_raw": per[True]}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
