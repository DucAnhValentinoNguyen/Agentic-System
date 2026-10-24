# INC-017: the cost guard detached billing and Twin went offline (19 Oct 2026)

**Found by.** The audit log of the Google Cloud project shows `DisableResourceBilling`, called by the `twin-billing-guard` service account, at
21:33:24 UTC on 19 Oct (23:33 in Munich). I noticed on 20 Oct, when the billing page showed the cost close to the 5 EUR cap, and confirmed that the
guard had already fired the evening before. From then on the health check returned 500, chat returned 503, and every request failed with "billing is
disabled for this project". The site itself stayed up (GitHub Pages); only the assistant was gone.

**What the logs say.**
- First charge after credits: 15 Oct, 15:34 UTC, 0.02 EUR (the guard logs every budget notification, about every 20 to 40 minutes).
- Until then every check read "cost 0.00 is below the limit 5.00", because the guard counts cost *after* credits (INC-014).
- From 15 Oct to 19 Oct the cost reached 5 EUR: about 1.2 EUR a day.

**Cause.** Four things, three of them already known.
1. **The free credits ended around 15 Oct.** They had hidden the real bill: 20.54 EUR of usage in the first twelve days of October showed as 0.00 charged.
2. **Cloud Run was billed for CPU all the time** (INC-015: 73% of usage, the model only 7%). The fix, `cpu_idle = true` on all three services, was written and
   planned on 12 Oct, but applying it needs a manual approval in the deploy pipeline. It was not applied; on 20 Oct the live services still showed no
   `cpu_idle`. I asked for the approval twice and did not follow up when the credits were about to end.
3. **My cost estimate was wrong.** "1 to 2 EUR a month" was computed from model spend alone; it left out the services that were the real bill.
4. **A design gap in the hard stop.** The plan was "at the cap, switch to the free open model". But the stop works by detaching billing from the project
   that also hosts the application, and Cloud Run does not run without billing. The free fallback never had a chance to answer; the whole service stopped.

**What worked.** The guard did what it was built for. It fired within about four days of the first real charge, the extra cost stayed close to the 5 EUR
limit, and nothing ran away. Without it the bill would have continued at about 1.2 EUR a day.

**Fix and changes.**
- Decision on 20 Oct: leave Google Cloud and host on AWS, with the language model as an open-weight model on a local GPU (plan in `docs/AWS_MIGRATION.md`).
- The new stop will stop the *instance* (AWS Budgets action) while the fallback model path stays outside the thing being stopped, and a second budget will
  alert on gross usage while credits are still paying, so a hidden cost shows up early.
- Weekly numbers will report cost by service, not only model spend.
- The Google project stays unbilled on purpose: the Calendar API used for bookings still works without billing, which was checked on 20 Oct.

**Lessons.**
- A cost control that measures only what you watch is blind to the rest. Credits are a second mask: measure gross usage while they last.
- An approval gate is a safety feature and a delay. A fix that matters to cost needs an owner and a date, not only a plan.
- A hard stop that disables its own host cannot degrade gracefully. Put the fallback and the brake on different sides of the line.

**What I do not know.** The cost on AWS has not been measured; the migration is planned and not built. I have not yet checked whether anything else in the
Google project (images, storage) keeps costing while billing is detached.
