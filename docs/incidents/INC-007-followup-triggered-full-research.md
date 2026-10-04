# INC-007: a vague follow-up triggered a full research plan with repeated searches

**Detection.** A screenshot of a real conversation: after "what did she do in the AWS challenge?", the follow-up
"tell me more about his solutions" showed ten step lines in the chat, several of them near-identical searches
("... generative AI race strategy solutions" twice).

**Measured before changing anything** (`evals/followup_check.py`, 6 follow-up conversations, routing only): research
mode fired on 1 of 6 follow-ups, using 7 searches of which 2 were near-duplicates; on 4 standalone complex questions
it fired 4 of 4 with 3 near-duplicates. So the problem was narrow: one vague follow-up read as a big question, plus a
planner and a reflect step that repeated searches.

**Cause.** The classifier marked "tell me more about his solutions" as complex, because the word "solutions" and the
earlier turn looked like an overview request, and nothing stopped the planner or the reflect round from re-running
searches that had already been done.

**Fix.** (1) The classifier call that already runs on every message gets one more field, `followup`: true when the
message only makes sense given earlier turns. It is the model's judgement in the same call, with no extra call and no
keyword rules. Follow-ups take the single-search path, which already uses a standalone query rewritten from the
conversation. (2) Near-duplicate searches (token-overlap similarity of 0.75 or more) are dropped within a round and
against earlier rounds; if the reflect step would only repeat, it answers with the evidence it has. 3 unit tests.

**Confirmation.** Same check after: research mode on 0 of 6 follow-ups (0 searches), still 4 of 4 on standalone
complex questions, 0 near-duplicates. Replaying the screenshot's conversation on the live service: the follow-up
answered in 4.8 s with no research steps.

**Caveats.** Six follow-ups, one run each, and I tuned against the one case that failed, so there is no held-out
check. The follow-up flag is an LLM verdict and can be wrong; a genuinely complex follow-up would now get a single
search.

**A correction found while verifying.** Re-running the retrieval study to confirm the dedupe did not hurt recall
showed research mode unchanged (0.810 to 0.799) but plain RAG's baseline higher (0.685 to 0.750), so the research gain
is +0.049 (95% CI 0.000 to +0.104), not +0.125. The earlier run had caught plain RAG on a low draw. The site card,
README and experiment write-up now report both runs. See `docs/experiment.md`.
