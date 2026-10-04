"""Does research mode trigger on follow-ups, and does it repeat searches?

Runs each conversation through the real graph. The second turn stops before the answer node (no answer cost),
so only routing is measured: how many searches ran, and how many were near-duplicates of an earlier search.
Standalone complex questions must still use research mode.

  uv run python evals/followup_check.py
"""

import asyncio
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-api"))

from app.gateway.router import VertexProvider, build_router
from app.graph.build import build_graph
from app.retrieval import Index
from app.retrieval_client import LocalRetriever

FOLLOWUPS = [
    ("what did he do in the AWS challenge?", "tell me more about his solutions"),
    ("What is SurgGround?", "how is it trained?"),
    ("Tell me about the gnhf fix", "what was the impact?"),
    ("What is SciPaLI?", "and what was the biggest improvement?"),
    ("What is TraceForge?", "what was his part in it?"),
    ("What did he do at ZEISS?", "which models did that beat?"),
]
STANDALONE = [
    "Compare his two open-source contributions: what was each bug and what did he change?",
    "Which of his projects have private code, and why?",
    "What GPU hardware has he used across his projects?",
    "List the hackathons and competitions he took part in, with outcomes where stated.",
]
STOP = {"the", "a", "an", "of", "and", "in", "to", "for", "his", "he", "on", "with", "what", "how", "is", "are"}


class NoCalendar:
    enabled = False


def toks(q: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", q.lower()) if t not in STOP}


def near_dupes(queries: list[str]) -> int:
    n = 0
    for i, q in enumerate(queries):
        a = toks(q)
        if any(a and toks(p) and len(a & toks(p)) / len(a | toks(p)) >= 0.75 for p in queries[:i]):
            n += 1
    return n


async def turn(graph, cfg, question: str, stop_before_answer: bool) -> list[str]:
    searches: list[str] = []
    kw = {"interrupt_before": ["answer"]} if stop_before_answer else {}
    async for mode, ev in graph.astream({"question": question, "records": []}, cfg,
                                        stream_mode=["custom", "updates"], **kw):
        if mode == "custom" and ev.get("type") == "step" and ev["text"].startswith("Searching: "):
            searches.append(ev["text"][len("Searching: "):])
    return searches


async def main() -> None:
    router, index = build_router(), Index.load()
    tok = VertexProvider("tok", {})
    await index.warm(await tok.token())
    graph = build_graph(router, LocalRetriever(index, tok.token), NoCalendar())
    fu_research = fu_searches = fu_dupes = 0
    print("FOLLOW-UPS (second turn)")
    for first, second in FOLLOWUPS:
        cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
        await turn(graph, cfg, first, stop_before_answer=False)
        s = await turn(graph, cfg, second, stop_before_answer=True)
        fu_research += bool(s); fu_searches += len(s); fu_dupes += near_dupes(s)
        print(f"  {second!r:48} research={'yes' if s else 'no ':3} searches={len(s)} duplicates={near_dupes(s)}")
        await asyncio.sleep(1)
    print(f"  -> research mode on {fu_research}/{len(FOLLOWUPS)} follow-ups, {fu_searches} searches, {fu_dupes} near-duplicates")
    sa_research = sa_dupes = 0
    print("STANDALONE COMPLEX QUESTIONS")
    for q in STANDALONE:
        cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
        s = await turn(graph, cfg, q, stop_before_answer=True)
        sa_research += bool(s); sa_dupes += near_dupes(s)
        print(f"  {q[:60]!r:64} research={'yes' if s else 'no ':3} searches={len(s)} duplicates={near_dupes(s)}")
        await asyncio.sleep(1)
    print(f"  -> research mode on {sa_research}/{len(STANDALONE)}, {sa_dupes} near-duplicates")


asyncio.run(main())
