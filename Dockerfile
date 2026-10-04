FROM node:22-slim AS widget
WORKDIR /w
COPY widget/package.json widget/package-lock.json ./
RUN npm ci
COPY widget/ ./
RUN npm run build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_NO_DEV=1 PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY services/ services/
COPY ingest/ ingest/
COPY --from=widget /w/dist widget/dist
RUN useradd -m app
USER app
ENV PORT=8080
CMD ["sh", "-c", "exec .venv/bin/uvicorn app.main:app --app-dir services/agent-api --host 0.0.0.0 --port $PORT"]
