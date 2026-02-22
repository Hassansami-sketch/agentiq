# AgentIQ v2 — API Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        BROWSER / CLIENT                             │
│                   frontend/dashboard.html                           │
└────────────────────────────┬────────────────────────────────────────┘
                             │ HTTPS (JSON / FormData)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    FastAPI (api/main.py)                            │
│                      Port 8000                                      │
│                                                                     │
│  Middleware stack (in order):                                       │
│  1. CORS           — allow browser origins                          │
│  2. Rate Limiter   — 60 req/min per IP                              │
│  3. Request Logger — logs method + path + latency                   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Auth Layer                                                   │   │
│  │  POST /auth/register  → bcrypt hash → create User + Org     │   │
│  │  POST /auth/login     → bcrypt verify → issue JWT           │   │
│  │  GET  /auth/me        → decode JWT → return user info        │   │
│  │                                                              │   │
│  │  Every protected route: Bearer JWT  OR  X-API-Key header    │   │
│  │  Admin routes: require is_admin = true on User              │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────────┐   │
│  │  Enrichment    │  │   CRM / Leads  │  │    Campaigns       │   │
│  │                │  │                │  │                    │   │
│  │ POST /jobs     │  │ GET  /leads    │  │ POST /campaigns    │   │
│  │  → Celery task │  │ POST /leads    │  │  → create record   │   │
│  │                │  │ PATCH /leads   │  │                    │   │
│  │ POST /enrich   │  │  /{id}         │  │ POST /campaigns    │   │
│  │  /single       │  │ DELETE /leads  │  │  /{id}/send        │   │
│  │  → direct call │  │  /{id}         │  │  → background loop │   │
│  │                │  │                │  │                    │   │
│  │ GET /jobs/{id} │  │ POST /leads    │  │ GET /email/logs    │   │
│  │  /results      │  │  /import/csv   │  │                    │   │
│  │  /export (CSV) │  │ GET /leads     │  │                    │   │
│  │                │  │  /export/csv   │  │                    │   │
│  └───────┬────────┘  └───────┬────────┘  └────────┬───────────┘   │
│          │                   │                     │               │
└──────────┼───────────────────┼─────────────────────┼───────────────┘
           │                   │                     │
     ┌─────▼──────┐    ┌───────▼───────┐    ┌────────▼────────┐
     │   Celery   │    │  Neon Postgres │    │   SMTP Server   │
     │  Worker    │    │  (cloud DB)   │    │  (Gmail/SG/MG)  │
     │            │    │               │    │                 │
     │ Per company│    │ Tables:       │    │ Rate limited:   │
     │ session    │    │ • users       │    │ N emails/min    │
     │ Atomic SQL │    │ • orgs        │    │ Per-lead vars   │
     │ counters   │    │ • jobs        │    │ Audit to        │
     │ Batch      │    │ • enrichment  │    │ email_logs      │
     │ commits    │    │   _results    │    │ Update lead     │
     │            │    │ • leads       │    │ status →        │
     └─────┬──────┘    │ • conversations│    │ "contacted"     │
           │           │ • campaigns   │    └─────────────────┘
           │           │ • campaign    │
     ┌─────▼──────┐    │   _leads      │
     │  Groq API  │    │ • email_logs  │
     │  (llama)   │    │ • api_keys    │
     │            │    │ • usage_logs  │
     │ Tool calls:│    │ • plans       │
     │ • find_url │    │ • subscriptions│
     │ • scrape   │    └───────────────┘
     │ • search   │
     │ • linkedin │    ┌───────────────┐
     └────────────┘    │  Upstash Redis│
                       │               │
                       │ • Job queue   │
                       │ • Task state  │
                       │ • Rate limits │
                       └───────────────┘

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ENRICHMENT FLOW (one company):
─────────────────────────────
  POST /jobs  →  Job created (status=queued)
       │
       └─→  Celery picks up task
                  │
                  ├── For each company in batch:
                  │     1. find_company_website("Stripe")
                  │     2. scrape_website("https://stripe.com")
                  │     3. scrape_website(".../about")
                  │     4. search_web("Stripe funding investors")
                  │     5. search_web("Stripe news 2025")
                  │     6. get_linkedin_info("Stripe")
                  │     7. search_web("Stripe competitors")
                  │     8. Groq returns JSON → parse → save to enrichment_results
                  │     9. UPDATE jobs SET completed_items = completed_items + 1  (atomic)
                  │    10. await asyncio.sleep(2)   ← rate limit between companies
                  │
                  └── Job status = "completed"

LEAD PIPELINE:
──────────────
  CSV Import ──→  Lead (status=new)
                        │
          POST /leads/import/csv
          or POST /leads (manual)
                        │
                  status = "new"
                        │
          Campaign send or manual note
                        │
                  status = "contacted"   ← auto-set on campaign send
                        │
          Lead replies
                        │
                  status = "replied"
                        │
          Deal closes
                        │
                  status = "converted"   ← sets converted_at timestamp

EMAIL CAMPAIGN FLOW:
────────────────────
  POST /campaigns  →  Campaign created (status=draft) + CampaignLeads
       │
  POST /campaigns/{id}/send
       │
       └─→  asyncio.create_task(send_campaign_bulk(...))
                  │
                  ├── For each pending CampaignLead:
                  │     1. render_template(body, {first_name, company, industry, …})
                  │     2. SMTPClient.send_email(to_email, subject, body_html)
                  │     3. UPDATE campaign_leads SET status='sent'
                  │     4. UPDATE campaigns SET sent_count = sent_count + 1  (atomic)
                  │     5. UPDATE leads SET status='contacted'
                  │     6. INSERT INTO email_logs (audit record)
                  │     7. INSERT INTO conversations (touchpoint record)
                  │     8. await asyncio.sleep(60 / send_rate)  ← rate control
                  │
                  └── Campaign status = "completed"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AUTHENTICATION FLOW:
────────────────────
  POST /auth/register
    body: { email, password, full_name, org_name }
    → bcrypt hash password
    → INSERT users + organizations
    → slug collision detection with random suffix
    → return JWT (HS256, 24h expiry)

  POST /auth/login
    body: OAuth2 form (username=email, password)
    → bcrypt verify
    → UPDATE last_login
    → return JWT

  Every protected request:
    Header: Authorization: Bearer <jwt>
    OR:     X-API-Key: aiq_sk_<key>

  JWT decode → user_id UUID → SELECT user → check is_active
  API key   → SHA256 hash  → SELECT api_keys → get org → get first user

  Admin routes (GET /admin/users):
    Additional check: user.is_admin == True
    Returns 403 Forbidden if not admin

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DATABASE SCHEMA SUMMARY:
────────────────────────
  organizations   id, name, slug, stripe_customer_id
  users           id, email, hashed_password, is_admin, organization_id
  plans           id, name, tier, monthly_credits, stripe_price_id
  subscriptions   id, organization_id, plan_id, stripe_subscription_id, status
  api_keys        id, organization_id, key_hash, key_prefix, is_active
  jobs            id, organization_id, status, total_items, completed_items
  enrichment_results  id, job_id, organization_id, company_name, ...29 fields
  usage_logs      id(bigint), organization_id, action, credits_consumed, tokens_used
  leads           id, organization_id, company_name, email, status(pipeline)
  conversations   id, lead_id, channel, direction, body, status
  campaigns       id, organization_id, subject, body_template, send_rate, stats
  campaign_leads  id, campaign_id, lead_id, status
  email_logs      id(bigint), campaign_id, lead_id, to_email, status, smtp_message_id

  Key indexes on every FK + (org_id, status) + (org_id, created_at) composites
  ON DELETE CASCADE: enrichment_results → jobs, conversations → leads
```
