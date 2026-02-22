from datetime import datetime
from datetime import datetime
import uuid
from sqlalchemy import (
    Column, String, Integer, Float, Boolean,
    DateTime, ForeignKey, Text, JSON, BigInteger, Index, UniqueConstraint
)
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID

from db.base import Base

class Organization(Base):
    __tablename__ = "organizations"
    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name         = Column(String(255), nullable=False)
    slug         = Column(String(100), unique=True, nullable=False)
    stripe_customer_id = Column(String(100), unique=True, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    users        = relationship("User", back_populates="organization")
    jobs         = relationship("Job", back_populates="organization")
    api_keys     = relationship("APIKey", back_populates="organization")
    subscription = relationship("Subscription", back_populates="organization", uselist=False)


class User(Base):
    __tablename__ = "users"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email           = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    full_name       = Column(String(255), nullable=True)
    is_active       = Column(Boolean, default=True)
    is_verified     = Column(Boolean, default=False)
    is_admin        = Column(Boolean, default=False)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True, index=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    last_login      = Column(DateTime, nullable=True)

    organization = relationship("Organization", back_populates="users")
    jobs         = relationship("Job", back_populates="created_by")


class Plan(Base):
    __tablename__ = "plans"
    id                 = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name               = Column(String(100), nullable=False)
    tier               = Column(String(50), nullable=False)
    stripe_price_id    = Column(String(100), unique=True, nullable=True)
    monthly_credits    = Column(Integer, nullable=False)
    max_batch_size     = Column(Integer, nullable=False)
    max_concurrent_jobs = Column(Integer, default=1)
    api_access         = Column(Boolean, default=False)
    priority_processing = Column(Boolean, default=False)
    price_monthly      = Column(Float, nullable=False)
    price_yearly       = Column(Float, nullable=True)
    is_active          = Column(Boolean, default=True)
    created_at         = Column(DateTime, default=datetime.utcnow)

    subscriptions = relationship("Subscription", back_populates="plan")


class Subscription(Base):
    __tablename__ = "subscriptions"
    id                     = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id        = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    plan_id                = Column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False)
    stripe_subscription_id = Column(String(100), unique=True, nullable=True)
    status                 = Column(String(50), default="active")
    credits_used           = Column(Integer, default=0)
    credits_reset_at       = Column(DateTime, nullable=True)
    current_period_start   = Column(DateTime, nullable=True)
    current_period_end     = Column(DateTime, nullable=True)
    cancelled_at           = Column(DateTime, nullable=True)
    created_at             = Column(DateTime, default=datetime.utcnow)

    organization = relationship("Organization", back_populates="subscription")
    plan         = relationship("Plan", back_populates="subscriptions")

    __table_args__ = (
        # FIX: was missing — queried on every Stripe webhook
        Index("idx_subscriptions_org", "organization_id"),
    )


class APIKey(Base):
    __tablename__ = "api_keys"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    name            = Column(String(100), nullable=False)
    key_hash        = Column(String(255), unique=True, nullable=False)
    key_prefix      = Column(String(20), nullable=False)
    is_active       = Column(Boolean, default=True)
    last_used_at    = Column(DateTime, nullable=True)
    expires_at      = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    organization = relationship("Organization", back_populates="api_keys")


class Job(Base):
    __tablename__ = "jobs"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    created_by_id   = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    name            = Column(String(255), nullable=True)
    agent_type      = Column(String(50), nullable=False, default="lead_enrichment")
    status          = Column(String(50), default="pending", index=True)
    priority        = Column(Integer, default=5)
    input_data      = Column(JSON, nullable=False)   # stores companies list + config
    agent_config    = Column(JSON, default=dict)
    total_items     = Column(Integer, default=0)
    completed_items = Column(Integer, default=0)
    failed_items    = Column(Integer, default=0)
    progress_pct    = Column(Float, default=0.0)
    celery_task_id  = Column(String(255), nullable=True)
    error_message   = Column(Text, nullable=True)
    credits_used    = Column(Integer, default=0)
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)
    started_at      = Column(DateTime, nullable=True)
    completed_at    = Column(DateTime, nullable=True)

    organization = relationship("Organization", back_populates="jobs")
    created_by   = relationship("User", back_populates="jobs")

    # FIX: removed cascade="all, delete-orphan" from the ORM relationship.
    # Cascade is now handled at the DB level via ON DELETE CASCADE on the FK,
    # so deleting a job issues ONE DELETE statement, not N (one per result).
    results = relationship(
        "EnrichmentResult", back_populates="job",
        passive_deletes=True,   # tells SQLAlchemy the DB handles cascade
    )

    __table_args__ = (
        Index("idx_jobs_org_status",   "organization_id", "status"),
        Index("idx_jobs_org_created",  "organization_id", "created_at"),
    )


class EnrichmentResult(Base):
    __tablename__ = "enrichment_results"
    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # FIX: ON DELETE CASCADE at DB level so Job deletion is one SQL statement
    job_id          = Column(UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)

    input_name      = Column(String(255), nullable=False)
    input_website   = Column(String(500), nullable=True)
    company_name    = Column(String(255), nullable=True)
    website         = Column(String(500), nullable=True)
    linkedin_url    = Column(String(500), nullable=True)
    founded_year    = Column(Integer, nullable=True)
    headquarters    = Column(String(255), nullable=True)
    employee_count  = Column(String(50), nullable=True)
    industry        = Column(String(255), nullable=True)
    company_type    = Column(String(100), nullable=True)
    description     = Column(Text, nullable=True)
    key_products    = Column(JSON, default=list)
    target_customers = Column(String(255), nullable=True)
    tech_stack      = Column(JSON, default=list)
    recent_news     = Column(Text, nullable=True)
    funding_info    = Column(String(500), nullable=True)
    key_contacts    = Column(JSON, default=list)

    # FIX: raw_data removed — was storing full Groq response per row.
    # At 100k results this becomes gigabytes of redundant JSON in Neon.
    # The parsed fields above capture everything needed.

    confidence_score   = Column(Integer, nullable=True)
    enrichment_notes   = Column(Text, nullable=True)
    status             = Column(String(50), default="completed")
    error_message      = Column(Text, nullable=True)
    model_used         = Column(String(100), nullable=True)
    tokens_used        = Column(Integer, default=0)
    tool_calls_made    = Column(Integer, default=0)
    processing_time_ms = Column(Integer, default=0)
    enriched_at        = Column(DateTime, default=datetime.utcnow)

    job = relationship("Job", back_populates="results")

    __table_args__ = (
        # FIX: job_id index was missing — every results fetch was a full table scan
        Index("idx_results_job_id",        "job_id"),
        # FIX: composite org+status index for dashboard COUNT queries
        Index("idx_results_org_status",    "organization_id", "status"),
        # FIX: composite org+time for time-range analytics queries
        Index("idx_results_org_time",      "organization_id", "enriched_at"),
        # Existing indexes kept
        Index("idx_results_org",           "organization_id"),
        Index("idx_results_company",       "company_name"),
        # FIX: prevent double-enrichment of same company within same job
        UniqueConstraint("job_id", "input_name", name="uq_results_job_company"),
    )


class UsageLog(Base):
    __tablename__ = "usage_logs"
    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    job_id          = Column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    action          = Column(String(100), nullable=False)
    credits_consumed = Column(Integer, default=1)
    tokens_used     = Column(Integer, default=0)
    model_used      = Column(String(100), nullable=True)
    extra_data      = Column(JSON, default=dict)
    created_at      = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        # FIX: was only individual indexes; billing query filters on BOTH columns
        Index("idx_usage_org_time", "organization_id", "created_at"),
        # FIX: job_id was unindexed — needed for per-job usage breakdown
        Index("idx_usage_job",      "job_id"),
    )


# ═══════════════════════════════════════════════════════════════════
#  CRM / Lead Generation Module  (added per SaaS requirements)
# ═══════════════════════════════════════════════════════════════════

class Lead(Base):
    """
    CRM lead with full pipeline tracking.
    Status pipeline: new → contacted → replied → converted (or dead)
    """
    __tablename__ = "leads"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    created_by_id   = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    # Identity
    company_name    = Column(String(255), nullable=False)
    contact_name    = Column(String(255), nullable=True)
    email           = Column(String(255), nullable=True, index=True)
    phone           = Column(String(50), nullable=True)
    website         = Column(String(500), nullable=True)
    linkedin_url    = Column(String(500), nullable=True)

    # Enrichment data
    industry        = Column(String(255), nullable=True)
    employee_count  = Column(String(50), nullable=True)
    headquarters    = Column(String(255), nullable=True)
    description     = Column(Text, nullable=True)
    funding_info    = Column(String(500), nullable=True)

    # Pipeline
    status          = Column(String(50), default="new", nullable=False)  # new|contacted|replied|converted|dead
    score           = Column(Integer, default=0)  # 0-100 lead score
    notes           = Column(Text, nullable=True)
    tags            = Column(JSON, default=list)

    # Source tracking
    source          = Column(String(100), nullable=True)   # csv_import|scrape|manual|enrichment
    enrichment_result_id = Column(UUID(as_uuid=True), ForeignKey("enrichment_results.id"), nullable=True)

    # Timestamps
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_contacted_at = Column(DateTime, nullable=True)
    converted_at    = Column(DateTime, nullable=True)

    conversations   = relationship("Conversation", back_populates="lead", cascade="all, delete-orphan")
    campaign_leads  = relationship("CampaignLead", back_populates="lead")

    __table_args__ = (
        Index("idx_leads_org",            "organization_id"),
        Index("idx_leads_org_status",     "organization_id", "status"),
        Index("idx_leads_org_created",    "organization_id", "created_at"),
        Index("idx_leads_email",          "email"),
    )


class Conversation(Base):
    """Tracks every touchpoint/message with a lead."""
    __tablename__ = "conversations"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    lead_id         = Column(UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    user_id         = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    channel         = Column(String(50), nullable=False, default="email")   # email|phone|linkedin|note
    direction       = Column(String(10), nullable=False, default="outbound") # outbound|inbound
    subject         = Column(String(500), nullable=True)
    body            = Column(Text, nullable=False)
    status          = Column(String(50), default="sent")  # sent|delivered|opened|replied|bounced|failed
    external_msg_id = Column(String(255), nullable=True)   # SMTP message ID

    sent_at         = Column(DateTime, nullable=True)
    opened_at       = Column(DateTime, nullable=True)
    replied_at      = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead", back_populates="conversations")

    __table_args__ = (
        Index("idx_conv_lead",    "lead_id"),
        Index("idx_conv_org",     "organization_id"),
        Index("idx_conv_created", "organization_id", "created_at"),
    )


class Campaign(Base):
    """Email outreach campaign — targets a list of leads."""
    __tablename__ = "campaigns"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    created_by_id   = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    name            = Column(String(255), nullable=False)
    subject         = Column(String(500), nullable=False)
    body_template   = Column(Text, nullable=False)          # Supports {{first_name}}, {{company}} placeholders
    from_name       = Column(String(255), nullable=True)
    reply_to        = Column(String(255), nullable=True)

    status          = Column(String(50), default="draft")   # draft|scheduled|running|paused|completed
    send_rate       = Column(Integer, default=30)           # emails per minute — rate control
    schedule_at     = Column(DateTime, nullable=True)       # null = send now

    # Stats (updated atomically)
    total_leads     = Column(Integer, default=0)
    sent_count      = Column(Integer, default=0)
    opened_count    = Column(Integer, default=0)
    replied_count   = Column(Integer, default=0)
    bounced_count   = Column(Integer, default=0)
    failed_count    = Column(Integer, default=0)

    created_at      = Column(DateTime, default=datetime.utcnow)
    started_at      = Column(DateTime, nullable=True)
    completed_at    = Column(DateTime, nullable=True)

    leads           = relationship("CampaignLead", back_populates="campaign", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_campaigns_org",        "organization_id"),
        Index("idx_campaigns_org_status", "organization_id", "status"),
    )


class CampaignLead(Base):
    """Junction: which leads are in which campaign + per-lead send status."""
    __tablename__ = "campaign_leads"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id = Column(UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    lead_id     = Column(UUID(as_uuid=True), ForeignKey("leads.id",     ondelete="CASCADE"), nullable=False)

    status      = Column(String(50), default="pending")  # pending|sent|opened|replied|bounced|unsubscribed|failed
    sent_at     = Column(DateTime, nullable=True)
    opened_at   = Column(DateTime, nullable=True)
    replied_at  = Column(DateTime, nullable=True)
    error_msg   = Column(Text, nullable=True)

    campaign    = relationship("Campaign", back_populates="leads")
    lead        = relationship("Lead", back_populates="campaign_leads")

    __table_args__ = (
        UniqueConstraint("campaign_id", "lead_id", name="uq_campaign_lead"),
        Index("idx_cl_campaign", "campaign_id"),
        Index("idx_cl_lead",     "lead_id"),
        Index("idx_cl_status",   "campaign_id", "status"),
    )


class EmailLog(Base):
    """Immutable audit log for every email send attempt."""
    __tablename__ = "email_logs"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    campaign_id     = Column(UUID(as_uuid=True), ForeignKey("campaigns.id"), nullable=True)
    lead_id         = Column(UUID(as_uuid=True), ForeignKey("leads.id"),     nullable=True)

    to_email        = Column(String(255), nullable=False)
    from_email      = Column(String(255), nullable=False)
    subject         = Column(String(500), nullable=False)
    status          = Column(String(50),  nullable=False)  # sent|failed|bounced
    smtp_message_id = Column(String(255), nullable=True)
    error_detail    = Column(Text, nullable=True)
    sent_at         = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_emaillog_org",      "organization_id"),
        Index("idx_emaillog_campaign", "campaign_id"),
        Index("idx_emaillog_sent",     "organization_id", "sent_at"),
    )
