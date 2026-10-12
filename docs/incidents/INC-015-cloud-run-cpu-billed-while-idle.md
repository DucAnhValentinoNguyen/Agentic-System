# INC-015: Cloud Run billed CPU all the time an instance was alive (12 Oct 2026)

**Found by.** Looking at the billing report by product after the free credits were about to end. Credits had hidden it: October 1-12 showed
EUR 20.54 of usage and EUR 0.00 charged.

**What the report said.** Cloud Run EUR 18.35 (73% of all usage), Artifact Registry EUR 4.07, Gemini EUR 1.83, Cloud Storage EUR 0.61,
everything else under EUR 0.15. The model, which I had been watching and capping, was about 7% of the usage.

**Cause (read from the running services through the API, not guessed).**
1. The three Cloud Run services had no `cpu_idle` setting, so CPU was **always allocated**: an instance is billed for its whole lifetime
   (including the idle minutes before it is shut down), not only while it serves a request. The one other service on the account that
   was configured with `cpuIdle: true` is billed per request.
2. `twin-retrieval` had `min_instance_count = 1` until about 7 Oct 23:00, so one instance with always-on CPU ran around the clock.
3. The billing account is shared by four projects, so part of the Cloud Run figure may belong to another project's services; the
   split needs Reports grouped by project.

**Why my own controls did not see it.** The spend counters, the daily and monthly caps and the EUR 5 hard stop all measured **model**
cost. The hard stop watches cost after credits, which was zero. Infrastructure cost was never part of the daily numbers, and my estimate
of "EUR 1-2 a month" was written from the model figures alone.

**Fix.** `cpu_idle = true` on all three services (CPU is billed only while a request or an open chat connection is being served; a chat
WebSocket counts as a request). Retrieval was already back to zero minimum instances. Applied through Terraform behind the approval gate.

**Check after the fix.** Billing, Reports, group by Project and Service: Cloud Run for `agentsystems-510414` should fall to cents per day.
Not yet measured; this entry is updated with the before and after daily figures.

**What I would change in the process.** Report cost by product (not only model cost) in the weekly numbers, and set the budget alert
on gross usage as well as on cost after credits, so a spike shows up while credits still cover it.
