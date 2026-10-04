# Blueprint: Twin, an operated agent for ducanhvalentinonguyen.com

*What we set out to build, why, what we actually built, and where the two differ.*

## 1. Goal
Build one deployed system that exercises the whole loop of production agent engineering: agents (multi-step reasoning,
tools, MCP, memory, retrieval, structured outputs), rigorous evaluation, a Python backend with streaming, DevOps on GCP,
and scale (latency, cost, routing, graceful failure). Built as a 3-day hackathon, then extended.

## 2. Decision log
| Decision | Why | Alternative considered |
|---|---|---|
| Build an assistant on his own site ("Twin") | The content already exists, so facts are checkable ground truth and evaluation can be objective; it stays useful afterwards | A coaching/tutoring loop with two domain packs: needs new domains with subjective rubrics, too much for 3 days |
| One hypothesis fixed before building | Keeps the experiment honest | Free-form tuning |
| Managed models first, local GPU as a benchmark | A home GPU and tunnel must not be on the serving path | RTX 4090 as the primary provider |
| Gemini (EU Vertex) primary, a second Gemini route, Groq open models as cross-vendor fallback | Real fallback across two vendors; EU residency for the primary | Single provider |
| Firestore instead of Cloud SQL | Serverless, free tier, TTL retention built in, minutes to provision | Postgres + pgvector + Alembic (the original plan) |
| Langfuse only | One observability platform done properly | Also LangSmith / Braintrust |
| Production runs plain RAG; verify node off | Experiment showed no benefit for ~2 s and ~68% more cost | Ship verification |

## 3. Architecture (as built)
See the diagram in the [README](../README.md). In words:
- **Widget** (TypeScript, one script tag, Shadow DOM) talks over WebSocket: `delta`, `retract`, `step`, `choices`, `done`.
- **agent-api** (FastAPI + LangGraph on Cloud Run): classify, then either `retrieve`, or **research mode** (`plan`, parallel
  `research` via `Send`, `reflect`, one more round if gaps), then `answer` (streamed), optional `verify`, `finalize`
  (citations validated). Booking and message branches collect details, show them back, and `interrupt()` for an explicit yes.
- **Model router:** tiers, circuit breaker, TTFT timeout, 429 retry and cooldown, cost accounting, truncation guard;
  Vertex Gemini (EU) -> second Gemini route -> Groq `gpt-oss`; retrieval-only degraded mode as the last rung.
- **retrieval** (gRPC, separate Cloud Run service): BM25 + Vertex embeddings, 3 s deadline, in-process fallback.
- **MCP server** (stdio subprocess): calendar free/busy and booking (idempotent), send-mail tool.
- **Firestore:** turns, audit, durable daily caps, TTL 30 days, one composite index ([SCHEMA.md](SCHEMA.md)).
- **Online scoring:** thumbs from the widget; a scheduled Cloud Run Job runs the LLM judge on sampled turns.
- **Ops:** Langfuse traces, JSON logs, 6 log metrics, 6 alert policies, dashboard, billing budget; Terraform; GitHub Actions CI.

## 4. Capability coverage
| Capability | Where |
|---|---|
| Multi-step reasoning, orchestration (LangGraph) | research mode; booking/message flows with `interrupt()` |
| Tool calling, MCP | stdio MCP server, tools discovered via `langchain-mcp-adapters` |
| Memory | LangGraph thread checkpoints (in-memory, one instance); durable turn history in Firestore |
| Retrieval | gRPC service, BM25 + embeddings, validated citations |
| Structured outputs | Pydantic models, strong-model retry, parse-failure counter |
| Eval datasets, offline + online scoring, LLM-as-judge | `evals/` (security 36, facts 40, multi-hop 12), thumbs, scheduled judge; calibration pending |
| A/B tests | offline paired bootstrap; stable online assignment by session (currently forced to A) |
| Tracing, dashboards | Langfuse + Cloud Monitoring |
| Python services, streaming, gRPC, DB/schema | FastAPI, WebSocket protocol, gRPC retrieval, Firestore schema |
| Containers, Cloud Run, CI/CD, IaC | Dockerfile (multi-stage), Terraform, GitHub Actions; deploys via `scripts/deploy.sh` |
| Logs, metrics, alerts, own incidents | 6 metrics, 6 alerts; incidents INC-001..005 |
| Latency, cost, routing, fallbacks, rate limits | router, budgets, per-IP/session caps, load test at 30 concurrent sessions |
| TypeScript | the widget |

## 5. Plan vs. what happened
| Planned | Actual |
|---|---|
| Cloud SQL + pgvector + Alembic | Firestore; retrieval is in-process BM25 + embeddings held in memory |
| Kimi as second vendor | Groq (no Kimi key); Gemini second route added |
| OTel traces to Cloud Trace | Not built; Langfuse is the trace layer |
| Hybrid retrieval via gRPC, MCP over a remote server | gRPC yes; MCP as a stdio subprocess |
| Judge calibrated on 50 human labels | Export and scorer built; **labels still pending** |
| Automatic canary deploys with WIF | Scripted deploys; CI checks only |
| A/B on live traffic | Machinery built; offline result only; prod forced to A |
| 4090 as a local provider | Optional local provider + benchmark (see `docs/model-comparison.md` when finished) |
| 3 days | Built over more, extended with research mode, messaging, persistence |

## 6. Known limits and open items
Conversation state is in memory; no automated CD; judge uncalibrated; research mode helps retrieval (+0.125 recall) but
answer quality is unmeasured and it can't fix vocabulary gaps; free-tier judge quota (200k tokens/day) limits online scoring.
Open: human labels to calibrate the judge, a local-model benchmark write-up, automated deploys.
