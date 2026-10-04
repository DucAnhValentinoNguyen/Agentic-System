# INC-001: first deploy answered every question without a model (real)

**When:** 2 Oct 2026, first three deploys. **Impact:** 100% of turns in degraded mode on v1, then about a third on v2. No visitors affected (preview behind `?twin=1`).

**Detection.** My own smoke test: the reply was "The language model is unavailable right now..." (the
degraded-mode text). Cloud Run logs, filtered on `llm_call` with an error, showed the cause directly.

**Causes (two, found in sequence).**
1. v1: `403 Permission aiplatform.endpoints.predict denied` and a 403 on the embedding call at startup. The
   `roles/aiplatform.user` binding for the new service account had not propagated yet; embeddings were only
   warmed once at startup, so they stayed off for the instance's life.
2. v2: `429 Resource exhausted` from Vertex on the first calls after a cold start, with a single provider
   configured, so one throttled call meant a degraded answer.

**Fixes.** Lazy embedding retry (at most once a minute); one short retry on 429; a second Vertex route
(the other Gemini model, separate quota); later a cross-vendor fallback (Groq).

**Confirmation (same four-question smoke test on the live URL).** v1: 2/2 turns degraded. v2: 1/3 degraded.
v3: 0/4 degraded, with 3 of 11 model calls still hitting 429 and being absorbed by the retry and second route.

**What the system did right:** degraded mode returned the three closest site sections instead of an error,
and never invented an answer.
