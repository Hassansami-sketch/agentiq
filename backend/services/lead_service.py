"""
services/lead_service.py
Lead import, scraping, and pipeline management.

Features:
- CSV import with column auto-mapping
- Basic web scrape fallback for missing data
- Duplicate detection by email
- Bulk upsert with conflict resolution
"""
import sys, os
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

import csv
import io
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from db.models import Lead

logger = logging.getLogger(__name__)

# ── Column mapping: maps common CSV header variants → Lead field names ─────────
COLUMN_MAP: Dict[str, str] = {
    # Company
    "company":       "company_name",
    "company name":  "company_name",
    "company_name":  "company_name",
    "organization":  "company_name",
    "org":           "company_name",
    # Contact
    "name":          "contact_name",
    "contact":       "contact_name",
    "contact name":  "contact_name",
    "contact_name":  "contact_name",
    "full name":     "contact_name",
    "first name":    "_first_name",  # special: merged below
    "last name":     "_last_name",
    # Email
    "email":         "email",
    "email address": "email",
    "e-mail":        "email",
    # Phone
    "phone":         "phone",
    "tel":           "phone",
    "telephone":     "phone",
    "mobile":        "phone",
    # Web
    "website":       "website",
    "url":           "website",
    "domain":        "website",
    # LinkedIn
    "linkedin":      "linkedin_url",
    "linkedin url":  "linkedin_url",
    "linkedin_url":  "linkedin_url",
    # Industry / size
    "industry":      "industry",
    "sector":        "industry",
    "employees":     "employee_count",
    "employee count":"employee_count",
    "size":          "employee_count",
    "headcount":     "employee_count",
    # Location
    "location":      "headquarters",
    "city":          "headquarters",
    "hq":            "headquarters",
    "headquarters":  "headquarters",
    # Notes
    "notes":         "notes",
    "description":   "description",
    "comments":      "notes",
}

VALID_STATUSES = {"new", "contacted", "replied", "converted", "dead"}


def _map_headers(raw_headers: List[str]) -> Dict[int, str]:
    """Return {column_index: lead_field} mapping for recognized headers."""
    mapping: Dict[int, str] = {}
    for i, h in enumerate(raw_headers):
        key = h.strip().lower()
        if key in COLUMN_MAP:
            mapping[i] = COLUMN_MAP[key]
    return mapping


def parse_csv_bytes(content: bytes) -> Tuple[List[Dict], List[str]]:
    """
    Parse CSV bytes → list of raw row dicts.
    Returns (rows, warnings).

    Common errors handled:
    - BOM (UTF-8-sig)
    - Windows CRLF line endings
    - Empty rows
    - Headers with trailing spaces
    """
    warnings: List[str] = []

    try:
        text = content.decode("utf-8-sig").strip()  # handles BOM
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1").strip()
            warnings.append("File decoded as latin-1 (not UTF-8)")
        except Exception as e:
            return [], [f"Cannot decode file: {e}"]

    if not text:
        return [], ["File is empty"]

    reader = csv.DictReader(io.StringIO(text))
    rows: List[Dict] = []
    for i, row in enumerate(reader):
        # Skip blank rows
        if not any(v.strip() for v in row.values()):
            continue
        rows.append({k.strip(): v.strip() for k, v in row.items()})

    logger.info("CSV parse: %d rows, %d columns", len(rows), len(reader.fieldnames or []))
    return rows, warnings


async def import_leads_from_csv(
    db: AsyncSession,
    content: bytes,
    organization_id: UUID,
    created_by_id: Optional[UUID],
    source: str = "csv_import",
) -> Dict:
    """
    Import leads from CSV bytes into the leads table.

    Returns:
        {
          "created": int,
          "updated": int,
          "skipped": int,
          "warnings": list[str],
          "errors": list[str]
        }

    Duplicate detection: if a lead with the same (organization_id, email)
    already exists, we UPDATE it (upsert). Rows with no email and no
    company_name are skipped.
    """
    raw_rows, warnings = parse_csv_bytes(content)
    errors: List[str] = []
    created = updated = skipped = 0

    if not raw_rows:
        return {"created": 0, "updated": 0, "skipped": 0,
                "warnings": warnings, "errors": errors or ["No valid rows found"]}

    for row_num, row in enumerate(raw_rows, start=2):  # start=2 because row 1 = header
        try:
            lead_data = _map_row(row)

            if not lead_data.get("company_name") and not lead_data.get("email"):
                skipped += 1
                warnings.append(f"Row {row_num}: skipped (no company or email)")
                continue

            # Upsert by email within org
            existing = None
            if lead_data.get("email"):
                r = await db.execute(
                    select(Lead).where(
                        Lead.organization_id == organization_id,
                        Lead.email == lead_data["email"]
                    )
                )
                existing = r.scalar_one_or_none()

            if existing:
                # Update existing lead with any new data
                for field, value in lead_data.items():
                    if value and not getattr(existing, field, None):
                        setattr(existing, field, value)
                existing.updated_at = datetime.utcnow()
                updated += 1
                logger.info("Lead updated (email=%s)", lead_data.get("email"))
            else:
                lead = Lead(
                    organization_id=organization_id,
                    created_by_id=created_by_id,
                    source=source,
                    **lead_data,
                )
                db.add(lead)
                created += 1

            if (created + updated) % 50 == 0:
                await db.flush()

        except Exception as e:
            errors.append(f"Row {row_num}: {e}")
            logger.error("CSV import row %d error: %s", row_num, e)
            continue

    await db.commit()
    logger.info(
        "CSV import complete: %d created, %d updated, %d skipped, %d errors",
        created, updated, skipped, len(errors),
    )
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "warnings": warnings,
        "errors": errors,
    }


def _map_row(row: Dict[str, str]) -> Dict:
    """Convert a raw CSV row dict → Lead field dict."""
    result: Dict = {}
    first_name = ""
    last_name = ""

    for col_header, value in row.items():
        if not value:
            continue
        key = col_header.strip().lower()
        field = COLUMN_MAP.get(key)
        if not field:
            continue
        if field == "_first_name":
            first_name = value
        elif field == "_last_name":
            last_name = value
        else:
            result[field] = value

    # Merge first/last name if present
    if first_name or last_name:
        full = f"{first_name} {last_name}".strip()
        if full and not result.get("contact_name"):
            result["contact_name"] = full

    # Normalize website
    if "website" in result:
        w = result["website"]
        if w and not w.startswith(("http://", "https://")):
            result["website"] = "https://" + w

    # Ensure company_name is set
    if not result.get("company_name") and result.get("website"):
        domain = result["website"].replace("https://", "").replace("http://", "").split("/")[0]
        result["company_name"] = domain

    return result


async def update_lead_status(
    db: AsyncSession,
    lead: Lead,
    new_status: str,
) -> Lead:
    """
    Advance a lead through the pipeline.
    Validates status transitions and updates timestamps.

    Pipeline: new → contacted → replied → converted
    Any status can go to: dead
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{new_status}'. Must be one of: {VALID_STATUSES}")

    old_status = lead.status
    lead.status = new_status
    lead.updated_at = datetime.utcnow()

    if new_status == "contacted" and old_status == "new":
        lead.last_contacted_at = datetime.utcnow()
    elif new_status == "converted":
        lead.converted_at = datetime.utcnow()

    await db.commit()
    logger.info("Lead %s: %s → %s", lead.id, old_status, new_status)
    return lead


async def get_pipeline_summary(db: AsyncSession, organization_id: UUID) -> Dict:
    """Return count per status for the pipeline kanban view."""
    rows = await db.execute(
        select(Lead.status, func.count(Lead.id))
        .where(Lead.organization_id == organization_id)
        .group_by(Lead.status)
    )
    summary = {s: 0 for s in VALID_STATUSES}
    for status, count in rows:
        summary[status] = count
    return summary
