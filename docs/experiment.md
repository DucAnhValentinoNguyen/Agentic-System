# Experiment: does claim verification reduce unsupported claims?

**Hypothesis (written before building the verify node).** Adding a verification step, which checks each
claim in the answer against the retrieved sources and removes unsupported claims, reduces unsupported
claims compared with plain RAG, at an acceptable latency and cost.

- **A**: plain RAG answer. **B**: A + a `verify` node (claim extraction, rewrite on failure; streamed as
  provisional text, then `retract` + replacement).
- Same model (Gemini 2.5 Flash), same retrieval, same 24 held-out questions (17 answerable, 7 not).
- Judge: `gpt-oss-120b` on Groq, a different model family from the answerer.
- Caveat on "acceptable": I did not fix numeric latency/cost budgets in advance. That was a gap in the
  hypothesis; the verdict below does not depend on it.

## Result

| | A (plain RAG) | B (+ verify) | B - A, 95% paired bootstrap CI |
|---|---|---|---|
| Unsupported claims / claims | 1 / 44 (2.3%) | 1 / 39 (2.6%) | 0.00 per answer [-0.13, +0.13] |
| Golden-fact coverage (answerable) | 13 / 17 | 12 / 17 | -0.06 [-0.29, +0.12] |
| Abstained on unanswerable | 7 / 7 | 7 / 7 | n/a |
| Latency p50 / p95 | 2.17 s / 2.74 s | 3.89 s / 6.59 s | **+2.01 s [+1.57, +2.54]** |
| Cost per answer | $0.00070 | $0.00118 | **+$0.0005 [+0.0003, +0.0007]** |

**Verdict: not supported.** The baseline is already at about 2% unsupported claims (small curated corpus,
a "answer only from the numbered sources" prompt, citation validation). The verify step finds almost
nothing to remove, and adds about 2 s and about 68% cost per answer. **Decision: production stays on
variant A**; B stays in the code behind `AB_VARIANT` for future work.

## How the first result was wrong

The first judged run showed B better (unsupported 12.8% to 7.3%). Before believing it I read the flagged
items. Most were not hallucinations: the judge was counting *abstentions* ("I don't have that information
on the site") and "email him" suggestions as unsupported claims, and 6 judge failures were silently scored
as "covers nothing". I tightened the judge prompt, added a deterministic filter for non-claims, made judge
failures exclude a case from both variants instead of scoring it, and re-judged the *saved answers* (the
generated text did not change). The apparent improvement disappeared.

## Limits (read before quoting any number)

- 24 questions, one run each, one corpus: wide intervals. "No detectable benefit" is not "no benefit".
- The judge fixes were made while looking at this held-out set's outputs, so it is no longer an untouched
  held-out set. Treat the numbers as development-grade.
- **The judge is not yet calibrated against human labels.** `evals/results/audit.csv` holds the shuffled
  items; fill `human_unsupported`, then `uv run python evals/calibrate.py score` prints Cohen's kappa with a
  bootstrap CI. Until then the judge's absolute numbers are unverified; only the A-vs-B comparison, which uses
  the same judge on both, is somewhat robust.
- Abstention is measured with a phrase check (the LLM judge was unreliable on it). I read all 14 unanswerable
  answers: every one was a correct abstention.
- Two bugs in my own measurement were found by sanity checks and fixed: per-turn cost was under-counted
  (node updates overwrote each other's call records), and the judge failure handling above.

Reproduce: `uv run python evals/run_offline.py --split heldout` (about 10 minutes; the Groq free tier is
limited to 8,000 tokens per minute).

---

# Experiment 2: does research mode (plan, parallel search, reflect) retrieve better evidence?

**Setup.** "Research mode" is a LangGraph path for questions that need several places on the site: a
planner splits the question into 2-4 searches (structured output), the original question is always searched
too, the searches run **in parallel** (LangGraph `Send` fan-out), a reflect step checks whether every part has
evidence and, if not, runs **one more round** of targeted searches, then the answer is written from the merged
evidence. The chat shows each step live. 12 multi-hop questions (`evals/datasets/multihop.jsonl`), each with the
site sections that must be found (gold anchors). `evals/retrieval_study.py` runs the real graph but stops before
the answer node, so only **evidence recall** (share of gold sections retrieved) is measured: deterministic, no judge,
3 repeats per question.

| Evidence recall (mean over 12 questions) | |
|---|---|
| raw question as the query (no LLM) | 0.667 |
| plain RAG (LLM-rewritten query, the production path) | 0.685 |
| **research mode** | **0.810** |
| research - plain | **+0.125, 95% CI [+0.051, +0.218]** |

Plain RAG is also unstable: its recall for the *same* question varies by about 0.21 between runs (mean range per
question) because the LLM writes a different search query each time; research mode varies by 0.15, since it always
also searches the original question.

**Cost.** A throttled-but-complete dev run measured roughly +3.4 to +7.8 s median latency and up to about 2x cost per
answer (the research path makes 6-8 model calls). So production uses it only when the classifier flags a question as
complex (`RESEARCH_MODE=auto`): 12/12 multi-hop questions are flagged, 2/28 single-fact ones.

**What is NOT shown.**
- **Answer quality.** I intended to judge answers too, but the free-tier judge quota (200k tokens per day) ran out
  (INC-005); the earlier judged runs were also contaminated by provider rate limits (degraded answers, fallbacks to a
  weaker model). Better evidence does not by itself guarantee a better answer.
- **A clean held-out set.** I wrote the 12 questions and fixed one design flaw (research mode could retrieve less than
  plain RAG; fixed by always searching the original question) after seeing 4 of them. Treat the +0.125 as a development
  result.
- **Persistent weak spots.** "Which of his projects have private code?" stays at recall 0.25 in 2 of 3 runs: the site says
  "Repository private" / "not mine to publish", which neither keyword nor embedding search connects to "private code".
  Research mode does not fix retrieval vocabulary gaps; query expansion or better chunk text would.
