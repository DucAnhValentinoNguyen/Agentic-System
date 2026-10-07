"""How much conversation history should each step see, and does a running summary help?

Each scenario: the visitor asks about a project (turn A), then sends k unrelated filler turns, then asks a question
that only makes sense given turn A ("how is it trained?"). The run stops before the answer node. Metric: was the
section A was about retrieved for that last question (gold anchor in the evidence)? Fillers are small talk, so they
cost one cheap classifier call each and no answer generation.

Configurations are (history window in messages, summary_after in messages; 0 = no summary).

  uv run python evals/context_study.py
"""

import asyncio
import json
import math
import random
import statistics
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-api"))

from app.config import settings
from app.gateway.router import ProviderError, build_router
from app.graph.build import build_graph
from app.retrieval import Index
from app.retrieval_client import LocalRetriever

SCENARIOS = [  # (turn A, question that refers back, gold anchor)
    ("What is SurgGround?", "How is it trained?", "surgground"),
    ("Tell me about the gnhf fix", "What was the impact of that?", "gnhf-merged-fix"),
    ("What is SciPaLI?", "And what was the biggest improvement there?", "scipali"),
    ("What is TraceForge?", "What was his part in it?", "traceforge"),
    ("What is EdgeLoop?", "How does that work end to end?", "edgeloop"),
    ("What did he do at ZEISS?", "Which models did that beat?", "surgical-vision-language-foundation-model"),
]
FILLERS = ["thanks!", "ok cool", "great", "nice", "awesome", "got it", "alright", "perfect", "wonderful", "good to know"]
CONFIGS = [(2, 0), (6, 0), (10, 0), (6, 12), (6, 4)]
GAPS = [2, 4, 8]


class NoCalendar:
    enabled = False


FAILS = {"n": 0}  # provider failures seen; a conversation with any failure is discarded and repeated


async def run(graph, a, ref, gold, k, rng) -> float | None:
    before = FAILS["n"]
    hit = await _run(graph, a, ref, gold, k, rng)
    return hit if FAILS["n"] == before else None


async def _run(graph, a, ref, gold, k, rng) -> float:
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    await asyncio.sleep(rng.random())
    async for _ in graph.astream({"question": a, "records": []}, cfg, stream_mode="updates", interrupt_before=["answer"]):
        pass
    # turn A is not answered (stopped before the answer): record it as the history a real turn would have left
    await graph.aupdate_state(cfg, {"history": [{"role": "user", "content": a},
                                                {"role": "assistant", "content": f"(an answer about: {a})"}]})
    for i in range(k):
        async for _ in graph.astream({"question": FILLERS[i % len(FILLERS)], "records": []}, cfg, stream_mode="updates"):
            pass
    final: dict = {}
    async for ev in graph.astream({"question": ref, "records": []}, cfg, stream_mode="updates",
                                  interrupt_before=["answer"]):
        for upd in ev.values():
            final.update(upd if isinstance(upd, dict) else {})
    return float(gold in {c["anchor"] for c in final.get("chunks") or []})


async def main() -> None:
    router, index = build_router(), Index.load()
    await index.warm(await router.providers[0].token())
    inner = router.complete

    async def counted(*args, **kw):
        try:
            return await inner(*args, **kw)
        except ProviderError:
            FAILS["n"] += 1
            raise
    router.complete = counted
    graph = build_graph(router, LocalRetriever(index, router.providers[0].token), NoCalendar())
    rng, rows = random.Random(1), []
    sem = asyncio.Semaphore(1)  # one at a time: parallel runs trip the model quota and contaminate the result
    for window, after in CONFIGS:
        settings.history_window, settings.summary_after = window, after or 10**6
        for k in GAPS:
            async def one(s, gap):
                async with sem:
                    for attempt in range(3):
                        got = await run(graph, *s, gap, rng)
                        if got is not None:
                            return got
                        await asyncio.sleep(10)  # let the quota recover, then repeat the whole conversation
                    return float("nan")
            res = await asyncio.gather(*[one(s, k) for s in SCENARIOS])
            rows.append({"window": window, "summary_after": after, "gap_turns": k, "hits": res})
            bad = sum(math.isnan(r) for r in res)
            print(f"window={window:<3} summary_after={after:<3} gap={k} turns: found "
                  f"{sum(r for r in res if not math.isnan(r)):.0f}/{len(res) - bad}" + (f"  ({bad} discarded: provider failures)" if bad else ""),
                  flush=True)
    print("\nshare of follow-ups that retrieved the right section (6 scenarios per cell)")
    print(f"{'config':<26}" + "".join(f"gap {k} turns".rjust(13) for k in GAPS) + "   mean")
    for window, after in CONFIGS:
        cells = [next(r for r in rows if (r["window"], r["summary_after"], r["gap_turns"]) == (window, after, k)) for k in GAPS]
        vals = [statistics.fmean([h for h in c["hits"] if not math.isnan(h)] or [float("nan")]) for c in cells]
        name = f"window {window}, " + (f"summary after {after}" if after else "no summary")
        print(f"{name:<26}" + "".join(f"{v:13.2f}" for v in vals) + f"   {statistics.fmean(vals):.2f}")
    (ROOT / "evals/results/context_study.json").write_text(json.dumps(rows, indent=1))


asyncio.run(main())
