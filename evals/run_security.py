"""Security / robustness suite for the Twin agent. Deterministic checks, no LLM judge.

  uv run python evals/run_security.py --url wss://HOST/ws/chat [--poison]

--poison runs only the indirect-injection cases and expects the server to have been started with
EXTRA_CORPUS_PATH=evals/datasets/poison_corpus.jsonl (a local server; never production).
Exit code is non-zero if any case fails, so it can gate CI.
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import websockets

DATA = Path(__file__).parent / "datasets" / "security.jsonl"


def check(case: dict, text: str) -> list[str]:
    """Deterministic pass/fail rules for one case; returns the list of failures."""
    low, fails = text.lower().replace("\u2019", "'"), []  # models write curly apostrophes too
    for bad in case.get("must_not_contain", []):
        if bad.lower() in low:
            fails.append(f"contains forbidden text: {bad!r}")
    any_of = case.get("must_contain_any")
    if any_of and not any(a.lower() in low for a in any_of):
        fails.append("did not abstain / refuse (none of the expected phrases present)")
    if len(text) > case.get("max_chars", 2000):
        fails.append(f"answer too long ({len(text)} chars)")
    return fails


async def run_case(url: str, case: dict) -> dict:
    sid = "secgate-" + uuid.uuid4().hex[:16]  # monitoring ignores test traffic with this prefix
    texts, cites, degraded = [], [], False
    async with websockets.connect(url, origin="https://ducanhvalentinonguyen.com") as ws:
        for i, q in enumerate(case["turns"]):
            await ws.send(json.dumps({"session_id": sid, "turn_id": f"t{i}", "text": q}))
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), 60))
                if ev["type"] in ("done", "error"):
                    texts.append(ev.get("text", ""))
                    cites = [c["anchor"] for c in ev.get("citations", [])]
                    degraded = degraded or bool(ev.get("degraded"))
                    break
    text = texts[case.get("check_turn", -1)]
    fails = check(case, text)
    if degraded:
        fails.append("degraded mode (model unavailable) - result inconclusive")
    return {"id": case["id"], "category": case["category"], "passed": not fails,
            "fails": fails, "answer": text, "citations": cites}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--poison", action="store_true")
    ap.add_argument("--sleep", type=float, default=5.5, help="seconds between cases (rate limit)")
    ap.add_argument("--out", default="evals/results/security.json")
    a = ap.parse_args()
    cases = [json.loads(line) for line in DATA.read_text().splitlines()]
    cases = [c for c in cases if bool(c.get("needs_poison_corpus")) == a.poison]
    results = []
    for c in cases:
        n_turns = len(c["turns"])
        t0 = time.time()
        results.append(await run_case(a.url, c))
        r = results[-1]
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']:<14} {'; '.join(r['fails'])}")
        if not r["passed"]:  # show what was said, so a failure can be judged without re-running it
            print(f"      answer: {str(r.get('answer', r.get('text', '')))[:400]!r}")
        await asyncio.sleep(max(0.0, a.sleep * n_turns - (time.time() - t0)))
    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["passed"])
    print("\n" + "category".ljust(24) + "passed")
    for k, v in by_cat.items():
        print(f"{k.ljust(24)}{sum(v)}/{len(v)}")
    total = sum(r["passed"] for r in results)
    print(f"\nTOTAL {total}/{len(results)}")
    Path(a.out).parent.mkdir(exist_ok=True)
    Path(a.out).write_text(json.dumps({"url": a.url, "poison": a.poison, "results": results}, indent=1))
    return 0 if total == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
