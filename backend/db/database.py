import sys, os
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from db.base import Base

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import text

from core.config import settings

logger = logging.getLogger(__name__)

# ── Neon-tuned connection args ────────────────────────────────────────────────
# Neon serverless uses a proxy that kills idle connections after ~5 min.
# pool_pre_ping re-checks the connection before handing it out.
# pool_recycle forces a new connection before Neon kills the old one.
# connect_args sets per-query timeout so hung queries don't block forever.
#
# FIX: Neon free tier has a ~10 connection hard cap.
# Old config: pool_size=10 + max_overflow=5 = 15 from API alone → exhaustion.
# New config: pool_size=5 + max_overflow=2 = 7 from API, leaves room for worker.

import ssl

_ssl_context = ssl.create_default_context()
_ssl_context.check_hostname = False
_ssl_context.verify_mode = ssl.CERT_NONE

_CONNECT_ARGS = {
    "server_settings": {"application_name": "agentiq-api"},
    "command_timeout": 30,
    "ssl": _ssl_context,
}

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=(settings.ENVIRONMENT == "development"),

    # FIX: Reduced pool size to stay within Neon free-tier connection limits
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,

    pool_pre_ping=True,             # Re-validate connection before use
    pool_recycle=settings.DB_POOL_RECYCLE,   # Force-recycle before Neon proxy kills it
    pool_timeout=10,                # FIX: Don't wait more than 10s for a free connection
    connect_args=_CONNECT_ARGS,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency — yields a DB session per request.

    FIX: Only commits if the request handler didn't already commit.
    Read-only GET requests no longer trigger a needless COMMIT round-trip.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            # commit is a no-op if nothing was written (read-only requests)
            if session.in_transaction():
                await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager version for use outside FastAPI (Celery worker)."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            if session.in_transaction():
                await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """Create all tables. Safe to call on every startup (CREATE TABLE IF NOT EXISTS)."""
    from db.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("✓ All tables verified/created in Neon PostgreSQL")


async def check_db_health() -> bool:
    """Quick liveness probe — used by /health endpoint."""
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error("DB health check failed: %s", e)
        return False


