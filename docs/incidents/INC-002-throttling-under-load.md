# INC-002: Vertex throttling under load, and a controlled test of the fix

**Detection.** Load test (`evals/load_test.py`, 30 concurrent sessions x 4 turns, live service): all 120
turns succeeded and none degraded, but first-token latency p95 was **10.09 s** (p50 4.86 s). The router's own
status showed 55 failed model calls out of the last 200 and 28 turns on a fallback. At 10 sessions x 5 turns
the same service was fine (p95 3.59 s, 50/50 ok).

**Root cause.** Intermittent 429s from Vertex under concurrency. The circuit breaker never opened (it needs
consecutive failures and successes kept resetting it), so every request paid its own failed call plus a retry
back-off before falling back.

**Fix.** Load shedding: after a 429 a provider is skipped for 10 s (`THROTTLE_COOLDOWN_S`); the last provider in
the chain is always tried. Covered by unit tests.

**Why a controlled test.** After deploying the fix, Vertex did not throttle at all (0 of 200 calls failed;
p95 4.30 s), so that run proves nothing about the fix. I added a probabilistic fault injector
(`FAULT_INJECT=vertex_429@0.4`, raises a real `RateLimitError`) and compared cooldown off vs on, same image,
same 30x4 load:

| | cooldown off | cooldown on |
|---|---|---|
| first token p50 / p95 | 2.99 s / 4.34 s | 2.28 s / 3.61 s |
| failed model calls (of 200) | 66 | 1 |
| all 120 turns ok, 0 degraded | yes | yes |

**Caveats.** One run per arm. Injected 429s return instantly, so a real 429 (a network round trip) would make
the benefit larger. Trade-off: while the primary is cooling down, "strong" answers come from the second route
(Gemini Flash-Lite), which is a lower-quality model; I have not measured the quality cost. An always-failing
provider is a different case: there the existing circuit breaker already opens after 3 failures.
