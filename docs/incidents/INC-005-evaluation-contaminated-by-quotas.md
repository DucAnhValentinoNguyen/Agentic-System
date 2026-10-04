# INC-005: my evaluation numbers were contaminated by provider quotas

**Detection.** Two runs of the *same* plain-RAG pipeline on the same 4 questions gave evidence recall 0.92 and 0.58;
the research variant showed "degraded" answers (no model available) in both runs. The logs held 63 and 66 lines of
`429 Resource exhausted` (Vertex Gemini) plus Groq token-limit errors.

**Root causes (three).**
1. The research path makes 6-8 model calls per question, and the free/new-project Vertex quota throttled bursts, so
   some answers degraded and others silently moved to a weaker fallback model.
2. The Groq judge shares one 8,000 tokens-per-minute quota with the Groq fallback provider, and has a hard
   **200,000 tokens per day** cap per model. Repeated judging runs exhausted the daily cap; the runs then stalled waiting
   ~10 minutes at a time, and the online judge job failed on its first two attempts.
3. Plain RAG's own variance: the LLM-written search query differs run to run (about 0.21 recall range per question).

**Fixes.**
- The runner pauses between runs and **excludes a case from both variants** if either answer was degraded or the judge
  failed (and reports the exclusions).
- Recall, the metric I care about for retrieval, no longer needs a judge: `evals/retrieval_study.py` stops the real graph
  before the answer node and measures it deterministically with repeats.
- The online judge runs every 3 hours, 6 turns per run, to stay inside the daily quota.

**Confirmation.** The retrieval study ran 72 graph runs without rate-limit failures affecting recall; the online judge then
scored a live turn successfully (2 claims, 0 unsupported).

**Lesson.** Check what your measurement shares with the system you are measuring: here, quota. And a number that moves
between identical runs is a finding about the metric before it is a finding about the system.
