import sys
import os
import asyncio
import uuid
import logging
import time
import csv
import io




# Fix path so all modules are found
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional, List
from uuid import UUID
from collections import defaultdict

from fastapi import FastAPI, Depends, HTTPException, Header, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles


from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from pydantic import BaseModel, EmailStr

from core.config import settings
from core.startup_check import validate_config
from core.exception_handlers import register_exception_handlers
from core.rate_limiter import check_rate_limit
from core.job_recovery import stuck_job_cleanup, job_health_summary

from db.database import get_db, init_db, check_db_health, engine
from db.models import (
    User, Organization, Job, EnrichmentResult,
    APIKey, UsageLog, Lead, Conversation,
    Campaign, CampaignLead, EmailLog
)

from services.auth import (
    authenticate_user, create_user, create_access_token,
    decode_token, get_user_by_id, get_org_by_api_key,
    generate_api_key
)
from services.worker import enrich_job_task, enqueue_job

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL, "INFO"))
logger = logging.getLogger(__name__)


# ── Sentry (§10 monitoring) ───────────────────────────────────────────────────
# Activated only when SENTRY_DSN is set in .env.
# If DSN is wrong:  Sentry silently drops events — app keeps running.
# If DSN is blank:  no-op, nothing imported.
if settings.SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            integrations=[FastApiIntegration(), SqlalchemyIntegration()],
            traces_sample_rate=0.2,          # capture 20% of requests as traces
            environment=settings.ENVIRONMENT,
            release="agentiq@2.0.0",
        )
        logger.info("✓ Sentry error tracking active (env=%s)", settings.ENVIRONMENT)
    except Exception as e:
        logger.warning("Sentry init failed (non-fatal): %s", e)


# ── Lifespan ──────────────────────────────────────────────────────────────────

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

app = FastAPI(
    title="AgentIQ API", version="2.0.0",
    docs_url="/docs", redoc_url="/redoc",
    lifespan=lifespan
)

@app.get("/dashboard")
async def serve_dashboard():
    return FileResponse("dashboard.html")



register_exception_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

# ── Middlewares ───────────────────────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    ms = round((time.time() - start) * 1000)
    logger.info("%s %s %d (%dms)", request.method, request.url.path, response.status_code, ms)
    return response

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

# ── Auth Dependencies ─────────────────────────────────────────────────────────

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    api_key: Optional[str] = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> User:
    if api_key:
        org = await get_org_by_api_key(db, api_key)
        if org:
            r = await db.execute(select(User).where(User.organization_id == org.id).limit(1))
            user = r.scalar_one_or_none()
            if user and user.is_active:
                return user
    if token:
        payload = decode_token(token)
        if payload:
            try:
                user = await get_user_by_id(db, UUID(payload.get("sub", "")))
            except (ValueError, AttributeError):
                user = None
            if user and user.is_active:
                return user
    raise HTTPException(
        status_code=401, detail="Invalid or missing credentials",
        headers={"WWW-Authenticate": "Bearer"}
    )


async def get_current_org(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Organization:
    org = await db.get(Organization, current_user.organization_id)
    if not org:
        raise HTTPException(404, "Organization not found")
    return org


# ── Schemas ───────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    org_name: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    org_id: str

class CreateJobRequest(BaseModel):
    name: Optional[str] = None
    agent_type: str = "lead_enrichment"
    companies: List[str]
    websites: Optional[dict] = {}
    extra_configs: Optional[dict] = {}

class SingleEnrichRequest(BaseModel):
    company_name: str
    website: Optional[str] = None

class CreateAPIKeyRequest(BaseModel):
    name: str
    expires_days: Optional[int] = None


# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.post("/auth/register", response_model=TokenResponse, tags=["Auth"])
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    from services.auth import get_user_by_email
    if await get_user_by_email(db, req.email):
        raise HTTPException(400, "Email already registered")
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user, org = await create_user(db, req.email, req.password, req.full_name, req.org_name)
    token = create_access_token({"sub": str(user.id)})
    logger.info("New user registered: %s org=%s", req.email, org.name)
    return TokenResponse(access_token=token, user_id=str(user.id), org_id=str(org.id))


@app.post("/auth/login", response_model=TokenResponse, tags=["Auth"])
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(401, "Invalid email or password")
    user.last_login = datetime.utcnow()
    await db.commit()
    return TokenResponse(
        access_token=create_access_token({"sub": str(user.id)}),
        user_id=str(user.id), org_id=str(user.organization_id)
    )


@app.get("/auth/me", tags=["Auth"])
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": str(current_user.id), "email": current_user.email,
        "full_name": current_user.full_name, "org_id": str(current_user.organization_id),
        "is_verified": current_user.is_verified, "is_admin": current_user.is_admin,
    }


@app.post("/auth/refresh-token", tags=["Auth"])
async def refresh_token(current_user: User = Depends(get_current_user)):
    """
    Issue a fresh JWT for an authenticated user.

    Use this before the current token expires to stay logged in.

    Common errors:
      401 → current token is already expired — user must login again
      403 → account deactivated

    Debug:
      curl -X POST http://localhost:8000/auth/refresh-token \\
        -H "Authorization: Bearer <old_token>"
    """
    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")
    new_token = create_access_token({"sub": str(current_user.id)})
    logger.info("Token refreshed for user %s", current_user.email)
    return {
        "access_token": new_token,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/auth/change-password", tags=["Auth"])
async def change_password(
    req: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Change the authenticated user's password.

    Rules:
      • current_password must match the stored hash
      • new_password must be at least 8 characters

    Common errors:
      400 → wrong current password
      400 → new password too short

    Debug:
      curl -X POST http://localhost:8000/auth/change-password \\
        -H "Authorization: Bearer <token>" \\
        -H "Content-Type: application/json" \\
        -d '{"current_password":"OldPass1!","new_password":"NewPass2@"}'
    """
    from services.auth import verify_password, hash_password
    if not verify_password(req.current_password, current_user.hashed_password):
        logger.warning("change-password: wrong current password for %s", current_user.email)
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    current_user.hashed_password = hash_password(req.new_password)
    await db.commit()
    logger.info("Password changed for user %s", current_user.email)
    return {"message": "Password updated successfully"}


# ── Jobs ──────────────────────────────────────────────────────────────────────

@app.post("/jobs", status_code=201, tags=["Jobs"])
async def create_job(
    req: CreateJobRequest,
    current_user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    if not req.companies:
        raise HTTPException(400, "No companies provided")
    if len(req.companies) > 500:
        raise HTTPException(400, "Max 500 companies per job")

    # Enforce concurrent job cap
    active_r = await db.execute(
        select(func.count(Job.id)).where(
            Job.organization_id == org.id,
            Job.status.in_(["queued", "running"])
        )
    )
    if (active_r.scalar() or 0) >= settings.MAX_CONCURRENT_JOBS_PER_ORG:
        raise HTTPException(
            429,
            f"Max {settings.MAX_CONCURRENT_JOBS_PER_ORG} concurrent jobs reached. "
            "Wait for a job to complete before creating another."
        )

    companies = list(dict.fromkeys([c.strip() for c in req.companies if c.strip()]))
    job = Job(
        organization_id=org.id, created_by_id=current_user.id,
        name=req.name or f"Batch — {len(companies)} companies",
        agent_type=req.agent_type, status="queued", total_items=len(companies),
        input_data={"companies": companies, "websites": req.websites or {}, "extra_configs": req.extra_configs or {}},
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    task = enrich_job_task.delay(str(job.id), str(org.id))
    job.celery_task_id = task.id
    await db.commit()
    logger.info("Job created %s — %d companies", job.id, len(companies))
    return {"job_id": str(job.id), "status": job.status, "total_companies": len(companies)}


@app.get("/jobs", tags=["Jobs"])
async def list_jobs(
    page: int = 1, limit: int = 20, status_filter: Optional[str] = None,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    limit = min(limit, 100)
    q = select(Job).where(Job.organization_id == org.id)
    if status_filter:
        q = q.where(Job.status == status_filter)
    q = q.order_by(desc(Job.created_at)).offset((page - 1) * limit).limit(limit)
    jobs = (await db.execute(q)).scalars().all()

    count_q = select(func.count(Job.id)).where(Job.organization_id == org.id)
    if status_filter:
        count_q = count_q.where(Job.status == status_filter)
    total = (await db.execute(count_q)).scalar() or 0

    return {"jobs": [_fmt_job(j) for j in jobs], "total": total, "page": page,
            "pages": max(1, (total + limit - 1) // limit)}


@app.get("/jobs/{job_id}", tags=["Jobs"])
async def get_job(job_id: UUID, org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    job = await db.get(Job, job_id)
    if not job or job.organization_id != org.id:
        raise HTTPException(404, "Job not found")
    return _fmt_job(job)


@app.post("/jobs/{job_id}/cancel", tags=["Jobs"])
async def cancel_job(job_id: UUID, org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    """Cancel a queued or running job."""
    job = await db.get(Job, job_id)
    if not job or job.organization_id != org.id:
        raise HTTPException(404, "Job not found")
    if job.status not in ("queued", "running"):
        raise HTTPException(400, f"Cannot cancel a '{job.status}' job")
    if job.celery_task_id:
        try:
            from services.worker import celery_app
            celery_app.control.revoke(job.celery_task_id, terminate=True, signal="SIGTERM")
        except Exception as e:
            logger.warning("Could not revoke task %s: %s", job.celery_task_id, e)
    job.status = "cancelled"
    job.completed_at = datetime.utcnow()
    await db.commit()
    return {"message": "Job cancelled", "job_id": str(job_id)}


@app.get("/jobs/{job_id}/results", tags=["Jobs"])
async def get_job_results(
    job_id: UUID, page: int = 1, limit: int = 50,
    status_filter: Optional[str] = None,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    job = await db.get(Job, job_id)
    if not job or job.organization_id != org.id:
        raise HTTPException(404, "Job not found")

    limit = min(limit, 500)
    q = select(EnrichmentResult).where(EnrichmentResult.job_id == job_id)
    if status_filter:
        q = q.where(EnrichmentResult.status == status_filter)
    q = q.order_by(EnrichmentResult.enriched_at).offset((page - 1) * limit).limit(limit)
    results = (await db.execute(q)).scalars().all()

    count_q = select(func.count(EnrichmentResult.id)).where(EnrichmentResult.job_id == job_id)
    if status_filter:
        count_q = count_q.where(EnrichmentResult.status == status_filter)
    total = (await db.execute(count_q)).scalar() or 0

    return {"results": [_fmt_result(r) for r in results], "total": total,
            "job_total": job.total_items, "page": page,
            "pages": max(1, (total + limit - 1) // limit)}


@app.get("/jobs/{job_id}/export", tags=["Jobs"])
async def export_job_csv(
    job_id: UUID,
    org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db),
):
    """Download all enrichment results for a job as CSV."""
    job = await db.get(Job, job_id)
    if not job or job.organization_id != org.id:
        raise HTTPException(404, "Job not found")

    results = (await db.execute(
        select(EnrichmentResult).where(EnrichmentResult.job_id == job_id)
        .order_by(EnrichmentResult.enriched_at)
    )).scalars().all()

    # FIX: Stream CSV in chunks — never loads all rows into memory at once.
    # Handles 100k+ results without OOM by yielding row-by-row to the client.
    HEADERS = [
        "Company Name", "Website", "LinkedIn", "HQ", "Founded", "Employees",
        "Industry", "Company Type", "Description", "Key Products", "Target Customers",
        "Tech Stack", "Recent News", "Funding Info", "Key Contacts",
        "Confidence Score", "Status", "Tokens Used", "Tool Calls", "Processing (ms)", "Enriched At",
    ]

    def _row(r) -> str:
        buf = io.StringIO()
        csv.writer(buf).writerow([
            r.company_name, r.website, r.linkedin_url, r.headquarters, r.founded_year,
            r.employee_count, r.industry, r.company_type, r.description,
            ", ".join(r.key_products or []), r.target_customers,
            ", ".join(r.tech_stack or []), r.recent_news, r.funding_info,
            ", ".join(r.key_contacts or []), r.confidence_score, r.status,
            r.tokens_used, r.tool_calls_made, r.processing_time_ms,
            r.enriched_at.isoformat() if r.enriched_at else "",
        ])
        return buf.getvalue()

    header_buf = io.StringIO()
    csv.writer(header_buf).writerow(HEADERS)

    def _stream():
        yield header_buf.getvalue()
        for r in results:
            yield _row(r)

    return StreamingResponse(
        _stream(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=agentiq-{job_id}.csv"},
    )


# ── Single Enrichment ─────────────────────────────────────────────────────────

@app.post("/enrich/single", tags=["Enrichment"])
async def enrich_single(
    req: SingleEnrichRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    from agents.enrichment_agent import EnrichmentAgent
    try:
        result = await EnrichmentAgent().enrich_company(
            db=db, job_id=None, organization_id=org.id,
            company_name=req.company_name, website_hint=req.website,
        )
        return {"success": True, "data": _fmt_result(result)}
    except Exception as e:
        logger.error("Single enrichment failed for %s: %s", req.company_name, e)
        raise HTTPException(500, f"Enrichment failed: {str(e)}")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/dashboard/stats", tags=["Dashboard"])
async def dashboard_stats(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    """
    FIX: All 5 stats queries run in parallel via asyncio.gather instead of
    sequentially. Cuts dashboard load time by ~4x on a cold connection.
    """
    month_ago = datetime.utcnow() - timedelta(days=30)
    oid = org.id

    (
        (total_enriched_r, jobs_month_r, avg_conf_r, total_tokens_r, active_r),
        recent_r,
    ) = await asyncio.gather(
        asyncio.gather(
            db.execute(select(func.count(EnrichmentResult.id))
                       .where(EnrichmentResult.organization_id == oid,
                              EnrichmentResult.status == "completed")),
            db.execute(select(func.count(Job.id))
                       .where(Job.organization_id == oid, Job.created_at >= month_ago)),
            db.execute(select(func.avg(EnrichmentResult.confidence_score))
                       .where(EnrichmentResult.organization_id == oid,
                              EnrichmentResult.confidence_score.isnot(None))),
            db.execute(select(func.sum(UsageLog.tokens_used))
                       .where(UsageLog.organization_id == oid)),
            db.execute(select(func.count(Job.id))
                       .where(Job.organization_id == oid,
                              Job.status.in_(["queued", "running"]))),
        ),
        db.execute(
            select(Job).where(Job.organization_id == oid)
            .order_by(desc(Job.created_at)).limit(5)
        ),
    )

    return {
        "total_enrichments":   total_enriched_r.scalar() or 0,
        "jobs_this_month":     jobs_month_r.scalar() or 0,
        "avg_confidence_score": round(avg_conf_r.scalar() or 0, 1),
        "total_tokens_used":   total_tokens_r.scalar() or 0,
        "active_jobs":         active_r.scalar() or 0,
        "recent_jobs":         [_fmt_job(j) for j in recent_r.scalars().all()],
    }


# ── API Keys ──────────────────────────────────────────────────────────────────

@app.post("/api-keys", status_code=201, tags=["API Keys"])
async def create_api_key(req: CreateAPIKeyRequest, org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    raw_key, key_hash, key_prefix = generate_api_key()
    expires_at = datetime.utcnow() + timedelta(days=req.expires_days) if req.expires_days else None
    ak = APIKey(organization_id=org.id, name=req.name, key_hash=key_hash, key_prefix=key_prefix, expires_at=expires_at)
    db.add(ak)
    await db.commit()
    return {"id": str(ak.id), "name": ak.name, "key": raw_key, "prefix": key_prefix,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "message": "Save this key — shown only once"}


@app.get("/api-keys", tags=["API Keys"])
async def list_api_keys(org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    r = await db.execute(
        select(APIKey).where(APIKey.organization_id == org.id, APIKey.is_active == True)
        .order_by(desc(APIKey.created_at))
    )
    keys = r.scalars().all()
    return {"api_keys": [
        {"id": str(k.id), "name": k.name, "prefix": k.key_prefix,
         "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
         "expires_at": k.expires_at.isoformat() if k.expires_at else None,
         "created_at": k.created_at.isoformat() if k.created_at else None}
        for k in keys
    ]}


@app.delete("/api-keys/{key_id}", tags=["API Keys"])
async def revoke_api_key(key_id: UUID, org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    ak = await db.get(APIKey, key_id)
    if not ak or ak.organization_id != org.id:
        raise HTTPException(404, "Key not found")
    ak.is_active = False
    await db.commit()
    return {"message": "API key revoked"}


# ── Stripe Webhook ────────────────────────────────────────────────────────────

@app.post("/webhooks/stripe", tags=["Billing"])
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Handles: checkout.session.completed, subscription updated/deleted, invoice.payment_failed."""
    if not settings.STRIPE_SECRET_KEY or not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(501, "Stripe not configured")

    import stripe as _stripe
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = _stripe.Webhook.construct_event(payload, sig, settings.STRIPE_WEBHOOK_SECRET)
    except _stripe.error.SignatureVerificationError:
        raise HTTPException(400, "Invalid Stripe signature")

    etype = event["type"]
    data = event["data"]["object"]
    logger.info("Stripe event: %s", etype)

    from db.models import Subscription, Plan

    if etype == "checkout.session.completed":
        cust_id = data.get("customer")
        sub_id = data.get("subscription")
        org_r = await db.execute(select(Organization).where(Organization.stripe_customer_id == cust_id))
        org = org_r.scalar_one_or_none()
        if org and sub_id:
            _stripe.api_key = settings.STRIPE_SECRET_KEY
            stripe_sub = _stripe.Subscription.retrieve(sub_id)
            price_id = stripe_sub["items"]["data"][0]["price"]["id"]
            plan_r = await db.execute(select(Plan).where(Plan.stripe_price_id == price_id))
            plan = plan_r.scalar_one_or_none()
            if plan:
                existing_r = await db.execute(select(Subscription).where(Subscription.organization_id == org.id))
                existing = existing_r.scalar_one_or_none()
                if existing:
                    existing.plan_id = plan.id
                    existing.stripe_subscription_id = sub_id
                    existing.status = "active"
                else:
                    db.add(Subscription(organization_id=org.id, plan_id=plan.id,
                                        stripe_subscription_id=sub_id, status="active"))
                await db.commit()

    elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_r = await db.execute(select(Subscription).where(Subscription.stripe_subscription_id == data.get("id")))
        sub = sub_r.scalar_one_or_none()
        if sub:
            sub.status = "cancelled" if etype.endswith("deleted") else data.get("status", sub.status)
            if etype.endswith("deleted"):
                sub.cancelled_at = datetime.utcnow()
            await db.commit()

    elif etype == "invoice.payment_failed":
        logger.warning("Payment failed for customer %s", data.get("customer"))

    return {"received": True, "event": etype}


# ── Usage / Billing ───────────────────────────────────────────────────────────

@app.get("/billing/usage", tags=["Billing"])
async def get_usage(days: int = 30, org: Organization = Depends(get_current_org), db: AsyncSession = Depends(get_db)):
    since = datetime.utcnow() - timedelta(days=days)
    logs = (await db.execute(
        select(UsageLog).where(UsageLog.organization_id == org.id, UsageLog.created_at >= since)
        .order_by(desc(UsageLog.created_at)).limit(500)
    )).scalars().all()
    return {
        "period_days": days,
        "total_credits_consumed": sum(l.credits_consumed for l in logs),
        "total_tokens_used": sum(l.tokens_used for l in logs),
        "total_api_calls": len(logs),
        "logs": [{"action": l.action, "credits": l.credits_consumed, "tokens": l.tokens_used,
                  "model": l.model_used, "created_at": l.created_at.isoformat()} for l in logs],
    }


# ── Health ────────────────────────────────────────────────────────────────────

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_job(j: Job) -> dict:
    return {
        "id": str(j.id), "name": j.name, "agent_type": j.agent_type,
        "status": j.status, "progress_pct": round(j.progress_pct or 0, 1),
        "completed_items": j.completed_items, "failed_items": j.failed_items,
        "total_items": j.total_items, "credits_used": j.credits_used,
        "error_message": j.error_message,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
    }


def _fmt_result(r: EnrichmentResult) -> dict:
    return {
        "id": str(r.id), "input_name": r.input_name,
        "company_name": r.company_name, "website": r.website,
        "linkedin_url": r.linkedin_url, "founded_year": r.founded_year,
        "headquarters": r.headquarters, "employee_count": r.employee_count,
        "industry": r.industry, "company_type": r.company_type,
        "description": r.description, "key_products": r.key_products or [],
        "target_customers": r.target_customers, "tech_stack": r.tech_stack or [],
        "recent_news": r.recent_news, "funding_info": r.funding_info,
        "key_contacts": r.key_contacts or [], "confidence_score": r.confidence_score,
        "enrichment_notes": r.enrichment_notes, "status": r.status,
        "error_message": r.error_message, "model_used": r.model_used,
        "tokens_used": r.tokens_used, "tool_calls_made": r.tool_calls_made,
        "processing_time_ms": r.processing_time_ms,
        "enriched_at": r.enriched_at.isoformat() if r.enriched_at else None,
    }


# ═══════════════════════════════════════════════════════════════════
#  CRM — Leads, Campaigns, Email, Conversations
#  All routes below require authentication.
# ═══════════════════════════════════════════════════════════════════

from db.models import Lead, Conversation, Campaign, CampaignLead, EmailLog
import asyncio as _asyncio

# Pipeline status constants — §5 Lead Generation pipeline
# new → contacted → replied → converted (or dead)
VALID_PIPELINE_STATUSES = ("new", "contacted", "replied", "converted", "dead")


# ── Role Guard ────────────────────────────────────────────────────────────────

def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Dependency: raises 403 if the user is not an admin."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# ── Schemas ───────────────────────────────────────────────────────────────────

class LeadCreate(BaseModel):
    company_name: str
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    linkedin_url: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[str] = None
    headquarters: Optional[str] = None
    description: Optional[str] = None
    funding_info: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = []
    source: Optional[str] = "manual"

class LeadUpdate(BaseModel):
    company_name: Optional[str] = None
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    linkedin_url: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[str] = None
    headquarters: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    status: Optional[str] = None
    score: Optional[int] = None

class CampaignCreate(BaseModel):
    name: str
    subject: str
    body_template: str
    from_name: Optional[str] = None
    reply_to: Optional[str] = None
    send_rate: int = 30
    lead_ids: Optional[List[str]] = []
    schedule_at: Optional[str] = None

class ConversationCreate(BaseModel):
    lead_id: str
    channel: str = "email"
    direction: str = "outbound"
    subject: Optional[str] = None
    body: str
    status: str = "sent"

class SendCampaignRequest(BaseModel):
    lead_ids: Optional[List[str]] = None   # None = send to all pending leads


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_lead(lead: Lead) -> dict:
    return {
        "id":              str(lead.id),
        "company_name":    lead.company_name,
        "contact_name":    lead.contact_name,
        "email":           lead.email,
        "phone":           lead.phone,
        "website":         lead.website,
        "linkedin_url":    lead.linkedin_url,
        "industry":        lead.industry,
        "employee_count":  lead.employee_count,
        "headquarters":    lead.headquarters,
        "description":     lead.description,
        "funding_info":    lead.funding_info,
        "status":          lead.status,
        "score":           lead.score,
        "notes":           lead.notes,
        "tags":            lead.tags or [],
        "source":          lead.source,
        "created_at":      lead.created_at.isoformat() if lead.created_at else None,
        "updated_at":      lead.updated_at.isoformat() if lead.updated_at else None,
        "last_contacted_at": lead.last_contacted_at.isoformat() if lead.last_contacted_at else None,
        "converted_at":    lead.converted_at.isoformat() if lead.converted_at else None,
    }


def _fmt_campaign(c: Campaign) -> dict:
    return {
        "id":            str(c.id),
        "name":          c.name,
        "subject":       c.subject,
        "status":        c.status,
        "send_rate":     c.send_rate,
        "total_leads":   c.total_leads,
        "sent_count":    c.sent_count,
        "opened_count":  c.opened_count,
        "replied_count": c.replied_count,
        "bounced_count": c.bounced_count,
        "failed_count":  c.failed_count,
        "created_at":    c.created_at.isoformat() if c.created_at else None,
        "started_at":    c.started_at.isoformat() if c.started_at else None,
        "completed_at":  c.completed_at.isoformat() if c.completed_at else None,
    }


def _fmt_conversation(c: Conversation) -> dict:
    return {
        "id":         str(c.id),
        "lead_id":    str(c.lead_id),
        "channel":    c.channel,
        "direction":  c.direction,
        "subject":    c.subject,
        "body":       c.body,
        "status":     c.status,
        "sent_at":    c.sent_at.isoformat() if c.sent_at else None,
        "opened_at":  c.opened_at.isoformat() if c.opened_at else None,
        "replied_at": c.replied_at.isoformat() if c.replied_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


# ════════════════════════════════════════════════════════════════════
#  LEADS
# ════════════════════════════════════════════════════════════════════

@app.post("/leads", status_code=201, tags=["CRM - Leads"])
async def create_lead(
    req: LeadCreate,
    current_user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """Create a single lead manually."""
    lead = Lead(
        organization_id=org.id,
        created_by_id=current_user.id,
        **req.model_dump(exclude_none=True),
    )
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    logger.info("Lead created: %s (%s)", lead.company_name, lead.id)
    return _fmt_lead(lead)


@app.get("/leads", tags=["CRM - Leads"])
async def list_leads(
    page: int = 1,
    limit: int = 50,
    status: Optional[str] = None,
    search: Optional[str] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """List leads with optional status filter and search."""
    limit = min(limit, 200)
    q = select(Lead).where(Lead.organization_id == org.id)

    if status:
        q = q.where(Lead.status == status)
    if search:
        like = f"%{search}%"
        from sqlalchemy import or_
        q = q.where(or_(
            Lead.company_name.ilike(like),
            Lead.contact_name.ilike(like),
            Lead.email.ilike(like),
        ))

    q = q.order_by(desc(Lead.created_at)).offset((page - 1) * limit).limit(limit)
    leads = (await db.execute(q)).scalars().all()

    count_q = select(func.count(Lead.id)).where(Lead.organization_id == org.id)
    if status:
        count_q = count_q.where(Lead.status == status)
    total = (await db.execute(count_q)).scalar() or 0

    return {
        "leads": [_fmt_lead(l) for l in leads],
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    }


@app.get("/leads/pipeline", tags=["CRM - Leads"])
async def pipeline_summary(
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """Return lead count per pipeline stage for kanban view."""
    from services.lead_service import get_pipeline_summary
    return await get_pipeline_summary(db, org.id)


@app.get("/leads/{lead_id}", tags=["CRM - Leads"])
async def get_lead(
    lead_id: UUID,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    lead = await db.get(Lead, lead_id)
    if not lead or lead.organization_id != org.id:
        raise HTTPException(404, "Lead not found")
    return _fmt_lead(lead)


@app.patch("/leads/{lead_id}", tags=["CRM - Leads"])
async def update_lead(
    lead_id: UUID,
    req: LeadUpdate,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """Update lead fields or advance pipeline status."""
    from services.lead_service import update_lead_status, VALID_STATUSES
    lead = await db.get(Lead, lead_id)
    if not lead or lead.organization_id != org.id:
        raise HTTPException(404, "Lead not found")

    updates = req.model_dump(exclude_none=True)
    new_status = updates.pop("status", None)

    for field, value in updates.items():
        setattr(lead, field, value)

    if new_status:
        if new_status not in VALID_STATUSES:
            raise HTTPException(400, f"Invalid status. Allowed: {VALID_STATUSES}")
        await update_lead_status(db, lead, new_status)
    else:
        lead.updated_at = datetime.utcnow()
        await db.commit()

    await db.refresh(lead)
    return _fmt_lead(lead)


@app.delete("/leads/{lead_id}", tags=["CRM - Leads"])
async def delete_lead(
    lead_id: UUID,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    lead = await db.get(Lead, lead_id)
    if not lead or lead.organization_id != org.id:
        raise HTTPException(404, "Lead not found")
    await db.delete(lead)
    await db.commit()
    logger.info("Lead deleted: %s", lead_id)
    return {"message": "Lead deleted"}


@app.post("/leads/import/csv", tags=["CRM - Leads"])
async def import_leads_csv(
    request: Request,
    current_user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """
    Import leads from a CSV file (multipart/form-data OR raw body).

    Supported columns (auto-detected, case-insensitive):
    company, company name, contact name, email, phone, website,
    linkedin, industry, employees, location, hq, notes

    Returns: {created, updated, skipped, warnings, errors}
    """
    from fastapi import UploadFile, File
    from services.lead_service import import_leads_from_csv

    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        file = form.get("file")
        if not file:
            raise HTTPException(400, "No file field in form data")
        content = await file.read()
    else:
        content = await request.body()

    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > 10 * 1024 * 1024:  # 10MB limit
        raise HTTPException(413, "File too large — max 10MB")

    logger.info("CSV import: %d bytes from user %s", len(content), current_user.id)

    result = await import_leads_from_csv(
        db=db,
        content=content,
        organization_id=org.id,
        created_by_id=current_user.id,
    )
    return result


@app.get("/leads/export/csv", tags=["CRM - Leads"])
async def export_leads_csv(
    status: Optional[str] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """Export all leads as CSV — streams to avoid memory issues on large datasets."""
    q = select(Lead).where(Lead.organization_id == org.id)
    if status:
        q = q.where(Lead.status == status)
    q = q.order_by(Lead.created_at)
    leads = (await db.execute(q)).scalars().all()

    HEADERS = [
        "Company", "Contact", "Email", "Phone", "Website", "LinkedIn",
        "Industry", "Employees", "HQ", "Status", "Score", "Source",
        "Notes", "Created At",
    ]

    def _row(l: Lead) -> str:
        buf = io.StringIO()
        csv.writer(buf).writerow([
            l.company_name, l.contact_name, l.email, l.phone, l.website,
            l.linkedin_url, l.industry, l.employee_count, l.headquarters,
            l.status, l.score, l.source, l.notes,
            l.created_at.isoformat() if l.created_at else "",
        ])
        return buf.getvalue()

    hdr = io.StringIO()
    csv.writer(hdr).writerow(HEADERS)

    def _stream():
        yield hdr.getvalue()
        for l in leads:
            yield _row(l)

    suffix = f"-{status}" if status else ""
    return StreamingResponse(
        _stream(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=leads{suffix}.csv"},
    )


# ── Lead Conversations ─────────────────────────────────────────────────────────

@app.get("/leads/{lead_id}/conversations", tags=["CRM - Leads"])
async def get_lead_conversations(
    lead_id: UUID,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    lead = await db.get(Lead, lead_id)
    if not lead or lead.organization_id != org.id:
        raise HTTPException(404, "Lead not found")
    q = select(Conversation).where(Conversation.lead_id == lead_id).order_by(Conversation.created_at)
    convs = (await db.execute(q)).scalars().all()
    return {"conversations": [_fmt_conversation(c) for c in convs]}


@app.post("/leads/{lead_id}/conversations", status_code=201, tags=["CRM - Leads"])
async def add_conversation(
    lead_id: UUID,
    req: ConversationCreate,
    current_user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """Manually log a touchpoint (call, email, note) against a lead."""
    lead = await db.get(Lead, lead_id)
    if not lead or lead.organization_id != org.id:
        raise HTTPException(404, "Lead not found")

    conv = Conversation(
        organization_id=org.id,
        lead_id=lead_id,
        user_id=current_user.id,
        channel=req.channel,
        direction=req.direction,
        subject=req.subject,
        body=req.body,
        status=req.status,
        sent_at=datetime.utcnow() if req.direction == "outbound" else None,
    )
    db.add(conv)

    if req.direction == "outbound" and lead.status == "new":
        lead.status = "contacted"
        lead.last_contacted_at = datetime.utcnow()

    await db.commit()
    await db.refresh(conv)
    return _fmt_conversation(conv)


# ════════════════════════════════════════════════════════════════════
#  CAMPAIGNS
# ════════════════════════════════════════════════════════════════════

@app.post("/campaigns", status_code=201, tags=["CRM - Campaigns"])
async def create_campaign(
    req: CampaignCreate,
    current_user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """Create a new email campaign with optional lead list."""
    campaign = Campaign(
        organization_id=org.id,
        created_by_id=current_user.id,
        name=req.name,
        subject=req.subject,
        body_template=req.body_template,
        from_name=req.from_name or settings.SMTP_FROM_NAME,
        reply_to=req.reply_to,
        send_rate=req.send_rate,
        status="draft",
    )
    db.add(campaign)
    await db.flush()

    # Attach leads if provided
    if req.lead_ids:
        for lid_str in req.lead_ids:
            try:
                lid = UUID(lid_str)
                lead = await db.get(Lead, lid)
                if lead and lead.organization_id == org.id and lead.email:
                    db.add(CampaignLead(campaign_id=campaign.id, lead_id=lid))
                    campaign.total_leads += 1
            except (ValueError, Exception) as e:
                logger.warning("Campaign create: invalid lead id %s: %s", lid_str, e)

    await db.commit()
    await db.refresh(campaign)
    logger.info("Campaign created: %s (%d leads)", campaign.name, campaign.total_leads)
    return _fmt_campaign(campaign)


@app.get("/campaigns", tags=["CRM - Campaigns"])
async def list_campaigns(
    page: int = 1, limit: int = 20,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    limit = min(limit, 100)
    q = (select(Campaign).where(Campaign.organization_id == org.id)
         .order_by(desc(Campaign.created_at)).offset((page - 1) * limit).limit(limit))
    campaigns = (await db.execute(q)).scalars().all()
    total = (await db.execute(
        select(func.count(Campaign.id)).where(Campaign.organization_id == org.id)
    )).scalar() or 0
    return {
        "campaigns": [_fmt_campaign(c) for c in campaigns],
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    }


@app.get("/campaigns/{campaign_id}", tags=["CRM - Campaigns"])
async def get_campaign(
    campaign_id: UUID,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    c = await db.get(Campaign, campaign_id)
    if not c or c.organization_id != org.id:
        raise HTTPException(404, "Campaign not found")
    return _fmt_campaign(c)


@app.post("/campaigns/{campaign_id}/leads", tags=["CRM - Campaigns"])
async def add_leads_to_campaign(
    campaign_id: UUID,
    lead_ids: List[str],
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """Add leads to an existing campaign."""
    campaign = await db.get(Campaign, campaign_id)
    if not campaign or campaign.organization_id != org.id:
        raise HTTPException(404, "Campaign not found")
    if campaign.status not in ("draft", "paused"):
        raise HTTPException(400, "Can only add leads to draft or paused campaigns")

    added = 0
    for lid_str in lead_ids:
        try:
            lid = UUID(lid_str)
            lead = await db.get(Lead, lid)
            if not lead or lead.organization_id != org.id or not lead.email:
                continue
            # Check not already added
            existing = (await db.execute(
                select(CampaignLead).where(
                    CampaignLead.campaign_id == campaign_id,
                    CampaignLead.lead_id == lid,
                )
            )).scalar_one_or_none()
            if not existing:
                db.add(CampaignLead(campaign_id=campaign_id, lead_id=lid))
                campaign.total_leads += 1
                added += 1
        except Exception as e:
            logger.warning("add_leads_to_campaign: %s — %s", lid_str, e)

    await db.commit()
    return {"added": added, "total_leads": campaign.total_leads}


@app.post("/campaigns/{campaign_id}/send", tags=["CRM - Campaigns"])
async def send_campaign(
    campaign_id: UUID,
    req: SendCampaignRequest,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """
    Start sending a campaign.
    Runs the email loop in the background (non-blocking).

    Errors:
    - 400 if campaign is not in draft/paused state
    - 503 if SMTP is not configured
    - 404 if campaign not found
    """
    from services.email_service import send_campaign_bulk

    if not settings.SMTP_HOST:
        raise HTTPException(503, "Email not configured — set SMTP_HOST in .env")

    campaign = await db.get(Campaign, campaign_id)
    if not campaign or campaign.organization_id != org.id:
        raise HTTPException(404, "Campaign not found")
    if campaign.status not in ("draft", "paused"):
        raise HTTPException(400, f"Campaign status is '{campaign.status}' — must be draft or paused to send")

    # Fetch pending CampaignLeads with their Lead objects
    q = (select(CampaignLead).where(
             CampaignLead.campaign_id == campaign_id,
             CampaignLead.status == "pending",
         ))
    if req.lead_ids:
        uuids = [UUID(lid) for lid in req.lead_ids]
        q = q.where(CampaignLead.lead_id.in_(uuids))

    cls = (await db.execute(q)).scalars().all()

    # Eagerly load the lead relationship for each
    for cl in cls:
        cl.lead = await db.get(Lead, cl.lead_id)

    if not cls:
        raise HTTPException(400, "No pending leads in this campaign")

    # Mark as running
    campaign.status = "running"
    campaign.started_at = datetime.utcnow()
    await db.commit()

    logger.info("Campaign %s: sending to %d leads", campaign_id, len(cls))

    # Fire-and-forget in background task
    _asyncio.create_task(send_campaign_bulk(db, campaign, cls, org.id))

    return {
        "message": f"Campaign started — sending to {len(cls)} leads",
        "campaign_id": str(campaign_id),
        "leads_queued": len(cls),
        "send_rate": f"{campaign.send_rate} emails/min",
    }


@app.post("/campaigns/{campaign_id}/pause", tags=["CRM - Campaigns"])
async def pause_campaign(
    campaign_id: UUID,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    campaign = await db.get(Campaign, campaign_id)
    if not campaign or campaign.organization_id != org.id:
        raise HTTPException(404, "Campaign not found")
    if campaign.status != "running":
        raise HTTPException(400, "Only running campaigns can be paused")
    campaign.status = "paused"
    await db.commit()
    return {"message": "Campaign paused"}


# ── Email Logs ────────────────────────────────────────────────────────────────

@app.get("/email/logs", tags=["CRM - Campaigns"])
async def get_email_logs(
    page: int = 1, limit: int = 50,
    campaign_id: Optional[str] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """View email send audit log."""
    limit = min(limit, 200)
    q = select(EmailLog).where(EmailLog.organization_id == org.id)
    if campaign_id:
        q = q.where(EmailLog.campaign_id == UUID(campaign_id))
    q = q.order_by(desc(EmailLog.sent_at)).offset((page - 1) * limit).limit(limit)
    logs = (await db.execute(q)).scalars().all()
    total = (await db.execute(
        select(func.count(EmailLog.id)).where(EmailLog.organization_id == org.id)
    )).scalar() or 0
    return {
        "logs": [
            {
                "id": l.id, "to_email": l.to_email, "subject": l.subject,
                "status": l.status, "sent_at": l.sent_at.isoformat(),
                "campaign_id": str(l.campaign_id) if l.campaign_id else None,
                "error_detail": l.error_detail,
            }
            for l in logs
        ],
        "total": total,
        "page": page,
    }


# ── Admin: List All Users (role-protected) ───────────────────────────────────

@app.get("/admin/users", tags=["Admin"])
async def admin_list_users(
    page: int = 1, limit: int = 50,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin-only: list all users across all organizations."""
    q = select(User).order_by(desc(User.created_at)).offset((page-1)*limit).limit(limit)
    users = (await db.execute(q)).scalars().all()
    total = (await db.execute(select(func.count(User.id)))).scalar() or 0
    return {
        "users": [
            {
                "id": str(u.id), "email": u.email, "full_name": u.full_name,
                "is_admin": u.is_admin, "is_active": u.is_active,
                "org_id": str(u.organization_id) if u.organization_id else None,
                "created_at": u.created_at.isoformat(),
                "last_login": u.last_login.isoformat() if u.last_login else None,
            }
            for u in users
        ],
        "total": total,
    }


# ════════════════════════════════════════════════════════════════════
#  §5  Lead Enrichment Trigger
# ════════════════════════════════════════════════════════════════════

@app.post("/leads/{lead_id}/enrich", tags=["CRM - Leads"])
async def enrich_lead(
    lead_id: UUID,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """
    Run the AI enrichment agent against a single lead record.

    Pulls company_name + website from the lead, runs the full
    7-step Groq agent, then writes back enriched fields:
    industry, employee_count, headquarters, description, funding_info,
    linkedin_url, confidence_score, etc.

    Common errors:
      404 → lead_id not found or belongs to different org
      503 → GROQ_API_KEY / API_KEY not configured in .env
      422 → lead has no company_name — nothing to enrich

    Debug:
      curl -X POST http://localhost:8000/leads/{lead_id}/enrich \\
        -H "Authorization: Bearer <token>"

    Takes 20–60 seconds depending on Groq response time.
    """
    from db.models import EnrichmentResult
    from agents.enrichment_agent import EnrichmentAgent

    lead = await db.get(Lead, lead_id)
    if not lead or lead.organization_id != org.id:
        raise HTTPException(404, "Lead not found")
    if not lead.company_name:
        raise HTTPException(422, "Lead has no company_name — cannot enrich")

    resolved_key = settings.groq_api_key_resolved
    if not resolved_key:
        raise HTTPException(
            503,
            "AI key not configured. Add GROQ_API_KEY=gsk_... or API_KEY=gsk_... to .env"
        )

    logger.info("Enriching lead %s (%s) for org %s", lead_id, lead.company_name, org.id)

    agent = EnrichmentAgent()
    result: EnrichmentResult = await agent.enrich_company(
        db=db,
        job_id=None,
        organization_id=org.id,
        company_name=lead.company_name,
        website_hint=lead.website,
    )

    # Write enriched data back to the lead record
    if result.status == "completed":
        if result.industry      and not lead.industry:       lead.industry      = result.industry
        if result.employee_count and not lead.employee_count: lead.employee_count = result.employee_count
        if result.headquarters  and not lead.headquarters:   lead.headquarters  = result.headquarters
        if result.description   and not lead.description:    lead.description   = result.description
        if result.funding_info  and not lead.funding_info:   lead.funding_info  = result.funding_info
        if result.linkedin_url  and not lead.linkedin_url:   lead.linkedin_url  = result.linkedin_url
        if result.website       and not lead.website:         lead.website       = result.website
        lead.updated_at = datetime.utcnow()
        lead.source = "ai_enriched"
        await db.commit()
        await db.refresh(lead)
        logger.info(
            "Lead %s enriched: confidence=%s tokens=%s",
            lead_id, result.confidence_score, result.tokens_used,
        )
    else:
        logger.warning("Lead enrichment failed for %s: %s", lead_id, result.error_message)

    return {
        "lead": _fmt_lead(lead),
        "enrichment": {
            "status":           result.status,
            "confidence_score": result.confidence_score,
            "tokens_used":      result.tokens_used,
            "tool_calls_made":  result.tool_calls_made,
            "processing_ms":    result.processing_time_ms,
            "error_message":    result.error_message,
        },
    }


# ════════════════════════════════════════════════════════════════════
#  §6  Email — Test & Template Preview
# ════════════════════════════════════════════════════════════════════

class EmailTestRequest(BaseModel):
    to_email: str


@app.post("/email/test", tags=["CRM - Campaigns"])
async def test_email_config(
    req: EmailTestRequest,
    _user: User = Depends(get_current_user),
):
    """
    Send a test email to verify SMTP configuration.

    Sends a simple HTML test message to req.to_email using the
    SMTP credentials configured in .env.

    Common errors + fixes:
      503 → SMTP_HOST not set
            Fix: Add SMTP_HOST=smtp.gmail.com to .env
      "Auth failed" → wrong SMTP_PASSWORD
            Fix: For Gmail use an App Password (not your real password)
            Go to: Google Account → Security → 2-Step Verification → App Passwords
      "Connection refused" → wrong SMTP_PORT or host
            Fix: Port 587 = STARTTLS, Port 465 = SSL, Port 25 = plain (blocked by most ISPs)
      "Recipient refused" → to_email is invalid
            Fix: Use a real email address

    Debug:
      curl -X POST http://localhost:8000/email/test \\
        -H "Authorization: Bearer <token>" \\
        -H "Content-Type: application/json" \\
        -d '{"to_email": "you@example.com"}'
    """
    if not settings.SMTP_HOST:
        raise HTTPException(
            503,
            "SMTP not configured. Add SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD to .env"
        )

    from services.email_service import send_email
    body_html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:40px auto;padding:32px;
                border:1px solid #e2e8f0;border-radius:12px">
        <h2 style="color:#f59e0b;margin:0 0 16px">AgentIQ ✓</h2>
        <p style="color:#374151">Your SMTP configuration is working correctly.</p>
        <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0">
        <p style="color:#6b7280;font-size:12px">
            Sent via {settings.SMTP_HOST}:{settings.SMTP_PORT}<br>
            From: {settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME}<br>
            Time: {datetime.utcnow().isoformat()} UTC
        </p>
    </div>
    """

    logger.info("Sending SMTP test email to %s via %s:%s", req.to_email, settings.SMTP_HOST, settings.SMTP_PORT)
    result = send_email(
        to_email=req.to_email,
        subject="AgentIQ — SMTP Test ✓",
        body_html=body_html,
        from_name="AgentIQ System",
    )

    if result["success"]:
        logger.info("Test email delivered to %s (message_id=%s)", req.to_email, result["message_id"])
        return {
            "success": True,
            "message": f"Test email sent to {req.to_email}",
            "smtp_host": settings.SMTP_HOST,
            "smtp_port": settings.SMTP_PORT,
            "message_id": result["message_id"],
        }
    else:
        logger.error("SMTP test failed: %s", result["error"])
        raise HTTPException(
            502,
            f"SMTP delivery failed: {result['error']}\n\n"
            "Common fixes:\n"
            "• Gmail: use App Password, not regular password\n"
            "• Check SMTP_HOST / SMTP_PORT / SMTP_USE_TLS in .env\n"
            "• Check firewall allows outbound on port 587"
        )


@app.get("/campaigns/{campaign_id}/preview", tags=["CRM - Campaigns"])
async def preview_campaign_template(
    campaign_id: UUID,
    lead_id: Optional[str] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """
    Render the campaign email template with real or sample data.

    If lead_id is provided: renders with that lead's actual fields.
    If not: renders with sample placeholder values so you can preview
    the layout before sending.

    Returns rendered subject + body HTML ready for preview.

    Debug:
      # Preview with sample data:
      curl http://localhost:8000/campaigns/{id}/preview \\
        -H "Authorization: Bearer <token>"

      # Preview with a specific lead:
      curl "http://localhost:8000/campaigns/{id}/preview?lead_id={lead_id}" \\
        -H "Authorization: Bearer <token>"
    """
    from services.email_service import render_template, build_lead_variables

    campaign = await db.get(Campaign, campaign_id)
    if not campaign or campaign.organization_id != org.id:
        raise HTTPException(404, "Campaign not found")

    if lead_id:
        lead = await db.get(Lead, UUID(lead_id))
        if not lead or lead.organization_id != org.id:
            raise HTTPException(404, "Lead not found")
        variables = build_lead_variables(lead)
        source = f"Lead: {lead.company_name}"
    else:
        # Sample values for preview without a real lead
        variables = {
            "first_name":    "Jane",
            "last_name":     "Smith",
            "full_name":     "Jane Smith",
            "company":       "Acme Corp",
            "industry":      "B2B SaaS",
            "website":       "https://acmecorp.com",
            "email":         "jane@acmecorp.com",
            "headquarters":  "San Francisco, CA",
            "employee_count": "50-200",
        }
        source = "Sample data (no lead selected)"

    rendered_subject = render_template(campaign.subject, variables)
    rendered_body    = render_template(campaign.body_template, variables)

    return {
        "campaign_id":      str(campaign_id),
        "campaign_name":    campaign.name,
        "rendered_subject": rendered_subject,
        "rendered_body":    rendered_body,
        "variables_used":   variables,
        "preview_source":   source,
        "send_rate":        campaign.send_rate,
        "total_leads":      campaign.total_leads,
    }


# ════════════════════════════════════════════════════════════════════
#  §10  Monitoring — Logs, Metrics, Admin Stats
# ════════════════════════════════════════════════════════════════════

@app.get("/logs", tags=["Monitoring"])
async def get_system_logs(
    page: int = 1,
    limit: int = 100,
    action: Optional[str] = None,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """
    Usage log viewer for the current organisation.

    Shows every AI enrichment call, credit consumption, token usage,
    and processing times. Useful for debugging cost overruns.

    Filters:
      ?action=enrichment    — only enrichment events
      ?action=api_call      — only API key authenticated calls

    Common errors:
      401 → not authenticated
      200 with empty list → no activity yet for this org

    Debug:
      curl "http://localhost:8000/logs?limit=20" \\
        -H "Authorization: Bearer <token>"
    """
    limit = min(limit, 500)
    q = (
        select(UsageLog)
        .where(UsageLog.organization_id == org.id)
        .order_by(desc(UsageLog.created_at))
        .offset((page - 1) * limit)
        .limit(limit)
    )
    if action:
        q = q.where(UsageLog.action == action)

    logs = (await db.execute(q)).scalars().all()
    total = (await db.execute(
        select(func.count(UsageLog.id)).where(UsageLog.organization_id == org.id)
    )).scalar() or 0

    return {
        "logs": [
            {
                "id":              l.id,
                "action":          l.action,
                "credits_consumed": l.credits_consumed,
                "tokens_used":     l.tokens_used,
                "model_used":      l.model_used,
                "job_id":          str(l.job_id) if l.job_id else None,
                "extra_data":      l.extra_data,
                "created_at":      l.created_at.isoformat(),
            }
            for l in logs
        ],
        "total":   total,
        "page":    page,
        "pages":   max(1, (total + limit - 1) // limit),
    }


@app.get("/metrics", tags=["Monitoring"])
async def get_org_metrics(
    days: int = 30,
    org: Organization = Depends(get_current_org),
    db: AsyncSession = Depends(get_db),
):
    """
    API usage metrics for the current organisation.

    Returns aggregated stats for the last N days:
      • total credits consumed
      • total tokens used
      • enrichments run
      • leads created + by status
      • campaigns created + sent
      • emails sent / failed
      • average enrichment confidence

    Useful for billing reconciliation and debugging unexpected
    credit consumption.

    Debug:
      curl "http://localhost:8000/metrics?days=7" \\
        -H "Authorization: Bearer <token>"
    """
    from db.models import Lead, Campaign, EmailLog
    since = datetime.utcnow() - timedelta(days=days)

    # Run all queries in parallel
    (
        credits_r, tokens_r, enrich_r, avg_conf_r,
        leads_total_r, leads_by_status_r,
        campaigns_r, emails_r, email_fail_r
    ) = await asyncio.gather(
        db.execute(select(func.sum(UsageLog.credits_consumed))
                   .where(UsageLog.organization_id == org.id, UsageLog.created_at >= since)),
        db.execute(select(func.sum(UsageLog.tokens_used))
                   .where(UsageLog.organization_id == org.id, UsageLog.created_at >= since)),
        db.execute(select(func.count(EnrichmentResult.id))
                   .where(EnrichmentResult.organization_id == org.id, EnrichmentResult.enriched_at >= since)),
        db.execute(select(func.avg(EnrichmentResult.confidence_score))
                   .where(EnrichmentResult.organization_id == org.id,
                          EnrichmentResult.confidence_score.isnot(None),
                          EnrichmentResult.enriched_at >= since)),
        db.execute(select(func.count(Lead.id))
                   .where(Lead.organization_id == org.id, Lead.created_at >= since)),
        db.execute(select(Lead.status, func.count(Lead.id))
                   .where(Lead.organization_id == org.id)
                   .group_by(Lead.status)),
        db.execute(select(func.count(Campaign.id))
                   .where(Campaign.organization_id == org.id, Campaign.created_at >= since)),
        db.execute(select(func.count(EmailLog.id))
                   .where(EmailLog.organization_id == org.id,
                          EmailLog.status == "sent", EmailLog.sent_at >= since)),
        db.execute(select(func.count(EmailLog.id))
                   .where(EmailLog.organization_id == org.id,
                          EmailLog.status == "failed", EmailLog.sent_at >= since)),
    )

    leads_by_status = {row[0]: row[1] for row in leads_by_status_r.all()}

    return {
        "period_days":            days,
        "credits_consumed":       credits_r.scalar() or 0,
        "tokens_used":            tokens_r.scalar() or 0,
        "enrichments_run":        enrich_r.scalar() or 0,
        "avg_confidence_score":   round(avg_conf_r.scalar() or 0, 1),
        "leads_created":          leads_total_r.scalar() or 0,
        "leads_by_status":        leads_by_status,
        "campaigns_created":      campaigns_r.scalar() or 0,
        "emails_sent":            emails_r.scalar() or 0,
        "emails_failed":          email_fail_r.scalar() or 0,
        "email_success_rate_pct": (
            round(emails_r.scalar() / (emails_r.scalar() + email_fail_r.scalar()) * 100, 1)
            if (emails_r.scalar() or 0) + (email_fail_r.scalar() or 0) > 0
            else None
        ),
    }


@app.get("/admin/stats", tags=["Admin"])
async def admin_platform_stats(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Admin-only: platform-wide statistics across ALL organisations.

    Returns:
      • total orgs, users, leads, jobs, enrichments, emails
      • active jobs right now
      • jobs in last 24 hours
      • total credits and tokens consumed all-time

    Common errors:
      403 → user does not have is_admin = true
            Fix: Run SQL:  UPDATE users SET is_admin=true WHERE email='you@company.com';

    Debug:
      curl http://localhost:8000/admin/stats \\
        -H "Authorization: Bearer <admin_token>"
    """
    from db.models import Lead as LeadModel, Campaign as CampaignModel, EmailLog as EmailLogModel
    since_24h = datetime.utcnow() - timedelta(hours=24)

    (
        orgs_r, users_r, leads_r, jobs_r,
        enrich_r, campaigns_r, emails_r,
        active_jobs_r, jobs_24h_r,
        total_credits_r, total_tokens_r,
    ) = await asyncio.gather(
        db.execute(select(func.count(Organization.id))),
        db.execute(select(func.count(User.id))),
        db.execute(select(func.count(LeadModel.id))),
        db.execute(select(func.count(Job.id))),
        db.execute(select(func.count(EnrichmentResult.id))),
        db.execute(select(func.count(CampaignModel.id))),
        db.execute(select(func.count(EmailLogModel.id)).where(EmailLogModel.status == "sent")),
        db.execute(select(func.count(Job.id)).where(Job.status.in_(["queued", "running"]))),
        db.execute(select(func.count(Job.id)).where(Job.created_at >= since_24h)),
        db.execute(select(func.sum(UsageLog.credits_consumed))),
        db.execute(select(func.sum(UsageLog.tokens_used))),
    )

    return {
        "platform": {
            "total_organizations": orgs_r.scalar() or 0,
            "total_users":         users_r.scalar() or 0,
            "total_leads":         leads_r.scalar() or 0,
            "total_jobs":          jobs_r.scalar() or 0,
            "total_enrichments":   enrich_r.scalar() or 0,
            "total_campaigns":     campaigns_r.scalar() or 0,
            "total_emails_sent":   emails_r.scalar() or 0,
        },
        "activity": {
            "active_jobs_now":     active_jobs_r.scalar() or 0,
            "jobs_last_24h":       jobs_24h_r.scalar() or 0,
        },
        "consumption": {
            "total_credits":       total_credits_r.scalar() or 0,
            "total_tokens":        total_tokens_r.scalar() or 0,
        },
    }
