# Twin: move off Google Cloud to AWS, with a self-hosted open model on the RTX 4090

Plan written 20 Oct 2026. Status is tracked at the end of this file.

## Context
- **Why:** On 19 Oct 2026 at 23:33 CEST the €5 hard stop detached billing from the GCP project (audit log: `DisableResourceBilling` by
  `twin-billing-guard`). Twin has been offline since then (API 500, chat 503). The cause was Cloud Run billing always-on CPU (INC-015), whose fix was never
  applied, plus the trial credits ending around 15 Oct (about €1.2 a day of real cost).
- **Decision:** leave GCP and host Twin on **AWS** (Frankfurt, paid from AWS credits) for the experience. The language model is an **open-weight model on this
  machine's RTX 4090**, reached from AWS over a private tunnel. **Bedrock is the only fallback**, so data stays in the EU. Voice stays from day one. Booking
  keeps Google Calendar. The interview moved to next week, so this is done properly (gate, Terraform, cost controls), not as a quick hack.
- **Facts checked (read-only):**
  - The AWS account works from the CLI in eu-central-1, but through **root access keys**, which step 0 replaces.
  - Credits have offset all usage since July.
  - The GPU is idle (24 GB free); Ollama has `gemma4:26b`, `gpt-oss:20b` and an abliterated Qwen.
  - Bedrock in eu-central-1 serves Qwen3-32B and Qwen3-235B-A22B (on demand), gpt-oss-20b/120b, Kimi K3 (inference profile), Titan Text Embeddings V2 and Cohere embeddings.
  - The Google Calendar API **still works with billing disabled**: it read 69 free slots through the OAuth client in project 1058744794873.

## Target architecture
```
browser widget ──WSS──> api.ducanhvalentinonguyen.com
                          Caddy (TLS) on one EC2 t3.small, eu-central-1
                          └─ agent-api container (FastAPI + LangGraph, retrieval in-process, SQLite on EBS)
                               ├─ LLM: Qwen3 on the RTX 4090 (Ollama) ──Tailscale, outbound-only both sides──> workstation
                               │      fallback: the same Qwen3-32B on Bedrock (OpenAI-compatible "Mantle" endpoint, signed with the instance role)
                               ├─ voice: faster-whisper on the 4090 (same tunnel); if the GPU is unreachable the mic says "please type"
                               ├─ embeddings: Titan Text Embeddings V2 on Bedrock (document vectors computed at build time)
                               ├─ booking: Google Calendar + Gmail API via the existing OAuth client (token in SSM Parameter Store)
                               └─ traces: Langfuse (unchanged); nightly SQLite backup to S3
cost control: AWS Budget action stops the instance at a hard limit; app-level Bedrock spend cap; gross-usage alert while credits pay
```

## Model choice (with reasoning, to be confirmed by measurement)
- **Requirement:** one model serves both tiers. It must fit in 24 GB alongside Whisper. It must produce reliable JSON for the classifier and the booking
  extractor, ground its answers with citations, abstain, handle English and German, and resist injection.
- **Kimi is out locally:** Kimi K2/K3 are about 1T-parameter models. No Kimi model that fits 24 GB is a sensible chat model.
- **The abliterated Qwen is out:** its safety training is removed, which is unsuitable for a public assistant.
- **Leading candidate: Qwen3-32B** (Q4_K_M, about 20 GB). It is the same weights Bedrock serves in Frankfurt, so the fallback answers like the primary and the
  prompts are tested once. **Challenger: Qwen3-30B-A3B** (mixture of experts, about 18 GB, faster, weaker). **Reference: gemma4:26b**, which already scored best in
  Twin's own benchmark (0.875 fact coverage against Gemini's 0.812, 33/33 security, 0 JSON failures, p50 3.7 s). gpt-oss-20b scored 0.646 with 5/76 JSON
  failures and is not a candidate. Gemma is not on Bedrock in Frankfurt, so choosing it means a different-family fallback.
- **How the winner is picked:** `evals/model_bench.py` (exists, supports `PROVIDERS=local` and GPU power) on all three, plus the booking flow with the real
  extractor. Criteria in order:
  1. security cases pass,
  2. abstention 12/12,
  3. JSON failures,
  4. fact coverage,
  5. latency p95,
  6. VRAM with Whisper loaded.
  Qwen3-32B is chosen unless it loses clearly on 1–4.
- **Judge** (online scoring): `gpt-oss-120b` on Bedrock, a different family from Qwen, also in the EU (replaces Groq).

## Booking: options considered, decision
| Option | Verdict |
|---|---|
| **Keep the Google Calendar + Gmail API with the existing OAuth client in the unbilled GCP project** | **Chosen.** Works today without billing (checked); zero code change; token moves to SSM Parameter Store. Rule: never delete project `agentsystems-510414` and never re-link billing. |
| New OAuth client in a fresh, billing-free GCP project | Cleaner separation; do it later if the old project ever becomes a liability (one consent re-run with `scripts/calendar_consent.py`). |
| Booking-page link only | Loses in-chat booking; fallback only. |
| CalDAV / Cal.com self-hosted | Large change; the calendar is Google anyway. |
Also check that the OAuth consent screen is "In production". In "Testing" mode refresh tokens expire after 7 days.

## Steps (each ends with a check)
**0. Safety first (lesson of INC-015 and INC-017)**
- **You:** create an IAM Identity Center user (or an IAM admin user) with MFA, add MFA on root, and **delete the root access keys**. I then use a named CLI profile.
- **You:** read the credit balance, the expiry date and the eligible services on the credits page, and tell me.
- **Me (Terraform):** before any compute exists, a monthly AWS Budget on actual cost with alerts at 50/80/100% and a **budget action that stops the
  EC2 instance** at the hard limit (proposed $25). A second budget on gross usage alerts while credits hide the cost. Plus Cost Anomaly Detection.
- **Done when:** the root keys are gone and the budgets show in the console.

**1. Model bake-off on the 4090 (Experiment 9)**
- Pull `qwen3:32b` and `qwen3:30b-a3b`, run `evals/model_bench.py` for each plus gemma4:26b, and record VRAM and power. Then run the same bench against Bedrock Qwen3-32B
  (a few cents) to confirm the fallback matches, and check Cost Explorer the next day: the charge must not appear under AWS Marketplace, or else
  credits may not cover it.
- **Done when:** the model is chosen with numbers in `docs/experiment.md`.

**2. Workstation serving**
- Ollama listens on the loopback interface only (`OLLAMA_HOST`), so the model server is reachable only through the private tunnel.
- Keep-alive for the chosen model; a small faster-whisper service (`services/stt-local/`, about 60 lines, large-v3-turbo); Tailscale on the workstation with
  an ACL that lets only the EC2 node reach the Ollama and Whisper ports. Both as systemd services that restart on boot.
- **Done when:** from another tailnet device, a chat completion and a transcription work, and nothing is reachable on the public interface.

**3. Make the app cloud-neutral (tests stay green, everything runs locally end to end)**
- `gateway/router.py`: remove `VertexProvider`. The local Ollama provider (existing `OpenAICompatProvider`) is primary. Add a `BedrockProvider` (OpenAI SDK
  against `bedrock-mantle.eu-central-1.api.aws`, requests signed with SigV4 from the instance role, no static key). `paid=True`, existing spend caps.
  Update `PRICES`.
- `retrieval.py` + `ingest/build_corpus.py`: embedding backend Titan V2. Document vectors are computed at build time and stored in the corpus file, so there
  are no embedding calls at start-up, which removes the 13-request warm-up. The query is embedded per question, with BM25-only as the existing fallback. Re-run
  `evals/lexical_study.py` and `evals/deep_study.py` against the Vertex numbers (0.848 / 0.958 / 1.000), as Experiment 10.
- `store.py` + `checkpoint.py`: replace Firestore with SQLite. LangGraph's own async SQLite checkpointer for chat state; tables for turns, audit,
  counters and the per-IP day counts; a 30-day retention job. One instance, so no shared store is needed.
- `stt.py`: call the local Whisper service. On failure, the existing "couldn't transcribe, please type" message. Re-run `evals/voice_study.py` (it was 11.5% WER).
- `main.py` / `config.py`: drop the GCP wiring and `billing_guard.py` (replaced by the budget action); secrets from SSM Parameter Store at start-up.
  `online_judge.py` becomes a nightly cron with the Bedrock judge.
- Tests: replace the Firestore fakes with SQLite in-memory; all 205 tests plus new provider and STT tests pass. Then run the whole stack locally with Docker against
  the 4090 and Bedrock.

**4. AWS infrastructure (Terraform in `infra/aws/`, state in S3 with locking)**
- Default VPC; a security group with only 80/443 open (no SSH; shell via SSM Session Manager).
- EC2 t3.small (x86, so images built on GitHub's x86 runners run unchanged), 20 GB gp3, Elastic IP.
- Instance role, least privilege: invoke only the Qwen3-32B, gpt-oss-120b and Titan V2 model ARNs; read `/twin/*` parameters; pull from ECR; write the
  backup bucket. ECR repository keeping 5 images.
- Cloud-init installs Docker, Caddy and Tailscale (auth key from SSM).
- **Done when:** `terraform apply` creates it all, and the box joins the tailnet and serves Caddy's default page over HTTPS.

**5. CI/CD with the same gate as before**
- GitHub OIDC → IAM role (keyless). Build → push to ECR → SSM Run Command on the instance: start the **candidate** container on :8081 behind
  `candidate.api…`, run the 48 gate cases from the runner, then swap and stop the old one.
- The Terraform workflow keeps plan-on-push and apply-after-approval.
- Remove the GCP workflows.

**6. Cutover**
- **You:** add the DNS A record `api.ducanhvalentinonguyen.com` → Elastic IP.
- **Me:** re-sync the widget to the new URL; update the privacy page (messages processed on your own hardware in Munich and in AWS Frankfurt; voice
  transcribed locally; Google Calendar for bookings); push the site.
- **Smoke tests:** a question, a real booking plus cancel on your calendar, voice, the gate, and the poison suite on a local server.

**7. Measure, document, clean up**
- After one week: Cost Explorer by service; GPU uptime and fallback rate from the logs.
- Write INC-017 (the hard stop fired, and what it taught) and Experiments 9 and 10. Update README, BLUEPRINT and MODEL_CARD.
- GCP: leave the project unbilled (needed for the Calendar OAuth client) and archive the GCP Terraform. Optionally delete Cloud Run services and images where
  the API allows it without billing.

## Cost (estimate, check against the credits)
| Item | Per month |
|---|---|
| EC2 t3.small, Frankfurt, always on | about $17–18 |
| EBS 20 GB gp3 + Elastic IP (public IPv4) | about $2 + $3.6 |
| ECR, S3 backups, SSM, CloudWatch basics | under $1 |
| Bedrock fallback (Qwen3-32B) + Titan embeddings + judge | capped in the app at $3 (most traffic is local) |
| **Total** | **about $25**, fixed and predictable; the hard limit stops the instance above it |
GPU electricity is borne by the workstation (about 0.1–0.3 Wh per answer in the earlier benchmark).

## Risks and how the plan handles them
- **Workstation downtime** (reboot, network, maintenance): the router's breaker sends traffic to Bedrock Qwen3-32B, and voice says "please type". The fallback rate is
  measured in step 7.
- **Model cold load** after idle: keep-alive (the GPU is otherwise free). The first token after a reload is measured.
- **Bedrock credits might not cover third-party models:** checked in step 1 with a $0.01 call before relying on it. The app cap and the budget action cap the damage either way.
- **The budget action reacts late** (cost data lags hours): the app-level spend cap is the fast brake, the budget action the backstop.
- **Single instance, no horizontal scaling:** acceptable at about 10 concurrent visitors (your number). Revisit with ECS if it grows.
- **The Google Calendar dependency on the old project:** documented rule. If the consent screen is in Testing mode, publish it or recreate the client.

## Verification (end to end)
- `uv run pytest -q` and `ruff` green.
- `evals/model_bench.py` results recorded.
- `evals/lexical_study.py` / `deep_study.py` within noise of the Vertex numbers.
- `evals/voice_study.py` WER reported.
- The deploy gate passes 48/48 against the AWS candidate; the poison suite passes 4/4 locally.
- Live: a question with citations, a booking created and cancelled on the real calendar, voice transcribed. With Ollama stopped on purpose, the next answer comes from
  Bedrock (`fallback` in the turn log), and voice returns "please type".
- Cost Explorer after 24 h and after 7 days matches the table; a test budget action fires at a tiny threshold before the real limit is set.

## Status log
- 20 Oct 2026: plan written. Step 0 (IAM user with MFA, credit balance) waits for the account owner.
