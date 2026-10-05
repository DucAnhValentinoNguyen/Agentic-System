# INC-010: a latency alert that was both too coarse and, partly, right

**Detection.** An email "p95 > 8000 ms for 10 min, alert closed" for 12:26-12:36 UTC on 5 Oct.

**Two causes.**
1. *The metric was too coarse.* The log-based latency metric used buckets that grow by 50% (boundaries near 5.8 s and 8.7 s).
   Cloud Monitoring estimates a percentile from the bucket, so any latency between 5.8 and 8.7 s was reported as 8.7 s and
   tripped the 8 s threshold. The worst real turn in that window was 8.3 s and the p95 about 6.5 s.
2. *Booking steps really were slow.* The slow turns were my live booking tests: 5.5 to 8.3 s per step, against about 2.3 s for an
   ordinary question. Each calendar call started a new MCP subprocess (process start, imports, Google credentials): measured
   about 1.05 s per call locally, and a booking step makes one to three of them on top of the model call.

**Fix.**
- One long-lived MCP session per server (`Calendar._worker`): a single background task owns the subprocess, calls are queued to it,
  and it restarts only after two failures in a row. Measured locally: 1.05 s per call down to 0.14-0.3 s after the first.
  4 tests (one session shared by many calls, content-block parsing, a tool error does not kill the session, a dead session restarts).
- Latency buckets at 1.2x steps (boundaries near 6.6, 7.9 and 9.5 s), so "8 s" means 8 s.

**Confirmation on the live service** (same booking conversation as before, real calendar, test event deleted): steps took 0.2 to
4.0 s, slowest 4.0 s versus 8.3 s before; the slowest step included starting the calendar process for the first time after the deploy.

**Limits.** One run; the first call after each deploy or restart still pays the process start. Model-call time (1-2 s per step, more
under Gemini throttling) is untouched. The alert only fires with at least 5 turns in the window (INC-009).
