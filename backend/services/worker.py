"""
services/worker.py  — UPDATED
Celery background task runner for enrichment jobs.

Changes from v2 original:
  1. SoftTimeLimitExceeded is now caught → jobs marked 'partial' not stuck as 'running'
  2. _mark_job_partial() added for graceful timeout handling
  3. enqueue_job() helper wraps apply_async with Redis-down guard
  4. Single task now handles the missing-key case cleanly

Common errors + fixes:
  celery.exceptions.NotRegistered: 'services.worker.enrich_job_task'
    Fix: Start worker with:  celery -A services.worker worker -Q enrichment,priority -l info
    The -A flag must point to the module containing celery_app

  redis.exceptions.ConnectionError: Error 111 connecting to localhost
    Fix: Redis not running. Start: redis-server
         Or check CELERY_BROKER_URL in .env

  SoftTimeLimitExceeded raised but not in try/except:
    Fix: Already handled below — jobs get status='partial' after 1h

  Worker prefetch causes memory bloat:
    Fix: worker_prefetch_multiplier=1 is already set (one task at a time)

Debug commands:
  # Start worker (from backend/ directory):
  celery -A services.worker worker -Q enrichment,priority --loglevel=info --concurrency=2

  # Monitor tasks in real time:
  celery -A services.worker flower  (then open http://localhost:5555)

  # Check active tasks:
  celery -A services.worker inspect active

  # Purge all queued tasks (DANGER in production):
  celery -A services.worker purge
"""
import sys, os
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

import asyncio, logging
from datetime import datetime
from uuid import UUID

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import update, text
from core.config import settings

import os
import ssl
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL")

app = Celery(
    "agentiq",
    broker=REDIS_URL,
    backend=REDIS_URL
)

# 🔥 THIS IS THE IMPORTANT PART
app.conf.broker_use_ssl = {
    "ssl_cert_reqs": ssl.CERT_NONE
}

app.conf.redis_backend_use_ssl = {
    "ssl_cert_reqs": ssl.CERT_NONE
}













celery_app = Celery(
    "agentiq",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,             # re-queue on worker crash
    task_reject_on_worker_lost=True, # don't silently drop on worker death
    worker_prefetch_multiplier=1,    # one task at a time (long-running jobs)
    task_routes={
        "services.worker.enrich_job_task":    {"queue": "enrichment"},
        "services.worker.single_enrich_task": {"queue": "priority"},
    },
)

logger = logging.getLogger(__name__)


def run_async(coro):
    """Run an async coroutine from a sync Celery task."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Helper: enqueue with Redis-down guard ─────────────────────────────────────

def enqueue_job(job_id: str, organization_id: str) -> str:
    """
    Dispatch enrich_job_task to Celery.
    Returns the Celery task ID.

    Raises RuntimeError if broker is unreachable — call site should
    catch this and return HTTP 503 to the client.

    Common error:
      redis.exceptions.ConnectionError → Redis not running
      Fix: redis-server  (local) or check CELERY_BROKER_URL (production)
    """
    try:
        result = enrich_job_task.apply_async(
            args=[job_id, organization_id],
            queue="enrichment",
        )
        logger.info("Job %s enqueued → Celery task %s", job_id, result.id)
        return result.id
    except Exception as e:
        logger.error("Failed to enqueue job %s: %s", job_id, e)
        raise RuntimeError(f"Job queue unavailable: {e}. Is Redis running?") from e


# ── Celery Task Definitions ───────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="services.worker.enrich_job_task",
    max_retries=3,
    soft_time_limit=3600,        # 1h → SoftTimeLimitExceeded
    time_limit=3900,             # 65min hard kill
)
def enrich_job_task(self, job_id: str, organization_id: str):
    """
    Main batch enrichment task.

    Error handling:
      SoftTimeLimitExceeded → job marked 'partial' (results saved so far are kept)
      Exception             → job marked 'failed', retried up to max_retries=3
      Worker crash          → task re-queued by broker (task_acks_late=True)
    """
    logger.info("Starting job %s (attempt %d/%d)", job_id, self.request.retries + 1, self.max_retries + 1)
    try:
        run_async(_run_job(self, job_id, organization_id))
    except SoftTimeLimitExceeded:
        # FIX: was unhandled — job would stay as 'running' forever after 1h
        logger.warning("Job %s hit 1h soft time limit — marking as partial", job_id)
        run_async(_mark_job_partial(
            job_id,
            "Time limit exceeded (1 hour). Partial results are saved. "
            "Re-launch the job with the remaining companies to continue."
        ))
    except Exception as exc:
        logger.error("Job %s failed (attempt %d): %s", job_id, self.request.retries + 1, exc)
        if self.request.retries < self.max_retries:
            logger.info("Retrying job %s in 60s...", job_id)
            raise self.retry(exc=exc, countdown=60)
        # Final failure — no more retries
        run_async(_mark_job_failed(job_id, str(exc)))
        raise


@celery_app.task(
    bind=True,
    name="services.worker.single_enrich_task",
    max_retries=2,
    soft_time_limit=120,
)
def single_enrich_task(self, organization_id: str, company_name: str, website: str = None):
    """Single-company enrichment outside of a job context."""
    try:
        return run_async(_run_single(organization_id, company_name, website))
    except SoftTimeLimitExceeded:
        logger.warning("Single enrich timed out for %s", company_name)
        return None
    except Exception as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30)
        logger.error("Single enrich failed for %s: %s", company_name, exc)
        return None


# ── Job Runner ────────────────────────────────────────────────────────────────

async def _run_job(task, job_id: str, organization_id: str):
    """
    Processes a batch enrichment job company-by-company.

    Design principles for reliability:
    - Fresh DB session per company (Neon kills idle sessions after 5min)
    - Atomic SQL increments (safe under retries)
    - Batch commits every WORKER_COMMIT_BATCH_SIZE (reduces Neon round-trips)
    - Individual company failure doesn't abort the whole job
    """
    from db.database import get_db_context
    from db.models import Job, EnrichmentResult, UsageLog
    from agents.enrichment_agent import EnrichmentAgent

    agent = EnrichmentAgent()

    # Phase 1: Mark running
    async with get_db_context() as db:
        job = await db.get(Job, UUID(job_id))
        if not job:
            logger.error("Job %s not found in DB — aborting", job_id)
            return

        job.status = "running"
        job.started_at = datetime.utcnow()
        job.celery_task_id = task.request.id
        await db.commit()

        companies = job.input_data.get("companies", [])
        websites  = job.input_data.get("websites", {})
        total     = len(companies)

    logger.info("Job %s: processing %d companies", job_id, total)

    completed = 0
    failed    = 0

    # Phase 2: Enrich — one fresh session per company
    for i, company in enumerate(companies):
        website_hint = websites.get(company) or websites.get(str(i))

        async with get_db_context() as db:
            try:
                await agent.enrich_company(
                    db=db,
                    job_id=UUID(job_id),
                    organization_id=UUID(organization_id),
                    company_name=company,
                    website_hint=website_hint,
                )
                completed += 1

                db.add(UsageLog(
                    organization_id=UUID(organization_id),
                    job_id=UUID(job_id),
                    action="enrichment",
                    credits_consumed=1,
                    tokens_used=0,
                    model_used=settings.GROQ_MODEL,
                    extra_data={"company": company, "source": "batch_job"},
                ))

                # Atomic SQL increment — safe under retries
                await db.execute(
                    text("""
                        UPDATE jobs
                        SET completed_items = completed_items + 1,
                            progress_pct    = (completed_items + 1)::float / total_items * 100,
                            credits_used    = credits_used + 1
                        WHERE id = :job_id
                    """),
                    {"job_id": job_id},
                )

            except Exception as e:
                failed += 1
                logger.error("Enrichment failed for '%s': %s", company, e)

                db.add(EnrichmentResult(
                    job_id=UUID(job_id),
                    organization_id=UUID(organization_id),
                    input_name=company,
                    status="failed",
                    error_message=str(e)[:500],
                    enriched_at=datetime.utcnow(),
                ))

                await db.execute(
                    text("""
                        UPDATE jobs
                        SET failed_items = failed_items + 1,
                            progress_pct = (completed_items + failed_items + 1)::float / total_items * 100
                        WHERE id = :job_id
                    """),
                    {"job_id": job_id},
                )

            # Batch commit
            if (i + 1) % settings.WORKER_COMMIT_BATCH_SIZE == 0 or (i + 1) == total:
                await db.commit()
                logger.info(
                    "Job %s: %d/%d (%.0f%%)",
                    job_id, i + 1, total, ((i + 1) / total) * 100,
                )

        # Brief pause between companies — Groq rate limit respect
        await asyncio.sleep(2)

    # Phase 3: Mark complete
    async with get_db_context() as db:
        await db.execute(
            text("""
                UPDATE jobs
                SET status       = 'completed',
                    completed_at = NOW(),
                    progress_pct = 100.0
                WHERE id = :job_id
            """),
            {"job_id": job_id},
        )
        await db.commit()

    logger.info(
        "Job %s complete: %d enriched, %d failed / %d total",
        job_id, completed, failed, total,
    )


async def _mark_job_failed(job_id: str, error: str):
    """Mark a job as permanently failed."""
    from db.database import get_db_context
    async with get_db_context() as db:
        await db.execute(
            text("""
                UPDATE jobs
                SET status        = 'failed',
                    error_message = :error,
                    completed_at  = NOW()
                WHERE id = :job_id AND status IN ('queued', 'running')
            """),
            {"job_id": job_id, "error": error[:1000]},
        )
        await db.commit()
    logger.error("Job %s marked as failed: %s", job_id, error[:200])


async def _mark_job_partial(job_id: str, message: str):
    """
    Mark a job as partially complete — used when the 1h soft time limit is hit.
    Results collected so far are kept. User can re-launch with remaining companies.
    """
    from db.database import get_db_context
    async with get_db_context() as db:
        await db.execute(
            text("""
                UPDATE jobs
                SET status        = 'partial',
                    error_message = :msg,
                    completed_at  = NOW()
                WHERE id = :job_id AND status = 'running'
            """),
            {"job_id": job_id, "msg": message},
        )
        await db.commit()
    logger.warning("Job %s marked as partial: %s", job_id, message)


async def _run_single(organization_id: str, company_name: str, website: str = None):
    """Single-company enrichment without a job context."""
    from db.database import get_db_context
    from agents.enrichment_agent import EnrichmentAgent

    agent = EnrichmentAgent()
    async with get_db_context() as db:
        result = await agent.enrich_company(
            db=db,
            job_id=None,                   # no FK — leads to nullable job_id
            organization_id=UUID(organization_id),
            company_name=company_name,
            website_hint=website,
        )
        return str(result.id)
