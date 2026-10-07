import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gcp_project: str = "agentsystems-510414"
    vertex_location: str = "europe-west4"  # EU-pinned; both models verified there
    embed_location: str = "europe-west4"
    embed_model: str = "text-embedding-005"
    fast_model: str = "google/gemini-2.5-flash-lite"
    strong_model: str = "google/gemini-2.5-flash"

    # Optional second vendor (OpenAI-compatible). Empty = not configured.

    # Optional self-hosted model (OpenAI-compatible, e.g. Ollama on the RTX 4090). Empty = not configured.
    local_base_url: str = ""
    local_model: str = ""
    local_token_headroom: int = 0  # reasoning models (gpt-oss) need extra room for thinking tokens
    providers: str = ""  # restrict/reorder providers, e.g. "local" or "vertex,groq" (evals); empty = default
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_fast_model: str = "openai/gpt-oss-20b"
    groq_strong_model: str = "openai/gpt-oss-120b"

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    retrieval_addr: str = ""          # host:443 of the gRPC retrieval service; empty = in-process
    retrieval_audience: str = ""      # its Cloud Run URL, for the ID token
    retrieval_deadline_s: float = 3.0
    corpus_path: str = "ingest/corpus.jsonl"
    # Evals only: extra (poisoned) chunks appended to the index. Never set in production.
    extra_corpus_path: str = ""
    site_url: str = "https://ducanhvalentinonguyen.com/"
    booking_page_url: str = ""  # set via BOOKING_PAGE_URL (Terraform var); empty = no link shown
    contact_email: str = "anh.nguyen1@campus.lmu.de"
    allowed_origins: str = (
        "https://ducanhvalentinonguyen.com,https://www.ducanhvalentinonguyen.com,"
        "http://localhost:8080,http://127.0.0.1:8080"
    )

    firestore_enabled: bool = False  # on in Cloud Run only; local runs and evals never write
    research_mode: str = "auto"  # auto = plan/parallel-search for complex questions; off; force
    ab_variant: str = "auto"  # auto = hash(session) split A/B; or force "A" / "B"
    ttft_timeout_s: float = 6.0
    call_timeout_s: float = 30.0
    retry_backoff_s: float = 0.7
    throttle_cooldown_s: float = 10.0
    breaker_failures: int = 3
    breaker_open_s: float = 60.0
    max_turns_per_session: int = 30
    max_message_chars: int = 1000
    # Voice input (push-to-talk): recordings are transcribed in memory and never stored or logged.
    max_audio_seconds: int = 30
    max_audio_b64_chars: int = 1_400_000
    audio_per_minute: int = 6   # per IP
    daily_audio_clips: int = 500
    rate_per_minute: int = 12
    daily_budget_usd: float = 2.0
    monthly_budget_usd: float = 12.0  # a second, longer fence for the credits: persisted, shared by all instances
    daily_turns_per_ip: int = 150  # per network address and day, persisted (the per-chat cap resets with a new chat)
    fault_inject: str = ""  # e.g. "vertex_429" — fault-injection exercises only


settings = Settings()

# The Langfuse SDK reads os.environ, not our settings object.
for _k, _v in (("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key),
               ("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key),
               ("LANGFUSE_HOST", settings.langfuse_host)):
    if _v:
        os.environ.setdefault(_k, _v)
