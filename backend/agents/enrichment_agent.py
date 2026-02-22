"""
agents/enrichment_agent.py
Groq-powered enrichment agent — FREE, no Anthropic key needed.
Model: llama-3.3-70b-versatile
"""

import sys, os
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

import asyncio, time, json, logging, re
from datetime import datetime
from typing import Optional
from uuid import UUID

from groq import Groq, RateLimitError, APIError, APITimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from db.models import EnrichmentResult, UsageLog
from agents.tools import execute_tool, GROQ_TOOL_DEFINITIONS

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are AgentIQ, an elite business intelligence analyst.
Research companies exhaustively and return structured JSON profiles.

## MANDATORY Steps — execute ALL in order:

STEP 1: Call find_company_website with the company name
STEP 2: Call scrape_website on the homepage URL
STEP 3: Call scrape_website on the About/Company page
STEP 4: Call search_web for "{company} funding raised investors series"
STEP 5: Call search_web for "{company} news 2024 2025 announcement"
STEP 6: Call get_linkedin_info with the company name
STEP 7: Call search_web for "{company} competitors market position"
STEP 8: Return ONLY the final JSON — no other text

## RULES:
- MUST call tools before writing any final answer
- Never skip steps — each reveals different data
- If scrape fails, use search_web as fallback
- Never fabricate data — use null for unknown fields
- Final response = ONLY raw JSON, no markdown, no explanation

## JSON Schema (return exactly this structure):
{
  "name": "Official company name",
  "website": "https://...",
  "linkedin_url": "https://linkedin.com/company/...",
  "founded_year": 2018,
  "headquarters": "San Francisco, CA, USA",
  "employee_count": "200-500",
  "industry": "B2B SaaS / Sales Intelligence",
  "company_type": "Series B Startup",
  "description": "2-3 sentences: what they do and who they serve",
  "key_products": ["Product A", "Service B"],
  "target_customers": "Mid-market sales teams",
  "tech_stack": ["React", "Python", "AWS"],
  "recent_news": "Raised $45M Series B in March 2024",
  "funding_info": "Series B — $45M raised, $120M total",
  "key_contacts": ["Jane Smith - CEO", "John Doe - CTO"],
  "annual_revenue_estimate": "$10M-50M ARR",
  "growth_signals": ["Hiring in sales", "EU expansion Q1 2024"],
  "risk_factors": ["Competition from Apollo.io"],
  "confidence_score": 8,
  "enrichment_notes": "Pricing page blocked, revenue estimated"
}

Confidence: 9-10=complete, 7-8=minor gaps, 5-6=significant gaps, 1-4=very limited"""


class EnrichmentAgent:
    MODELS = {
        "best":    "llama-3.3-70b-versatile",
        "fast":    "llama3-8b-8192",
        "preview": "llama-3.3-70b-versatile",  # llama-3.1-70b-versatile was deprecated
    }

    def __init__(self, model_tier: str = "best"):
        resolved_key = settings.groq_api_key_resolved
        if not resolved_key:
            raise ValueError(
                "No Groq API key found. Set GROQ_API_KEY=gsk_... or API_KEY=gsk_... in your .env file.\n"
                "Get a free key at https://console.groq.com"
            )
        self.client = Groq(api_key=resolved_key)
        self.model = self.MODELS.get(model_tier, self.MODELS["best"])
        self.max_iterations = settings.GROQ_MAX_TOOL_ITERATIONS
        self.max_retries = 3
        self.base_delay = 2.0
        logger.info(f"EnrichmentAgent ready — {self.model}")

    async def enrich_company(
        self,
        db: AsyncSession,
        job_id: Optional[UUID],
        organization_id: UUID,
        company_name: str,
        website_hint: Optional[str] = None,
        extra_context: Optional[dict] = None
    ) -> EnrichmentResult:
        start_time = time.time()
        total_tokens = 0
        tool_calls_count = 0
        final_text = ""
        iteration = 0

        logger.info(f"[{str(organization_id)[:8]}] Enriching: {company_name}")

        user_content = f"Research this company thoroughly: {company_name}"
        if website_hint:
            user_content += f"\nWebsite: {website_hint} — scrape it directly."
        if extra_context:
            for k, v in extra_context.items():
                user_content += f"\n{k}: {v}"

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content}
        ]

        while iteration < self.max_iterations:
            iteration += 1
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda m=messages: self._call_groq_with_retry(m))
            if response is None:
                break

            if response.usage:
                total_tokens += (response.usage.prompt_tokens or 0) + (response.usage.completion_tokens or 0)

            choice = response.choices[0]
            finish_reason = choice.finish_reason
            msg = choice.message

            if finish_reason == "stop":
                final_text = msg.content or ""
                logger.info(f"  ✓ {company_name} done — {iteration} iters, {tool_calls_count} tools")
                break

            if finish_reason == "tool_calls" and msg.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ]
                })
                for tc in msg.tool_calls:
                    tool_calls_count += 1
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    logger.info(f"  → {tc.function.name}({list(args.keys())})")
                    result = execute_tool(tc.function.name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(result)
                    })
            else:
                if msg.content:
                    final_text = msg.content
                break

        processing_time_ms = int((time.time() - start_time) * 1000)

        result = await self._save_result(
            db, job_id, organization_id, company_name,
            final_text, self.model, total_tokens, tool_calls_count, processing_time_ms
        )

        usage_log = UsageLog(
            organization_id=organization_id, job_id=job_id,
            action="enrichment", credits_consumed=1,
            tokens_used=total_tokens, model_used=self.model,
            extra_data={
                "company": company_name, "tool_calls": tool_calls_count,
                "iterations": iteration, "confidence": result.confidence_score,
                "processing_ms": processing_time_ms, "status": result.status
            }
        )
        db.add(usage_log)
        await db.commit()

        logger.info(f"✓ {company_name} | conf={result.confidence_score}/10 | tokens={total_tokens:,} | {processing_time_ms}ms")
        return result

    def _call_groq_with_retry(self, messages: list):
        import time as _time
        for attempt in range(self.max_retries):
            try:
                return self.client.chat.completions.create(
                    model=self.model, messages=messages,
                    tools=GROQ_TOOL_DEFINITIONS, tool_choice="auto",
                    max_tokens=settings.GROQ_MAX_TOKENS, temperature=0.1,
                    timeout=60.0,   # §2: explicit 60s timeout per API call
                )
            except APITimeoutError as e:
                # §2: Timeout error handling
                wait = self.base_delay * (2 ** attempt)
                logger.warning(
                    "  Groq API timeout (attempt %d/%d) — waiting %.0fs. "
                    "Fix: increase GROQ_MAX_TOKENS or check network. Error: %s",
                    attempt + 1, self.max_retries, wait, e,
                )
                _time.sleep(wait)
            except RateLimitError as e:
                wait = self.base_delay * (2 ** attempt)
                logger.warning(f"  Rate limited, waiting {wait:.0f}s — {e}")
                _time.sleep(wait)
            except APIError as e:
                if attempt < self.max_retries - 1:
                    _time.sleep(self.base_delay)
                else:
                    logger.error(f"  Groq APIError: {e}")
                    return None
            except Exception as e:
                logger.error(f"  Unexpected error: {e}")
                return None
        return None

    async def _save_result(self, db, job_id, organization_id, company_name,
                            raw_text, model_used, tokens_used, tool_calls_made, processing_time_ms):
        data = {}
        status = "completed"
        error_message = None
        try:
            json_str = self._extract_json(raw_text)
            if json_str:
                data = json.loads(json_str)
                if not isinstance(data, dict):
                    raise ValueError("Not a dict")
            else:
                raise ValueError(f"No JSON found. Preview: {raw_text[:200]}")
        except Exception as e:
            logger.error(f"  Parse error for {company_name}: {e}")
            status = "failed"
            error_message = str(e)

        result = EnrichmentResult(
            job_id=job_id, organization_id=organization_id, input_name=company_name,
            company_name=data.get("name") or company_name,
            website=data.get("website"), linkedin_url=data.get("linkedin_url"),
            founded_year=self._safe_int(data.get("founded_year")),
            headquarters=data.get("headquarters"), employee_count=data.get("employee_count"),
            industry=data.get("industry"), company_type=data.get("company_type"),
            description=data.get("description"), key_products=data.get("key_products") or [],
            target_customers=data.get("target_customers"), tech_stack=data.get("tech_stack") or [],
            recent_news=data.get("recent_news"), funding_info=data.get("funding_info"),
            key_contacts=data.get("key_contacts") or [],
            # raw_data column removed — was storing full Groq response per row (too large at scale)
            confidence_score=self._safe_int(data.get("confidence_score")),
            enrichment_notes=data.get("enrichment_notes"),
            status=status, error_message=error_message,
            model_used=model_used, tokens_used=tokens_used,
            tool_calls_made=tool_calls_made, processing_time_ms=processing_time_ms,
            enriched_at=datetime.utcnow()
        )
        db.add(result)
        await db.flush()
        return result

    def _extract_json(self, text: str) -> Optional[str]:
        if not text:
            return None
        text = text.strip()
        if text.startswith("{"):
            try:
                json.loads(text)
                return text
            except json.JSONDecodeError:
                pass
        cb = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if cb:
            try:
                json.loads(cb.group(1))
                return cb.group(1)
            except json.JSONDecodeError:
                pass
        jm = re.search(r"\{.*\}", text, re.DOTALL)
        if jm:
            try:
                json.loads(jm.group(0))
                return jm.group(0)
            except json.JSONDecodeError:
                pass
        partial = re.search(r"\{.*", text, re.DOTALL)
        if partial:
            candidate = partial.group(0).rstrip().rstrip(",") + "\n}"
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass
        return None

    def _safe_int(self, value) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
