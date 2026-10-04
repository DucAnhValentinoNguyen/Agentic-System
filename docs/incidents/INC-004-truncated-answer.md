# INC-004: answers cut off mid-sentence (found by Duc-Anh on the live site)

**Detection.** A visitor-style test, "tell me what is on this page", returned text that stopped at
"...featuring". Not caught by my suites: they test short factual questions.

**Diagnosis.** The server log already held the truncated answer (461 characters) and the model call had
succeeded, so it was not a streaming or widget bug. Token counts on 253 recent answer calls: median 188, max
496, and 5 calls (about 2%) at 480 or more against a `max_tokens` of 500. Gemini's hidden "thinking" tokens
count against that cap, so a broad, summary-style question can exhaust it in the middle of a sentence.

**Fix.** `max_tokens` 500 to 1500 for answers (answers stay 2-5 sentences, so typical cost is unchanged), 400/500
to 800 for the structured calls. Guard: the router reports `finish_reason == "length"` as a `truncated` event;
the answer node then trims to the last whole sentence (keeping its citation markers), retracts the streamed text,
and logs `answer_truncated`. Three tests cover the router signal and the trimming.

**Confirmation.** Five long-answer questions on the live service (v14), including the one that broke: 5/5
ended on a complete sentence (855 to 2,447 characters).

**Gap.** My eval sets had no broad "summarize everything" questions. Worth adding them to `facts.jsonl`.
