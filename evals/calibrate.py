"""Judge calibration against human labels.

  uv run python evals/calibrate.py export   # writes evals/results/audit.csv (fill the human_* columns)
  uv run python evals/calibrate.py score    # Cohen's kappa judge vs human, with a bootstrap CI

human_unsupported: number of claims in the answer NOT supported by the cited site text (0 if none).
Kappa is computed on the binary "has any unsupported claim".
"""

import csv
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "evals/results"


def kappa(a: list[int], b: list[int]) -> float:
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    pe = sum((sum(1 for x in a if x == k) / n) * (sum(1 for y in b if y == k) / n) for k in (0, 1))
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def export() -> None:
    rows = json.loads((RES / "offline_heldout.json").read_text())
    random.Random(3).shuffle(rows)
    questions = {}
    for line in (ROOT / "evals/datasets/facts.jsonl").read_text().splitlines():
        c = json.loads(line)
        questions[c["id"]] = c["question"]
    chunks = {}
    for line in (ROOT / "ingest/corpus.jsonl").read_text().splitlines():
        c = json.loads(line)
        chunks.setdefault(c["anchor"], c["text"])
    with open(RES / "audit.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "variant", "question", "answer", "cited_sources", "judge_unsupported",
                    "judge_flagged", "human_unsupported"])
        for r in rows:
            src = " || ".join(f"[{a}] {chunks.get(a, '')[:400]}" for a in r["chunks"][:3])
            w.writerow([r["id"], r["variant"], questions.get(r["id"], ""), r["answer"], src, r["judge"]["unsupported"],
                        "; ".join(r["judge"]["unsupported_text"]), ""])
    print("wrote", RES / "audit.csv", "- fill human_unsupported (0, 1, 2...) for ~30 rows")


def score() -> None:
    with open(RES / "audit.csv", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["human_unsupported"].strip() != ""]
    j = [int(int(r["judge_unsupported"]) > 0) for r in rows]
    h = [int(int(r["human_unsupported"]) > 0) for r in rows]
    rng = random.Random(1)
    boots = sorted(kappa(*zip(*[(j[i], h[i]) for i in rng.choices(range(len(j)), k=len(j))])) for _ in range(5000))
    print(f"n={len(rows)} agreement={sum(x == y for x, y in zip(j, h)) / len(j):.2f} "
          f"kappa={kappa(j, h):.2f} 95% CI [{boots[125]:.2f}, {boots[4875]:.2f}]")


{"export": export, "score": score}[sys.argv[1]]()
