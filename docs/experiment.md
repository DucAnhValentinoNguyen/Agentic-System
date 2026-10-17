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
- **Judge calibration against human labels (7 Oct, n=47).** Duc-Anh labelled 47 of the 48 held-out answers by
  hand, blind to the judge (`evals/label_sheet.py`, `evals/calibrate.py score`). Raw agreement is 0.94, but
  Cohen's kappa is -0.03 (95% bootstrap CI -0.06 to 1.00): agreement is high only because almost every answer is
  clean. The judge flagged 2 answers and the human judged both supported (two false alarms); the human flagged 1
  the judge did not (an on-topic question refused as off-topic, which is a routing failure rather than an
  unsupported claim). So on this sample the judge's flags have no demonstrated precision, and its recall cannot
  be measured because the set contains no confirmed unsupported claim. Consequence: the "2.3% vs 2.6%
  unsupported" figures above are within the judge's false-alarm rate and should be read as "no detectable
  difference", not as measured rates. Next step: a set enriched with deliberately corrupted answers, so recall
  can be measured at all.
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

| Evidence recall (mean over 12 questions, 3 repeats each) | Run 1 (3 Oct) | Run 2 (5 Oct, after the fixes below) |
|---|---|---|
| raw question as the query (no LLM) | 0.667 | 0.639 |
| plain RAG (LLM-rewritten query, the production path) | 0.685 | 0.750 |
| **research mode** | **0.810** | **0.799** |
| research - plain | **+0.125, 95% CI [+0.051, +0.218]** | **+0.049, 95% CI [0.000, +0.104]** |

**Read this as a modest, real-looking gain, not +0.125.** Research mode itself barely moved between runs (0.810 and
0.799). What changed is the *plain* baseline (0.685 to 0.750): its recall for the same question varies by about 0.16 to
0.21 between runs because the LLM writes a different search query each time, while research mode varies by 0.08 to 0.15
since it always also searches the original question. Run 1 happened to catch plain RAG on a bad day. Pooling the two
runs the gain is roughly +0.09, and the second run's interval just touches zero. A third run would narrow it further.
Run 2 came after two changes (follow-up questions skip the planner; near-duplicate searches are dropped) and shows
neither hurt research-mode recall.

**Cost.** A throttled-but-complete dev run measured roughly +3.4 to +7.8 s median latency and up to about 2x cost per
answer (the research path makes 6-8 model calls). So production uses it only when the classifier flags a question as
complex (`RESEARCH_MODE=auto`): 12/12 multi-hop questions are flagged, 2/28 single-fact ones.

**What is NOT shown.**
- **Answer quality.** I intended to judge answers too, but the free-tier judge quota (200k tokens per day) ran out
  (INC-005); the earlier judged runs were also contaminated by provider rate limits (degraded answers, fallbacks to a
  weaker model). Better evidence does not by itself guarantee a better answer.
- **A clean held-out set.** I wrote the 12 questions and fixed one design flaw (research mode could retrieve less than
  plain RAG; fixed by always searching the original question) after seeing 4 of them. Treat the gain as a development
  result.
- **Persistent weak spots.** "Which of his projects have private code?" stays at recall 0.25 in 2 of 3 runs: the site says
  "Repository private" / "not mine to publish", which neither keyword nor embedding search connects to "private code".
  Research mode does not fix retrieval vocabulary gaps; query expansion or better chunk text would.


# Experiment 3: does an LLM reranker help? (7 Oct 2026, `evals/rerank_study.py`)

**Question.** Retrieval returns 10 passages and the answer step sees the top 5. Some are irrelevant (an answer about gnhf came
with Service Desk AI and EdgeLoop text). Would a cheap model call that picks the passages that help answer reduce that noise
without losing the right one?

**Design.** 35 questions: the 12 multi-hop ones (gold sections given) and the 23 answerable single-fact ones whose gold facts could
be located in the corpus by substring (gold sections found automatically). Candidates are fetched once per question, so the
baseline (first 5 passages) and the reranked context are exactly paired; the reranker runs twice per question. Metrics: recall of
the gold sections in the context, and noise (passages in the context that are not gold). Paired bootstrap over questions.

| Variant | Recall (baseline 0.859) | Irrelevant passages (baseline 3.60) |
|---|---|---|
| Rerank only | 0.806 (-0.053, CI [-0.137, +0.017]) | 1.43 (-2.17, CI [-2.70, -1.63]) |
| **Rerank + always keep the retrieval's top 2** | **0.858 (-0.001, CI [-0.048, +0.043])** | **1.83 (-1.77, CI [-2.20, -1.33])** |

Cost of either: about +$0.0002 per question and +1.5 s of latency (one extra fast-model call).

**Decision.** Built in as `RERANK=llm` (with the top-2 floor), **off by default**. The noise reduction is real and large and recall
is unchanged, but I have no reliable measure that a cleaner context improves the *answers* (the judge is not validated, see the
calibration above), and 1.5 s is a noticeable price on every answer. Reranking alone is not safe: on one question it kept three
passages and none was the right one.

**Limits.** 35 questions, two reranker runs each; gold anchors for 23 of them were found by substring, not by hand; recall at the
passage level, not answer quality.

**Found on the way.** `Router.complete()` raised `KeyError('text')` when a reply was cut off by its token limit (the stream's
"truncated" event has no text), crashing any structured call that hit its cap. Fixed and covered by a test.

# Experiment 4: how much history should each step see, and does a summary help? (7 Oct 2026, `evals/context_study.py`)

**Question.** Every step sees the last N messages. When a visitor refers back ("how is it trained?") to something that fell out of
that window, the follow-up can no longer be resolved. Would a running summary of the older turns fix that, and how often should it
be refreshed?

**Design.** Six scenarios: ask about a project (turn A), send k small-talk turns (k = 2, 4, 8), then ask a question that only makes
sense given turn A. The run stops before the answer; the metric is whether the section A was about is among the retrieved passages.
Configurations: history window of 2, 6 or 10 messages, with no summary, or a summary refreshed once 12 or 4 messages have fallen
out of the window. Conversations run one at a time; a conversation in which any model call failed is discarded and repeated (a first
run with three in parallel was contaminated by provider throttling and was thrown away, as in INC-005; this run discarded none).

| Configuration | gap 2 turns | gap 4 | gap 8 | mean |
|---|---|---|---|---|
| window 2, no summary | 0.50 | 0.33 | 0.50 | 0.44 |
| window 6, no summary (previous behaviour) | 0.83 | 0.83 | 0.50 | 0.72 |
| window 10, no summary | 1.00 | 0.83 | 0.50 | 0.78 |
| window 6, summary after 12 messages | 1.00 | 0.50 | 1.00 | 0.83 |
| **window 6, summary after 4 messages** | **1.00** | **1.00** | **1.00** | **1.00** |

**Reading.** A larger window only postpones the problem (window 10 still fails at 8 turns). A summary that waits for 12 older
messages leaves a gap: turns that are neither in the window nor yet summarised are lost (0.50 at a gap of 4). Refreshing after 4
messages closes it: 18 of 18 follow-ups resolved, against 13 of 18 with the plain window.

**Decision.** Default `HISTORY_WINDOW=6`, `SUMMARY_AFTER=4`. Cost: one fast-model call about every second turn once a chat has
more than 3 exchanges (about $0.0001 and 0.5 to 1 s, added before the answer is marked complete).

**Limits.** Six scenarios per cell, 18 per configuration; the filler turns are small talk, which is easier to summarise than a
real long conversation; retrieval of the right section is the metric, not answer quality.

## Not done on purpose: memory across chats
Nothing is kept per visitor between chats. There is no product reason (a visitor asks a handful of questions), and it would turn a
30-day log into long-term personal data. If it ever matters it needs an explicit consent and retention rule first.


# Experiment 5: could Twin be A/B tested online? (9 Oct 2026, `evals/ab_power.py`, `evals/ab_stats.py`)

**Question.** Experiments 1-4 are offline and paired. Twin also assigns each visitor to an arm (`assign_variant`, a hash of the session id) and
logs `variant` on every turn. Is there enough real traffic for an online test of a change to the booking flow?

**Design.** Baseline and traffic from the turn log (2-9 Oct 2026): 583 turns, 253 sessions, of which 16 touched booking and 5 completed (31%).
Most of this traffic was the author's own testing, so genuine booking visitors are assumed to be about 12 a week. Sample sizes use the
two-proportion z test (alpha 0.05, power 0.8); power and peeking are simulated.

| Lift to detect (points) | Visitors per arm | Weeks of traffic |
|---|---|---|
| +30 | 43 | 7 |
| +20 | 94 | 16 |
| +10 | 361 | 60 |
| +5 | 1,398 | 233 |

**Reading.** In four weeks the smallest detectable lift is +39 points; a true +20-point lift with 30 visitors per arm is detected 35% of the time.
Five completions of 16 starters has a 95% Wilson interval of 14% to 56%, so even the baseline is not pinned down. Looking at an A/A test ten times and
stopping at the first p < 0.05 gives a false positive 19% of the time instead of 5%.

**Decision.** No online test of the booking flow. Changes are judged with offline paired experiments, where the sample and the pairing are controlled;
the online machinery (assignment, `variant` logging, sample-ratio check, A/A test, exact paired test, cluster bootstrap, Holm correction) is in
`evals/ab_stats.py` with tests, ready for a service with real traffic. Not done: an online A/A run (needs `AB_VARIANT=auto`, a Terraform apply).


# Experiment 6: does stemming and a stop-word list improve the lexical half of retrieval? (17 Oct 2026, `evals/lexical_study.py`)

**Question.** BM25 matched exact tokens only: "marathons" did not match "marathon", "LLMs" did not match "LLM", and filler words ("what", "is", "he")
added score to the wrong sections. Would Snowball stemming and a 70-word stop list help, and does it still help once the embedding half of the
hybrid search is added?

**Design.** Retrieval only (no answer generation, no judge), k = 5, paired comparison of four analyzers on the same questions: plain (the old
tokeniser), stop words only, stemming only, both. Two question sets. *Primary*: 35 answerable questions with known gold sections, the 12 multi-hop
cases plus the single-fact cases of the 40-case fact set whose gold facts can be located in the corpus (the 5 unanswerable cases have no gold
section and are not retrievable by definition); written before this change. *Phrasing*: 16 natural questions with inflections and filler words
(for example "How fast does he run marathons?"), written after the change was proposed, so it can favour the change. Metrics: recall of the gold
sections in the top 5 and the reciprocal rank of the first gold section; paired bootstrap interval over questions.

| Hybrid search (BM25 + Vertex embeddings) | Primary recall@5 (n = 35) | Phrasing recall@5 (n = 16) |
|---|---|---|
| plain (before) | 0.837 | 0.740 |
| stop words only | 0.857 | 0.792 |
| stemming only | 0.852 | 0.938 |
| **stemming + stop words** | **0.877 (+0.039, 95% interval +0.001 to +0.086; 4 questions better, 0 worse)** | **0.958 (+0.219, +0.062 to +0.438; 4 better, 0 worse)** |

**Reading.** On the lexical half alone (no embeddings) the picture is mixed: mean reciprocal rank on the primary set rises from 0.764 to 0.871, but one
question ("Which cloud platforms and services does he list?") drops from 1.00 to 0.00 and another from 0.75 to 0.50, so stemming and stop words are
not a free win in isolation. With the embedding half in place the changes are never negative on either set. The primary-set gain is small and its
interval just excludes zero; the phrasing-set gain is large but the set is small and was written with the change in mind. Snowball does not
stem "LLMs" to "LLM" (no vowel before the "s"), so a one-line rule for vowel-less acronym plurals was added.

**Decision.** Stemming and stop words are the default (`LEXICAL_ANALYZER=stem+stop`; `plain` restores the old behaviour). Results in
`evals/results/lexical_bm25.json` and `lexical_hybrid.json`.


# Experiment 7: should Twin read the pages the site links to? (17 Oct 2026, `evals/deep_study.py`)

**Question.** Twin knew one short paragraph per section of the home page. The report PDF, the interactive demo and three results pages were links
whose targets it never read, so a question such as "what does the report say about failover?" could only be answered with "the site only summarises
this" (Option 1, which hands over the links). Would reading those pages, one level deep and on the same site, make such questions answerable, and what
would 73 extra chunks cost the questions the home page already answered?

**Design.** The corpus builder reads each same-site page that a home-page section links to (5 pages: 3 HTML results pages, the demo overview and an
8-page PDF), splits HTML at its headings (table rows keep their cells) and the PDF by page, and records the linking section and the page URL, so a
citation points at the page the fact came from. Fetching and PDF parsing happen at build time only. Same hybrid retriever (stem + stop words,
BM25 + Vertex embeddings), k = 5, paired comparison of the home-only corpus (28 chunks) against the deep corpus (101 chunks). *Deep set*: 13
questions whose answers exist only in linked pages (checked mechanically: the gold phrase occurs in no home-page chunk). *Primary* (35) and *phrasing*
(16): the earlier sets, whose gold sections are on the home page, to measure harm.

| Set | n | Recall@5 home-only | Recall@5 deep | Change (95% interval) | Questions better / worse |
|---|---|---|---|---|---|
| deep (linked-page facts) | 13 | 0.000 | **1.000** | +1.000 (+1.000 to +1.000) | 13 / 0 |
| primary (home-page facts) | 35 | 0.877 | 0.848 | -0.029 (-0.086 to +0.000) | 0 / 1 |
| phrasing | 16 | 0.958 | 0.958 | 0.000 | 0 / 0 |

Mean reciprocal rank on the primary set rose from 0.912 to 0.936; on the deep set it is 0.712.

**What went wrong on the way (and was fixed).**
1. The first deep run silently had no embeddings: embedding all 101 sections in one request exceeded Vertex's 20,000-token limit (HTTP 400), so
   search fell back to BM25 alone and looked worse than it was. Sections are now embedded in batches of 8 (`EMBED_BATCH`, with a test).
2. My first harness located the home-page questions' gold sections in the *deep* corpus, which graded the home-only index against sections it could
   not contain (recall 0.766 instead of 0.877). Gold sections are now located in the home-only snapshot.
3. With no countermeasure, three broad listing questions got worse (recall -0.037). A cap of two chunks per linked page did not help and cost one
   deep question, so it was removed. A 10% score discount for linked chunks (the home page is the author's own curated summary, linked pages add
   detail) removed two of the three. One was chosen after seeing the failures, so the primary-set number is somewhat optimistic.

**Remaining cost.** "Which cloud platforms and services does he list?" now returns the AWS case-study report and demo instead of the Skills section.
Defensible (the report is about cloud services) but it is a miss against the intended answer. A bigger corpus raises the chance that a long page on
one topic outranks a short curated summary; the discount lowers it, it does not remove it.

**Risks checked.** Linked pages are untrusted text like any source: a fourth poisoned-document case (an injection hidden in a PDF page) passes
with the rest of the poison suite (4/4, local server). Facts can conflict between pages; for the five pages read here no conflict was found, but nothing
checks for it automatically. The deploy gate gained six questions whose answers exist only in linked pages (48 cases run before traffic moves).

**Decision.** Deep corpus on (`python ingest/build_corpus.py`; `--no-deep` rebuilds the old one). The retrieval service now embeds 101 sections at start-up
(13 requests), which lengthens a cold start by a few seconds; it does not run per question.
