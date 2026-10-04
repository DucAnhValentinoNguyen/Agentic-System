# Data schema (Firestore, Native mode, `europe-west3`)

Storage is best-effort: a failed write is logged (`store_failed`) and never breaks a conversation. Writes
only happen in Cloud Run (`FIRESTORE_ENABLED=true`); local runs and evals never write. `schema_version` is 1 on
every document. **Every collection carries `expire_at`, and a Firestore TTL policy deletes documents 30 days
after creation**, which is what the privacy page promises.

## `turns/{trace_id}`: one per answered turn
| field | type | notes |
|---|---|---|
| `ts`, `expire_at` | timestamp | created / TTL deadline (+30 days) |
| `session_id` | string | random per browser, not an account |
| `question`, `answer` | string | visitor text and the reply |
| `intent` | string | question / booking / message / smalltalk / off_topic / injection |
| `variant` | string | online A/B assignment (`A` plain RAG, `B` + verification) |
| `citations` | array<string> | anchors the answer cited (validated against what was retrieved) |
| `sources` | array<{anchor,title,text}> | the retrieved text the judge scores against (text capped at 1,500 chars) |
| `research_steps` | int | >0 when research mode ran |
| `latency_ms`, `cost_usd` | number | per turn, all model calls included |
| `providers`, `fallback`, `degraded` | array / bool / bool | which providers served it; any fallback; no-model mode |
| `feedback` | int or null | thumbs: 1, -1 (set from the widget) |
| `judge_pending` | bool | true for answered, non-degraded question turns with sources |
| `judge` | map or null | `{claims, unsupported, unsupported_text}` written by the online job |

Composite index: `turns(judge_pending ASC, ts DESC)` serves the judge job ("newest unjudged first").

## `audit/{auto-id}`: one per booking or message attempt (no personal data)
`kind` (booking | message), `status` (created / sent / already_* / limit / failed ...), `ts`, `expire_at`.

## `counters/{YYYY-MM-DD}`: durable daily caps
`bookings` (max 5/day), `messages` (max 20/day), `expire_at`. Incremented with `firestore.Increment`, so the
caps survive restarts and scale-out; if Firestore is unreachable the service falls back to a per-process count.

## Online scoring
- **Thumbs:** the widget posts `{trace_id, value}` to `/v1/feedback` (rate-limited per IP); stored on the turn and
  attached to the Langfuse trace as `user_feedback`.
- **LLM judge:** Cloud Run Job `twin-online-judge`, scheduled every 3 hours by Cloud Scheduler, scores up to 6 pending
  turns per run against their stored sources (same judge as the offline experiment) and writes `judge` plus
  `unsupported_claims` / `has_unsupported` scores to the Langfuse trace. The judge's free tier allows 200k tokens a day,
  which is why the cadence is low (see INC-005).
