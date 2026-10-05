# INC-009: the fallback alert kept emailing me (a noisy alert I designed)

**Detection.** Duc-Anh: "it keeps telling me", with the Cloud Monitoring alerts page. The API showed one policy, "more than
20% of turns needed a fallback provider", opening and closing about a dozen times in three days, with one open, plus a
smaller number of "p95 latency above 8 s" alerts.

**Diagnosis from the logs (last 30 hours).** 181 turns, 54 of 443 model calls (12%) hit Vertex `429 Resource exhausted`;
every turn still got an answer (0 degraded, 0 failed). Traffic was 1-3 turns in most hours and then bursts: 104 turns in one
hour, almost all of them my own voice-study run (103 `voicestudy-*` sessions, 28 of them via a fallback model).

**Cause.** A ratio alert on a tiny denominator. With 1-3 turns in a window, one fallback is 33% or 100%, so it flips on and
off. The signal underneath is real but minor: the project's Gemini quota is low, so test bursts get throttled, and the router
absorbs it by design (INC-002).

**Fix.** The fallback-share alert now needs the share above 20% **and** at least 10 turns in the 15-minute window; the
latency alert needs p95 above 8 s **and** at least 5 turns in 10 minutes (`combiner = AND` in `infra/terraform/monitoring.tf`).
A ratio is only reported when there is enough traffic for it to mean something.

**Not done, deliberately.** I did not raise the Gemini quota or add another region as an extra route (europe-west1 and
europe-west9 serve the same models, so it is the next step if real traffic grows). Real traffic is low and users were never
affected, so this was an alerting problem, not a capacity one.

**Lesson.** Alert on user impact (degraded or failed turns, which stayed at zero) and put a volume floor under every ratio.
Also: my own load and evaluation runs against the live service are traffic, and they can page me.
