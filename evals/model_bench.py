"""Model benchmark through the real graph: hosted Gemini vs self-hosted models on the RTX 4090.

Judge-free and deterministic: (1) the security/robustness cases (same pass/fail rules as the live suite),
(2) answerable fact questions scored by keyword coverage of the golden facts (numbers, names), (3) abstention on
unanswerable questions (phrase check). Also latency, throughput, structured-output parse failures, and for
local models GPU power draw. Retrieval is identical for every model (Vertex embeddings).

  PROVIDERS=vertex  uv run python evals/model_bench.py --label gemini
  PROVIDERS=local LOCAL_BASE_URL=http://localhost:11434/v1 LOCAL_MODEL=gemma4:26b \\
      uv run python evals/model_bench.py --label gemma4-26b --gpu-power
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agent-api"))
sys.path.insert(0, str(ROOT / "evals"))

from app.gateway.router import VertexProvider, build_router
from app.graph import build as graph_build
from app.graph.build import build_graph
from app.retrieval import Index
from app.retrieval_client import LocalRetriever
from run_security import check

ABSTAIN = re.compile(r"don't have|do not have|do(?:es)? not (?:yet )?(?:contain|have)|doesn't contain|"
                     r"no information|not (?:on|in) the (?:site|sources)|not mentioned|"
                     r"isn't (?:listed|mentioned)", re.IGNORECASE)
TOKEN = re.compile(r"[A-Za-z0-9#%+./-]+")


class NoCalendar:
    enabled = False


def key_tokens(fact: str) -> list[str]:
    """Distinctive tokens of a golden fact: anything with a digit, #, %, or a Proper Noun / ACRONYM."""
    out = []
    for t in TOKEN.findall(fact):
        t = t.strip(".,/-")
        if len(t) >= 2 and (any(ch.isdigit() for ch in t) or "#" in t or "%" in t
                            or (t[0].isupper() and len(t) >= 4) or t.isupper()):
            out.append(t.lower())
    return out


def coverage(golden: list[str], answer: str) -> float | None:
    low, scores = answer.lower(), []
    for g in golden:
        keys = key_tokens(g)
        if keys:
            scores.append(1.0 if sum(k in low for k in keys) / len(keys) >= 0.6 else 0.0)
    return statistics.fmean(scores) if scores else None


class GpuPower:
    """Samples nvidia-smi power draw once a second while a block runs."""

    def __init__(self):
        self.samples: list[float] = []
        self._task = None

    async def _loop(self):
        while True:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            try:
                self.samples.append(float(out.decode().splitlines()[0]))
            except (ValueError, IndexError):
                pass  # a missed sample is fine
            await asyncio.sleep(1)

    def start(self):
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        self._task.cancel()


async def run(graph, turns: list[str]) -> dict:
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    t0, ttft, final = time.monotonic(), None, {}
    texts = []
    for q in turns:
        async for mode, ev in graph.astream({"question": q, "records": [], "variant": "A", "research": "off"},
                                            cfg, stream_mode=["custom", "updates"]):
            if mode == "custom" and ev["type"] == "delta" and ttft is None:
                ttft = time.monotonic() - t0
            elif mode == "updates":
                for upd in ev.values():
                    for k, v in (upd or {}).items() if isinstance(upd, dict) else []:
                        if k == "records":
                            final.setdefault("records", []).extend(v)
                        else:
                            final[k] = v
        texts.append(final.get("answer", ""))
    recs = final.get("records") or []
    return {"texts": texts, "latency_s": time.monotonic() - t0, "ttft_s": ttft,
            "tokens_out": sum(r.tokens_out for r in recs), "gen_s": sum(r.latency_ms for r in recs) / 1000,
            "degraded": bool(final.get("degraded")), "providers": sorted({r.provider for r in recs if not r.error})}


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--gpu-power", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="cases per group (smoke tests)")
    ap.add_argument("--pause", type=float, default=1.5)
    ap.add_argument("--rescore", action="store_true", help="re-apply the checks to saved answers")
    a = ap.parse_args()
    load = lambda n: [json.loads(x) for x in (ROOT / f"evals/datasets/{n}.jsonl").read_text().splitlines()]
    sec = [c for c in load("security") if not c.get("needs_poison_corpus")]
    facts = load("facts")
    ans = [c for c in facts if c["answerable"]]
    una = [c for c in facts if not c["answerable"]]
    if a.limit:
        sec, ans, una = sec[:a.limit], ans[:a.limit], una[:a.limit]
    if a.rescore:
        path = ROOT / f"evals/results/model_bench_{a.label}.json"
        saved = json.loads(path.read_text())
        by_id = {c["id"]: c for c in sec}
        for r in saved["rows"]:
            if r["group"] == "security":
                r["score"] = float(not check(by_id[r["id"]], r["answer"]) and not r["degraded"])
            elif r["group"] == "abstain":
                r["score"] = float(bool(ABSTAIN.search(r["answer"].replace("\u2019", "'"))))
        sm = saved["summary"]
        sm["security_pass"] = f"{sum(r['score'] for r in saved['rows'] if r['group'] == 'security'):.0f}/{len(sec)}"
        sm["abstained_on_unanswerable"] = (f"{sum(r['score'] for r in saved['rows'] if r['group'] == 'abstain'):.0f}"
                                           f"/{len(una)}")
        path.write_text(json.dumps(saved, indent=1))
        print(json.dumps(sm, indent=1))
        return
    router, index = build_router(), Index.load()
    tok = VertexProvider("tok", {})
    await index.warm(await tok.token())
    graph = build_graph(router, LocalRetriever(index, tok.token), NoCalendar())
    power = GpuPower()
    if a.gpu_power:
        power.start()
    rows = []
    t_start = time.monotonic()
    for group, cases in (("security", sec), ("facts", ans), ("abstain", una)):
        for c in cases:
            r = await run(graph, c.get("turns") or [c["question"]])
            text = r["texts"][c.get("check_turn", -1)] if group == "security" else r["texts"][-1]
            if group == "security":
                r["score"] = float(not check(c, text) and not r["degraded"])
            elif group == "facts":
                r["score"] = coverage(c["golden"], text)
            else:
                r["score"] = float(bool(ABSTAIN.search(text.replace("\u2019", "'"))))
            r.update(group=group, id=c["id"], answer=text)
            rows.append(r)
            print(f"{group:8} {c['id']:<16} score={r['score']} {r['latency_s']:.1f}s", flush=True)
            await asyncio.sleep(a.pause)
    wall = time.monotonic() - t_start
    if a.gpu_power:
        power.stop()
    st = graph_build.STATS
    lat = [r["latency_s"] for r in rows]
    ttft = [r["ttft_s"] for r in rows if r["ttft_s"]]
    tps = [r["tokens_out"] / r["gen_s"] for r in rows if r["gen_s"] > 0 and r["tokens_out"]]
    summary = {
        "label": a.label, "cases": len(rows), "degraded": sum(r["degraded"] for r in rows),
        "security_pass": f"{sum(r['score'] for r in rows if r['group'] == 'security'):.0f}/{len(sec)}",
        "fact_keyword_coverage": round(statistics.fmean(r["score"] for r in rows
                                                         if r["group"] == "facts" and r["score"] is not None), 3),
        "abstained_on_unanswerable": f"{sum(r['score'] for r in rows if r['group'] == 'abstain'):.0f}/{len(una)}",
        "latency_p50_s": round(pct(lat, .5), 2), "latency_p95_s": round(pct(lat, .95), 2),
        "ttft_p50_s": round(pct(ttft, .5), 2), "gen_tokens_per_s_median": round(statistics.median(tps), 1),
        "structured_first_try_failures": f"{st['structured_failed']}/{st['structured_calls']}",
    }
    if a.gpu_power and power.samples:
        mean_w = statistics.fmean(power.samples)
        kwh_per_answer = mean_w * (wall / len(rows)) / 3.6e6
        summary.update(gpu_mean_watts=round(mean_w), gpu_energy_wh_per_answer=round(kwh_per_answer * 1000, 3),
                       electricity_eur_per_answer_at_0_30_per_kwh=round(kwh_per_answer * 0.30, 6))
    print("\n" + json.dumps(summary, indent=1))
    out = ROOT / "evals/results"
    out.mkdir(exist_ok=True)
    (out / f"model_bench_{a.label}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))


asyncio.run(main())
