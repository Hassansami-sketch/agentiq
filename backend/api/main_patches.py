"""
api/main_patches.py
══════════════════════════════════════════════════════════════════════
INSTRUCTIONS: These are the ADDITIONS to make to your existing main.py.
Do NOT replace main.py — apply the numbered patches below.

Patch 1: Add to imports at top of main.py
Patch 2: Update lifespan() function
Patch 3: Replace rate_limit middleware
Patch 4: Register exception handlers after app = FastAPI(...)
Patch 5: Update /health endpoint
══════════════════════════════════════════════════════════════════════
"""

# ══════════════════════════════════════════════════════════════════════
# PATCH 1 — Add to imports
# ══════════════════════════════════════════════════════════════════════
PATCH_1_IMPORTS = """
import asyncio as _asyncio   # already present — just noting alias usage
from core.startup_check import validate_config
from core.exception_handlers import register_exception_handlers
from core.rate_limiter import check_rate_limit
from core.job_recovery import stuck_job_cleanup, job_health_summary
"""


# ══════════════════════════════════════════════════════════════════════
# PATCH 2 — Replace lifespan() with this improved version
# ══════════════════════════════════════════════════════════════════════
PATCH_2_LIFESPAN = """
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Init DB tables
    await init_db()

    # 2. Validate config — raises RuntimeError if fatal issues found
    #    This causes the process to exit with a clear error message
    #    instead of crashing mysteriously on the first API call.
    validate_config()

    # 3. Log startup summary
    logger.info(
        "AgentIQ v2 started | env=%s | model=%s | db=%s | smtp=%s",
        settings.ENVIRONMENT,
        settings.GROQ_MODEL,
        "✓" if settings.DATABASE_URL else "MISSING",
        "✓" if settings.SMTP_HOST else "not configured",
    )
    yield
    logger.info("AgentIQ API v2 shutting down")
"""


# ══════════════════════════════════════════════════════════════════════
# PATCH 3 — Replace the rate_limit middleware (after app = FastAPI(...))
# ══════════════════════════════════════════════════════════════════════
PATCH_3_RATE_LIMITER = """
@app.middleware("http")
async def rate_limit(request: Request, call_next):
    # Skip rate limiting for health/docs endpoints
    if request.url.path in ("/health", "/docs", "/redoc", "/openapi.json"):
        return await call_next(request)

    ip = request.client.host if request.client else "unknown"
    allowed, retry_after = await check_rate_limit(ip, settings.RATE_LIMIT_PER_MINUTE)

    if not allowed:
        return Response(
            content=f'{{"detail":"Rate limit exceeded — max {settings.RATE_LIMIT_PER_MINUTE} req/min"}}',
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": str(retry_after)},
        )
    return await call_next(request)
"""


# ══════════════════════════════════════════════════════════════════════
# PATCH 4 — Register global exception handlers
# Add this line immediately after: app = FastAPI(...)
# ══════════════════════════════════════════════════════════════════════
PATCH_4_EXCEPTION_HANDLERS = """
register_rexception_handlers(app)
"""


# ══════════════════════════════════════════════════════════════════════
# PATCH 5 — Replace /health endpoint with enhanced version
# ══════════════════════════════════════════════════════════════════════
PATCH_5_HEALTH = """
@app.get("/health", tags=["System"])
async def health_check(db: AsyncSession = Depends(get_db)):
    '''
    System health check — use for uptime monitoring, load balancer probes.

    Returns:
      200 {"status": "ok"}   — everything healthy
      503 {"status": "degraded", "issues": [...]}  — partial failure

    Checks:
      - Database connectivity (SELECT 1)
      - AI key configured
      - Connection pool stats
      - Stuck job count

    Debug:
      curl http://localhost:8000/health
    '''
    from db.database import check_db_health, engine

    issues = []
    details = {}

    # DB check
    db_ok = await check_db_health()
    if not db_ok:
        issues.append("database_unreachable")
    else:
        # Connection pool stats (useful for debugging exhaustion)
        pool = engine.pool
        details["db_pool"] = {
            "size": pool.size(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
        }

    # AI key check
    details["ai_key"] = "configured" if settings.groq_api_key_resolved else "missing"
    if not settings.groq_api_key_resolved:
        issues.append("ai_key_missing")

    # Job health
    try:
        job_stats = await job_health_summary(db)
        details["jobs"] = job_stats
        if job_stats.get("stuck", 0) > 0:
            issues.append(f"{job_stats['stuck']}_stuck_jobs")
    except Exception as e:
        logger.warning("Health: job summary failed: %s", e)

    # SMTP status
    details["smtp"] = "configured" if settings.SMTP_HOST else "not_configured"

    status_code = 503 if issues else 200
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "degraded" if issues else "ok",
            "version": "2.0.0",
            "environment": settings.ENVIRONMENT,
            "issues": issues,
            **details,
        }
    )
"""


# ══════════════════════════════════════════════════════════════════════
# PATCH 6 — Replace enqueue call in POST /jobs route
# Find the existing apply_async call and replace with:
# ══════════════════════════════════════════════════════════════════════
PATCH_6_SAFE_ENQUEUE = """
# In POST /jobs (or wherever you dispatch enrichment jobs):
from services.worker import enqueue_job

try:
    celery_task_id = enqueue_job(str(job.id), str(current_user.organization_id))
    job.celery_task_id = celery_task_id
    await db.commit()
except RuntimeError as e:
    # Redis/broker is down — mark job failed immediately so UI doesn't show it stuck
    job.status = "failed"
    job.error_message = str(e)
    await db.commit()
    raise HTTPException(
        status_code=503,
        detail=f"Job queue unavailable. Is Redis running? {e}"
    )
"""
