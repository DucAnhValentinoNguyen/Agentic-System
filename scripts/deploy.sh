#!/bin/sh
# Build, push and roll out a new image: scripts/deploy.sh v9
# Needs: docker, terraform, ADC for the AgentSystems project, and a populated .env.
set -e
cd "$(dirname "$0")/.."
TAG="${1:?usage: deploy.sh <image-tag>}"
PROJECT=agentsystems-510414
IMG="europe-west3-docker.pkg.dev/$PROJECT/twin/agent-api:$TAG"

uv run ruff check . && uv run pytest -q
docker build -q -t "$IMG" .
uv run python - <<'PY' | docker login -u oauth2accesstoken --password-stdin europe-west3-docker.pkg.dev
import google.auth, google.auth.transport.requests
c, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
c.refresh(google.auth.transport.requests.Request()); print(c.token)
PY
docker push -q "$IMG"

set -a; . ./.env; set +a
cd infra/terraform
TF_VAR_google_calendar_token="$(cat ../../token_calendar.json)" \
TF_VAR_alert_email="${ALERT_EMAIL:?set ALERT_EMAIL in .env}" \
TF_VAR_groq_api_key="$GROQ_API_KEY" \
TF_VAR_langfuse_public_key="$LANGFUSE_PUBLIC_KEY" \
TF_VAR_langfuse_secret_key="$LANGFUSE_SECRET_KEY" \
  terraform apply -input=false -auto-approve -var "image_tag=$TAG"
