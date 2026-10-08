# INC-014: the hard stop would have failed at the moment it was needed (found by testing it on a throwaway project)

**Detection.** The billing hard stop (a budget publishes the cost; a small service detaches billing from the project at the limit) passed
every test I could run against Twin itself: below the limit nothing happens, another budget's message is ignored, a stranger gets 403.
What none of those showed was whether the guard's own identity could really detach billing. Running the guard's code as that identity
against a throwaway project, linked to the same billing account, returned 403.

**Cause, in two parts.**
1. *Real bug.* The detach function first read the project's billing state and only then wrote. The guard's identity (Project Billing
   Manager, deliberately the narrowest role that can attach or detach) may write but not read, so the read failed and nothing was
   ever detached.
2. *A false alarm worth recording.* The next two runs failed with the same 403 for a different reason: a newly granted role had not yet
   propagated (it takes up to a few minutes). After a 150-second wait the same call succeeded, and a second call was idempotent.
   Without the longer wait I would have chased a permission problem that did not exist.

**Fix.** The detach is a single write; the response is checked (`billingEnabled` must be false) and a leftover link raises, so Pub/Sub
retries. Verified end to end with the guard's real function as the narrow identity, and checked independently with my own credentials
before deleting the throwaway project. Tests cover "one write, no read first" and "fails loudly if billing is still on".

**A second trap, avoided before it happened.** This month's cost before credits was already about EUR 11.83 (covered by trial credit).
A EUR 5 limit measured on that basis would have switched Twin off at the next message. The budget counts cost *after* credits instead
(what would be charged to the card), and the guard ran in dry-run mode until the first real message under the new limit showed a cost
of EUR 0.00.

**Not tested, on purpose.** The real detach on this project, because it would take Twin down. It is documented in the README, with the
steps to re-link billing.

**Lesson.** A safety mechanism is only tested when its own identity does the dangerous thing. Test that on something disposable,
verify the result with a second, independent credential, and distinguish "denied" from "not yet allowed".
