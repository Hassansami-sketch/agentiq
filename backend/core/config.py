from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8",
        case_sensitive=True, extra="ignore"
    )

    APP_NAME: str = "AgentIQ"
    APP_URL: str = "http://localhost:3000"
    API_URL: str = "http://localhost:8000"
    ENVIRONMENT: str = "development"

    # ── AI — Groq ─────────────────────────────────────────────────────────────
    # Supports both naming conventions from the instruction:
    #   API_PROVIDER=groq  API_KEY=gsk_...   (short form)
    #   GROQ_API_KEY=gsk_...                 (explicit form — takes priority)
    API_PROVIDER: str = "groq"          # currently only groq is supported
    API_KEY: str = ""                   # alias — used if GROQ_API_KEY is blank
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    GROQ_MAX_TOKENS: int = 4096
    GROQ_MAX_TOOL_ITERATIONS: int = 15

    @property
    def groq_api_key_resolved(self) -> str:
        """Returns GROQ_API_KEY if set, falls back to API_KEY alias."""
        return self.GROQ_API_KEY or self.API_KEY

    # Neon PostgreSQL
    DATABASE_URL: str = ""       # asyncpg  (postgresql+asyncpg://...)
    DATABASE_URL_SYNC: str = ""  # psycopg2 (postgresql://...)   for Alembic

    # FIX: Reduced defaults to stay within Neon free-tier connection limits.
    # Neon free = 10 connections max. API (5+2=7) + Worker (2) = 9. Safe.
    DB_POOL_SIZE: int = 5        # was 10
    DB_MAX_OVERFLOW: int = 2     # was 5
    DB_POOL_RECYCLE: int = 240   # was 300 — recycle before Neon's 5-min idle kill

    # FIX: Worker gets its own smaller pool — it opens one connection per job, not per request
    DB_WORKER_POOL_SIZE: int = 2
    DB_WORKER_MAX_OVERFLOW: int = 1

    # Redis / Celery
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # Auth
    SECRET_KEY: str = ""
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 hours

    # Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_STARTER: str = ""
    STRIPE_PRICE_PRO: str = ""
    STRIPE_PRICE_ENTERPRISE: str = ""

    # Limits
    RATE_LIMIT_PER_MINUTE: int = 60
    MAX_CONCURRENT_JOBS_PER_ORG: int = 3

    # Worker behaviour
    # FIX: Batch commit every N companies instead of after each one.
    # 500 companies / 10 = 50 commits instead of 500.
    WORKER_COMMIT_BATCH_SIZE: int = 10

    # SMTP / Email outreach
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = False          # True = SMTP_SSL port 465; False = STARTTLS port 587
    SMTP_FROM_EMAIL: str = ""           # defaults to SMTP_USERNAME if blank
    SMTP_FROM_NAME: str = "AgentIQ"
    SMTP_RATE_LIMIT: int = 30           # global default: emails per minute

    # Monitoring
    SENTRY_DSN: str = ""
    LOG_LEVEL: str = "INFO"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
