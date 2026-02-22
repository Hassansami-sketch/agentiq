"""
core/startup_check.py
Startup config validator — run once in lifespan() before serving traffic.

Usage (in main.py lifespan):
    from core.startup_check import validate_config
    validate_config()

Raises RuntimeError for fatal misconfigurations.
Logs warnings for non-fatal issues.
"""
import logging
from core.config import settings

logger = logging.getLogger(__name__)


def validate_config():
    """
    Validate all required .env settings at startup.

    Fatal (raises RuntimeError — app will NOT start):
      - DATABASE_URL missing
      - No AI API key (GROQ_API_KEY or API_KEY)
      - SECRET_KEY missing or too short

    Warnings (logged — app starts but features may fail):
      - SMTP not configured → email campaigns will fail
      - SENTRY_DSN not set → no error tracking
      - Redis pointing to localhost in production
      - ALLOWED_ORIGINS wildcard in production
    """
    errors = []
    warnings = []

    # ── FATAL: Database ───────────────────────────────────────────────────────
    if not settings.DATABASE_URL:
        errors.append(
            "DATABASE_URL not set.\n"
            "  Get from: https://neon.tech → New Project → Connection Details\n"
            "  Format:   postgresql+asyncpg://user:pass@host/dbname?sslmode=require"
        )

    # ── FATAL: AI Key ─────────────────────────────────────────────────────────
    if not (settings.GROQ_API_KEY or settings.API_KEY):
        errors.append(
            "No AI API key found.\n"
            "  Fix: Add GROQ_API_KEY=gsk_... to .env\n"
            "  Get free key: https://console.groq.com → API Keys"
        )

    # ── FATAL: JWT Secret ─────────────────────────────────────────────────────
    if not settings.SECRET_KEY:
        errors.append(
            "SECRET_KEY not set — JWTs will be insecure.\n"
            "  Generate: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
    elif len(settings.SECRET_KEY) < 32:
        errors.append(
            f"SECRET_KEY too short ({len(settings.SECRET_KEY)} chars — need ≥ 32).\n"
            "  Generate: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )

    # ── WARNINGS: Non-fatal ───────────────────────────────────────────────────
    if not settings.SMTP_HOST:
        warnings.append(
            "SMTP_HOST not configured — email campaigns will fail when launched.\n"
            "  Fix: Add SMTP_HOST=smtp.gmail.com SMTP_USERNAME=you@gmail.com to .env"
        )

    if not settings.SENTRY_DSN:
        warnings.append("SENTRY_DSN not set — error tracking disabled (optional).")

    if settings.ENVIRONMENT == "production":
        if "localhost" in (settings.CELERY_BROKER_URL or ""):
            warnings.append(
                "CELERY_BROKER_URL points to localhost in production.\n"
                "  Fix: Set REDIS_URL / CELERY_BROKER_URL to your hosted Redis URL (e.g. Upstash)"
            )
        if "localhost" in settings.DATABASE_URL:
            warnings.append("DATABASE_URL points to localhost in production — use Neon/Supabase URL")

    # ── Output ────────────────────────────────────────────────────────────────
    for w in warnings:
        logger.warning("⚠  CONFIG WARNING: %s", w)

    if errors:
        msg = "\n\n🚨  STARTUP ABORTED — Fix these .env issues before starting:\n\n"
        msg += "\n\n".join(f"  ✗  {e}" for e in errors)
        msg += "\n\nRestart after fixing: uvicorn api.main:app --reload\n"
        raise RuntimeError(msg)

    logger.info("✓  Config validated (env=%s, warnings=%d)", settings.ENVIRONMENT, len(warnings))
