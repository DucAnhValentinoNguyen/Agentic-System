# INC-003: the LLM judge produced a false improvement (measurement incident)

See `docs/experiment.md`. In short: the first judged run said claim verification cut unsupported claims from
12.8% to 7.3%. Reading the flagged items showed the judge was flagging abstentions and contact suggestions, and
six judge failures were being scored as zeros. After fixing the judge and re-scoring the same answers, both
variants sit at about 2.5% and the benefit disappears. Lesson: read the judge's disagreements before trusting an
aggregate, and calibrate it against human labels (still pending, see `evals/calibrate.py`).

## Other bugs found by my own tests (not incidents)
- **Booking retry returned "slot taken".** Testing against the real calendar: re-submitting the same booking
  failed the free-slot check because the first attempt's event made the slot look busy. A retry after a timeout
  would have reported a false failure. Fix: reconcile by deterministic event id first; Google's reuse of deleted
  ids (409) handled; 5 tests with a fake calendar.
- **Per-turn cost under-counted.** Node updates overwrote each other's model-call records. Found because the
  A/B cost looked impossible (B cheaper than A). Fixed in the server and the experiment runner.
- **Structured output truncated.** The fast model pretty-printed JSON and a 200-token cap cut it off; every
  structured call now retries once on the stronger model and logs the failure (`structured_parse_failed`).
