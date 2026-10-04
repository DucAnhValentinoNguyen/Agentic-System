"""How often does the classifier flag a question as complex? (want: multi-hop yes, single-fact no)"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-api"))

from app.gateway.router import build_router
from app.graph.build import CLASSIFY, Classification, structured


async def flag(router, q: str) -> bool:
    c = await structured(router, Classification, [{"role": "system", "content": CLASSIFY},
                                                  {"role": "user", "content": q}], [], 800)
    return c.complex


async def main() -> None:
    router = build_router()
    multi = [json.loads(x) for x in (ROOT / "evals/datasets/multihop.jsonl").read_text().splitlines()]
    single = [json.loads(x) for x in (ROOT / "evals/datasets/facts.jsonl").read_text().splitlines()
              if json.loads(x)["answerable"]]
    for name, cases in [("multi-hop", multi), ("single-fact", single)]:
        flags = []
        for c in cases:
            flags.append(await flag(router, c["question"]))
            await asyncio.sleep(0.5)
        print(f"{name:12} flagged complex: {sum(flags)}/{len(flags)}")
        if name == "single-fact":
            print("  false positives:", [c["question"] for c, f in zip(cases, flags) if f])
        else:
            print("  missed:", [c["question"][:70] for c, f in zip(cases, flags) if not f])


asyncio.run(main())
