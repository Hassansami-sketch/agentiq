"""
services/email_service.py
SMTP email service with rate control, template rendering, and full error logging.

Supports:
- Single send
- Bulk send with configurable rate limit (emails/minute)
- Template variable substitution ({{first_name}}, {{company}}, etc.)
- Automatic retry on transient SMTP errors
- Full audit logging to EmailLog table
"""
import sys, os
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

import asyncio
import logging
import re
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from typing import Optional, List, Dict

from core.config import settings

logger = logging.getLogger(__name__)


# ── Template Engine ───────────────────────────────────────────────────────────

def render_template(template: str, variables: Dict[str, str]) -> str:
    """
    Replace {{variable}} placeholders in template.
    Example: render_template("Hi {{first_name}}", {"first_name": "Jane"})
    → "Hi Jane"
    Missing variables are left as-is (not blanked out).
    """
    def replacer(match):
        key = match.group(1).strip()
        return variables.get(key, match.group(0))  # leave placeholder if key missing

    return re.sub(r'\{\{(\s*\w+\s*)\}\}', replacer, template)


def build_lead_variables(lead) -> Dict[str, str]:
    """Extract template variables from a Lead ORM object."""
    first_name = ""
    last_name = ""
    if lead.contact_name:
        parts = lead.contact_name.strip().split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    return {
        "first_name":    first_name or "there",
        "last_name":     last_name,
        "full_name":     lead.contact_name or "there",
        "company":       lead.company_name or "",
        "industry":      lead.industry or "",
        "website":       lead.website or "",
        "email":         lead.email or "",
        "headquarters":  lead.headquarters or "",
        "employee_count": lead.employee_count or "",
    }


# ── SMTP Connection ───────────────────────────────────────────────────────────

class SMTPClient:
    """
    Thread-safe SMTP client with auto-reconnect and TLS support.

    Errors handled:
    - smtplib.SMTPAuthenticationError  → bad credentials
    - smtplib.SMTPConnectError         → host/port wrong or network issue
    - smtplib.SMTPRecipientsRefused    → invalid recipient
    - smtplib.SMTPServerDisconnected   → connection dropped (auto-retry)
    - ConnectionRefusedError           → SMTP host not reachable
    """

    def __init__(self):
        self._conn: Optional[smtplib.SMTP] = None

    def _connect(self) -> smtplib.SMTP:
        host = settings.SMTP_HOST
        port = settings.SMTP_PORT
        username = settings.SMTP_USERNAME
        password = settings.SMTP_PASSWORD
        use_tls = settings.SMTP_USE_TLS

        if not host:
            raise ValueError("SMTP_HOST not configured in .env")
        if not username or not password:
            raise ValueError("SMTP_USERNAME / SMTP_PASSWORD not set in .env")

        logger.info("SMTP: connecting to %s:%d (TLS=%s)", host, port, use_tls)

        if use_tls:
            conn = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            conn = smtplib.SMTP(host, port, timeout=10)
            conn.starttls()

        conn.login(username, password)
        logger.info("SMTP: authenticated as %s", username)
        return conn

    def get_connection(self) -> smtplib.SMTP:
        """Return active connection, reconnect if needed."""
        if self._conn:
            try:
                self._conn.noop()   # ping — raises if dead
                return self._conn
            except Exception:
                logger.warning("SMTP: connection lost, reconnecting…")
                self._conn = None

        self._conn = self._connect()
        return self._conn

    def close(self):
        if self._conn:
            try:
                self._conn.quit()
            except Exception:
                pass
            self._conn = None


# ── Single Email Send ─────────────────────────────────────────────────────────

def send_email(
    to_email: str,
    subject: str,
    body_html: str,
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
    body_text: Optional[str] = None,
) -> Dict:
    """
    Send a single email via SMTP. Returns a result dict.

    Returns:
        {"success": True,  "message_id": "...", "error": None}
        {"success": False, "message_id": None,  "error": "reason"}
    """
    from_email = settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME
    from_addr  = formataddr((from_name or settings.SMTP_FROM_NAME or "AgentIQ", from_email))
    message_id = make_msgid(domain=from_email.split("@")[-1] if "@" in from_email else "agentiq.app")

    msg = MIMEMultipart("alternative")
    msg["Subject"]    = subject
    msg["From"]       = from_addr
    msg["To"]         = to_email
    msg["Message-ID"] = message_id
    if reply_to:
        msg["Reply-To"] = reply_to

    if body_text:
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    client = SMTPClient()
    max_attempts = 2

    for attempt in range(max_attempts):
        try:
            conn = client.get_connection()
            conn.sendmail(from_email, [to_email], msg.as_string())
            logger.info("SMTP: sent → %s (id=%s)", to_email, message_id)
            return {"success": True, "message_id": message_id, "error": None}

        except smtplib.SMTPAuthenticationError as e:
            # Fatal — wrong credentials, don't retry
            logger.error("SMTP auth failed: %s", e)
            return {"success": False, "message_id": None, "error": f"Auth failed: {e}"}

        except smtplib.SMTPRecipientsRefused as e:
            # Fatal — bad address
            logger.warning("SMTP: recipient refused %s: %s", to_email, e)
            return {"success": False, "message_id": None, "error": f"Recipient refused: {e}"}

        except smtplib.SMTPServerDisconnected:
            # Transient — force reconnect and retry once
            logger.warning("SMTP: server disconnected, retrying (attempt %d)", attempt + 1)
            client._conn = None
            if attempt == max_attempts - 1:
                return {"success": False, "message_id": None, "error": "SMTP server disconnected after retry"}

        except Exception as e:
            logger.error("SMTP: unexpected error sending to %s: %s", to_email, e)
            return {"success": False, "message_id": None, "error": str(e)}

    return {"success": False, "message_id": None, "error": "Max retries exceeded"}


# ── Bulk Campaign Sender ──────────────────────────────────────────────────────

async def send_campaign_bulk(
    db,
    campaign,
    campaign_leads: List,
    organization_id,
) -> Dict:
    """
    Send campaign emails to all pending leads with rate control.

    Rate control: settings.SMTP_RATE_LIMIT emails/minute.
    Runs async — each send is offloaded to executor so we don't
    block the event loop during SMTP I/O.

    Returns summary: {sent, failed, total}
    """
    from db.models import EmailLog, Conversation
    from sqlalchemy import text

    rate_limit    = campaign.send_rate or settings.SMTP_RATE_LIMIT  # per minute
    interval_secs = 60.0 / rate_limit  # seconds between sends

    sent = 0
    failed = 0
    total = len(campaign_leads)

    logger.info(
        "Campaign %s: sending to %d leads at %d/min (%.1fs interval)",
        campaign.id, total, rate_limit, interval_secs,
    )

    loop = asyncio.get_event_loop()

    for i, cl in enumerate(campaign_leads):
        lead = cl.lead
        if not lead or not lead.email:
            logger.warning("Campaign %s: lead %s has no email, skipping", campaign.id, cl.lead_id)
            failed += 1
            continue

        # Render personalised email
        variables    = build_lead_variables(lead)
        subject_text = render_template(campaign.subject, variables)
        body_html    = render_template(campaign.body_template, variables)

        # Send in thread pool (SMTP is blocking)
        result = await loop.run_in_executor(
            None,
            lambda s=subject_text, b=body_html, e=lead.email: send_email(
                to_email=e,
                subject=s,
                body_html=b,
                from_name=campaign.from_name,
                reply_to=campaign.reply_to,
            )
        )

        now = datetime.utcnow()
        send_status = "sent" if result["success"] else "failed"

        # Update CampaignLead status atomically
        await db.execute(
            text("""
                UPDATE campaign_leads
                SET status   = :status,
                    sent_at  = :sent_at,
                    error_msg = :error
                WHERE id = :cl_id
            """),
            {
                "status": send_status,
                "sent_at": now if result["success"] else None,
                "error": result.get("error"),
                "cl_id": str(cl.id),
            }
        )

        # Update campaign counters
        if result["success"]:
            sent += 1
            await db.execute(
                text("UPDATE campaigns SET sent_count = sent_count + 1 WHERE id = :cid"),
                {"cid": str(campaign.id)}
            )
            # Update lead pipeline status
            if lead.status == "new":
                await db.execute(
                    text("UPDATE leads SET status='contacted', last_contacted_at=:ts WHERE id=:lid"),
                    {"ts": now, "lid": str(lead.id)}
                )
        else:
            failed += 1
            await db.execute(
                text("UPDATE campaigns SET failed_count = failed_count + 1 WHERE id = :cid"),
                {"cid": str(campaign.id)}
            )

        # Audit log
        db.add(EmailLog(
            organization_id=organization_id,
            campaign_id=campaign.id,
            lead_id=lead.id,
            to_email=lead.email,
            from_email=settings.SMTP_FROM_EMAIL or settings.SMTP_USERNAME or "",
            subject=subject_text,
            status=send_status,
            smtp_message_id=result.get("message_id"),
            error_detail=result.get("error"),
            sent_at=now,
        ))

        # Create conversation record
        if result["success"]:
            db.add(Conversation(
                organization_id=organization_id,
                lead_id=lead.id,
                channel="email",
                direction="outbound",
                subject=subject_text,
                body=body_html,
                status="sent",
                external_msg_id=result.get("message_id"),
                sent_at=now,
            ))

        await db.commit()

        logger.info(
            "Campaign %s: [%d/%d] %s → %s (%s)",
            campaign.id, i + 1, total, send_status, lead.email,
            result.get("error", "OK"),
        )

        # Rate limiting: wait between sends
        if i < total - 1:
            await asyncio.sleep(interval_secs)

    # Mark campaign complete
    await db.execute(
        text("""
            UPDATE campaigns
            SET status       = 'completed',
                completed_at = NOW()
            WHERE id = :cid
        """),
        {"cid": str(campaign.id)}
    )
    await db.commit()

    logger.info(
        "Campaign %s complete: %d sent, %d failed / %d total",
        campaign.id, sent, failed, total,
    )
    return {"sent": sent, "failed": failed, "total": total}
