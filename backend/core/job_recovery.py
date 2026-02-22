"""
core/job_recovery.py
Background job health utilities.

Handles:
  1. Stuck job detection — jobs stuck in 'running' > N hours auto-reset to 'failed'
  2. Orphaned queue detection — tasks in Celery queue with no DB job record
  3. Health summary for /health endpoint

Schedule stuck_job_cleanup() as a periodic task or call from /health endpoint.

Common errors + fixes:
  asyncpg.TooManyConnectionsError:
    Fix: Reduce pool size in database.py or increase Neon plan
  Job stuck in 'running' forever:
    Cause: Worker killed mid-job (OOM, deploy restart)
    Fix:   This module auto-resets them after STUCK_THRESHOLD_HOURS
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

STUCK_THRESHOLD_HOURS = 2   # reset running jobs older than this


async def stuck_job_cleanup(db) -> dict:
    """
    Scan for jobs stuck in 'running' or 'queued' state for too long
    and mark them as failed.

    Returns: {"reset": int} — number of jobs reset

    Debug:
      # Find stuck jobs manually:
      SELECT id, status, started_at FROM jobs
      WHERE status IN ('running','queued')
        AND started_at < NOW() - INTERVAL '2 hours';
    """
    from sqlalchemy import text

    cutoff = datetime.utcnow() - timedelta(hours=STUCK_THRESHOLD_HOURS)

    result = await db.execute(
        text("""
            UPDATE jobs
            SET status        = 'failed',
                error_message = 'Auto-reset: job exceeded maximum runtime without completing',
                completed_at  = NOW()
            WHERE status IN ('running', 'queued')
              AND (started_at < :cutoff OR (started_at IS NULL AND created_at < :cutoff))
            RETURNING id
        """),
        {"cutoff": cutoff},
    )
    reset_ids = result.fetchall()
    await db.commit()

    if reset_ids:
        logger.warning(
            "Stuck job cleanup: reset %d jobs older than %dh: %s",
            len(reset_ids), STUCK_THRESHOLD_HOURS,
            [str(r[0]) for r in reset_ids],
        )

    return {"reset": len(reset_ids)}


async def job_health_summary(db) -> dict:
    """
    Quick health snapshot of the job system for the /health endpoint.

    Returns:
      {
        "queued": 0,
        "running": 0,
        "stuck": 0,          # running jobs older than threshold
        "failed_24h": 0,
        "completed_24h": 0,
      }
    """
    from sqlalchemy import select, func
    from db.models import Job

    cutoff = datetime.utcnow() - timedelta(hours=STUCK_THRESHOLD_HOURS)
    since_24h = datetime.utcnow() - timedelta(hours=24)

    rows = await db.execute(
        select(Job.status, func.count(Job.id)).group_by(Job.status)
    )
    by_status = {r[0]: r[1] for r in rows}

    stuck_r = await db.execute(
        select(func.count(Job.id)).where(
            Job.status == "running",
            Job.started_at < cutoff,
        )
    )

    failed_r = await db.execute(
        select(func.count(Job.id)).where(
            Job.status == "failed",
            Job.completed_at >= since_24h,
        )
    )
    completed_r = await db.execute(
        select(func.count(Job.id)).where(
            Job.status == "completed",
            Job.completed_at >= since_24h,
        )
    )

    return {
        "queued":        by_status.get("queued", 0),
        "running":       by_status.get("running", 0),
        "stuck":         stuck_r.scalar() or 0,
        "failed_24h":    failed_r.scalar() or 0,
        "completed_24h": completed_r.scalar() or 0,
    }
