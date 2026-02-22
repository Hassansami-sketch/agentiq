# AgentIQ v2 — CRUD Examples

Full working examples for every table. All use the REST API.
Replace `<token>` with your JWT from `POST /auth/login`.

```bash
BASE=http://localhost:8000
TOKEN=<your_jwt_token>
```

---

## Table: Users

### CREATE — Register a new user
```bash
curl -X POST $BASE/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@company.com",
    "password": "SecurePass1!",
    "full_name": "Alice Johnson",
    "org_name": "Acme Corp"
  }'
# Response: {"access_token":"...","token_type":"bearer","org_id":"..."}
```

### READ — Get current user
```bash
curl $BASE/auth/me -H "Authorization: Bearer $TOKEN"
# Response: {"id":"...","email":"alice@company.com","full_name":"Alice Johnson",...}
```

### UPDATE — Change password
```bash
curl -X POST $BASE/auth/change-password \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"current_password":"SecurePass1!","new_password":"NewSecurePass2@"}'
# Response: {"message":"Password updated successfully"}
```

### READ (admin) — List all users
```bash
curl "$BASE/admin/users?page=1&limit=50" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
# Response: {"users":[...],"total":42}
```

---

## Table: Leads

### CREATE — Add lead manually
```bash
curl -X POST $BASE/leads \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "Stripe",
    "contact_name": "Patrick Collison",
    "email": "patrick@stripe.com",
    "phone": "+1 415 000 0001",
    "website": "https://stripe.com",
    "linkedin_url": "https://linkedin.com/company/stripe",
    "industry": "Fintech / Payments",
    "employee_count": "5000-10000",
    "headquarters": "San Francisco, CA",
    "description": "Online payment processing platform",
    "notes": "Warm intro via Tom",
    "tags": ["enterprise","payments","tier1"],
    "source": "manual"
  }'
# Response: {"id":"<uuid>","status":"new","company_name":"Stripe",...}
```

### CREATE (bulk) — Import from CSV
```bash
# CSV format — headers auto-detected:
cat > leads.csv << 'CSV'
company name,contact name,email,phone,website,industry,location
Stripe,Patrick Collison,patrick@stripe.com,+1 415 000 0001,https://stripe.com,Fintech,SF CA
Notion,Ivan Zhao,ivan@notion.so,+1 415 000 0002,https://notion.so,Productivity,SF CA
CSV

curl -X POST $BASE/leads/import/csv \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@leads.csv"
# Response: {"created":2,"updated":0,"skipped":0,"warnings":[],"errors":[]}
```

### READ — List with filters
```bash
# All leads (paginated):
curl "$BASE/leads?page=1&limit=50" -H "Authorization: Bearer $TOKEN"

# Filter by pipeline status:
curl "$BASE/leads?status=new" -H "Authorization: Bearer $TOKEN"
curl "$BASE/leads?status=contacted" -H "Authorization: Bearer $TOKEN"
curl "$BASE/leads?status=replied" -H "Authorization: Bearer $TOKEN"
curl "$BASE/leads?status=converted" -H "Authorization: Bearer $TOKEN"

# Search by company / contact / email:
curl "$BASE/leads?search=Stripe" -H "Authorization: Bearer $TOKEN"
curl "$BASE/leads?search=patrick@stripe.com" -H "Authorization: Bearer $TOKEN"

# Combined filter + search:
curl "$BASE/leads?status=new&search=Corp" -H "Authorization: Bearer $TOKEN"
```

### READ — Single lead
```bash
curl $BASE/leads/<lead_id> -H "Authorization: Bearer $TOKEN"
# Response: full lead object with all fields
```

### READ — Pipeline summary (kanban counts)
```bash
curl $BASE/leads/pipeline -H "Authorization: Bearer $TOKEN"
# Response: {"new":12,"contacted":8,"replied":3,"converted":5,"dead":2}
```

### UPDATE — Advance pipeline status
```bash
# Move to contacted:
curl -X PATCH $BASE/leads/<lead_id> \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"contacted"}'

# Move to replied:
curl -X PATCH $BASE/leads/<lead_id> \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"replied"}'

# Convert:
curl -X PATCH $BASE/leads/<lead_id> \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status":"converted"}'

# Update fields (any combination):
curl -X PATCH $BASE/leads/<lead_id> \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "notes": "Met at SaaStr — follow up in Q2",
    "score": 85,
    "tags": ["hot","enterprise","follow-up"]
  }'
```

### UPDATE — AI Enrich (writes back to lead)
```bash
curl -X POST $BASE/leads/<lead_id>/enrich \
  -H "Authorization: Bearer $TOKEN"
# Takes 20-60s. Writes: industry, employee_count, headquarters,
# description, funding_info, linkedin_url, website back to the lead.
# Response: {"lead":{...updated_lead},"enrichment":{"status":"completed","confidence_score":8,...}}
```

### DELETE
```bash
curl -X DELETE $BASE/leads/<lead_id> -H "Authorization: Bearer $TOKEN"
# Response: {"message":"Lead deleted"}
# Note: also deletes related conversations (ON DELETE CASCADE)
```

### EXPORT — Download all leads as CSV
```bash
# All leads:
curl "$BASE/leads/export/csv" \
  -H "Authorization: Bearer $TOKEN" \
  -o all_leads.csv

# Only new leads:
curl "$BASE/leads/export/csv?status=new" \
  -H "Authorization: Bearer $TOKEN" \
  -o new_leads.csv
```

---

## Table: Conversations

### CREATE — Log a touchpoint
```bash
curl -X POST $BASE/leads/<lead_id>/conversations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "channel": "email",
    "direction": "outbound",
    "subject": "Following up on our call",
    "body": "Hi Jane, great speaking with you. As promised here is the deck...",
    "status": "sent"
  }'
# channel options: email | phone | linkedin | note
# direction options: outbound | inbound
```

### CREATE — Log an inbound reply
```bash
curl -X POST $BASE/leads/<lead_id>/conversations \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "channel": "email",
    "direction": "inbound",
    "subject": "Re: Following up",
    "body": "Thanks — let'\''s schedule a demo for next week",
    "status": "received"
  }'
# Note: inbound messages auto-advance lead status to "replied"
```

### READ — Get all conversations for a lead
```bash
curl $BASE/leads/<lead_id>/conversations -H "Authorization: Bearer $TOKEN"
# Response: {"conversations":[{"id":"...","channel":"email","direction":"outbound",...},...]}
```

---

## Table: Campaigns

### CREATE — New campaign with template
```bash
curl -X POST $BASE/campaigns \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Q1 2026 — SaaS Founders",
    "subject": "Quick question about {{company}}",
    "body_template": "Hi {{first_name}},\n\nI came across {{company}} and was really impressed by what you'\''re building in {{industry}}.\n\nWould you be open to a 15-min call this week?\n\nBest,\nAlex",
    "from_name": "Alex at AgentIQ",
    "reply_to": "alex@agentiq.com",
    "send_rate": 20,
    "lead_ids": ["<lead_uuid_1>","<lead_uuid_2>"]
  }'
# Template variables: {{first_name}} {{last_name}} {{full_name}} {{company}}
#                     {{industry}} {{website}} {{email}} {{headquarters}}
```

### READ — List campaigns
```bash
curl "$BASE/campaigns?page=1&limit=20" -H "Authorization: Bearer $TOKEN"
```

### READ — Single campaign
```bash
curl $BASE/campaigns/<campaign_id> -H "Authorization: Bearer $TOKEN"
# Returns: name, subject, status, total_leads, sent_count, opened_count, etc.
```

### READ — Preview rendered template
```bash
# With sample data:
curl $BASE/campaigns/<campaign_id>/preview -H "Authorization: Bearer $TOKEN"

# With a real lead:
curl "$BASE/campaigns/<campaign_id>/preview?lead_id=<lead_id>" \
  -H "Authorization: Bearer $TOKEN"
# Response: {"rendered_subject":"Quick question about Stripe","rendered_body":"Hi Patrick,..."}
```

### UPDATE — Add more leads to a draft campaign
```bash
curl -X POST $BASE/campaigns/<campaign_id>/leads \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '["<lead_id_3>","<lead_id_4>"]'
# Response: {"added":2,"total_leads":4}
```

### RUN — Start sending
```bash
curl -X POST $BASE/campaigns/<campaign_id>/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
# Response: {"message":"Campaign started — sending to 4 leads","send_rate":"20 emails/min"}
```

### UPDATE — Pause a running campaign
```bash
curl -X POST $BASE/campaigns/<campaign_id>/pause -H "Authorization: Bearer $TOKEN"
# Response: {"message":"Campaign paused"}
```

---

## Table: Logs (email_logs + usage_logs)

### READ — Email send audit log
```bash
# All email logs for your org:
curl "$BASE/email/logs?page=1&limit=50" -H "Authorization: Bearer $TOKEN"

# Filter by campaign:
curl "$BASE/email/logs?campaign_id=<campaign_id>" -H "Authorization: Bearer $TOKEN"
```

### READ — Usage / credits log
```bash
# All usage events (paginated):
curl "$BASE/logs?page=1&limit=100" -H "Authorization: Bearer $TOKEN"

# Filter by event type:
curl "$BASE/logs?action=enrichment" -H "Authorization: Bearer $TOKEN"

# Aggregated metrics (§10):
curl "$BASE/metrics?days=30" -H "Authorization: Bearer $TOKEN"
# Response:
# {
#   "period_days": 30,
#   "credits_consumed": 124,
#   "tokens_used": 1482304,
#   "enrichments_run": 124,
#   "avg_confidence_score": 7.8,
#   "leads_created": 340,
#   "leads_by_status": {"new":200,"contacted":80,"replied":35,"converted":20,"dead":5},
#   "campaigns_created": 3,
#   "emails_sent": 210,
#   "emails_failed": 4,
#   "email_success_rate_pct": 98.1
# }
```

---

## Table: API Keys

### CREATE
```bash
curl -X POST $BASE/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Production Key"}'
# Response: {"key":"aiq_sk_xxxx","prefix":"aiq_sk_xxxx...","name":"Production Key"}
# SAVE the key — it's only shown once!
```

### READ — List active keys
```bash
curl $BASE/api-keys -H "Authorization: Bearer $TOKEN"
# Response: {"api_keys":[{"id":"...","name":"Production Key","prefix":"aiq_sk_ab12cd34...","created_at":"..."}]}
# Note: full key is never returned after creation
```

### USE — Authenticate with API key
```bash
curl $BASE/auth/me -H "X-API-Key: aiq_sk_your_full_key_here"
```

### DELETE — Revoke a key
```bash
curl -X DELETE $BASE/api-keys/<key_id> -H "Authorization: Bearer $TOKEN"
# Response: {"message":"API key revoked"}
```

---

## Table: Jobs (Enrichment)

### CREATE — Batch enrichment job
```bash
curl -X POST $BASE/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "April Batch — Enterprise SaaS",
    "companies": ["Salesforce","HubSpot","Pipedrive","Freshworks"],
    "websites": {
      "Salesforce": "https://salesforce.com",
      "HubSpot": "https://hubspot.com"
    }
  }'
# Response: {"job_id":"<uuid>","status":"queued","total_items":4}
```

### READ — List jobs
```bash
curl "$BASE/jobs?page=1&limit=20" -H "Authorization: Bearer $TOKEN"

# Filter by status:
curl "$BASE/jobs?status=running" -H "Authorization: Bearer $TOKEN"
curl "$BASE/jobs?status=completed" -H "Authorization: Bearer $TOKEN"
```

### READ — Poll job progress
```bash
curl $BASE/jobs/<job_id> -H "Authorization: Bearer $TOKEN"
# Response: {"status":"running","progress_pct":50.0,"completed_items":2,"total_items":4,...}
```

### READ — Get enrichment results
```bash
curl "$BASE/jobs/<job_id>/results?page=1&limit=50" -H "Authorization: Bearer $TOKEN"

# Filter by status:
curl "$BASE/jobs/<job_id>/results?status=completed" -H "Authorization: Bearer $TOKEN"
```

### EXPORT — Download results as CSV
```bash
curl $BASE/jobs/<job_id>/export \
  -H "Authorization: Bearer $TOKEN" \
  -o results.csv
```

### DELETE (cancel) — Stop a running job
```bash
curl -X POST $BASE/jobs/<job_id>/cancel -H "Authorization: Bearer $TOKEN"
# Response: {"message":"Job cancelled"}
```

---

## Direct Database Access (psql)

```bash
# Connect to Neon via psql:
psql "postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require"

-- Count leads by status:
SELECT status, COUNT(*) FROM leads GROUP BY status;

-- Top orgs by enrichment count:
SELECT o.name, COUNT(er.id) as enrichments
FROM organizations o
JOIN enrichment_results er ON er.organization_id = o.id
GROUP BY o.name ORDER BY enrichments DESC LIMIT 10;

-- Recent email sends:
SELECT to_email, status, sent_at, error_detail
FROM email_logs ORDER BY sent_at DESC LIMIT 20;

-- Credits consumed per org (last 30 days):
SELECT o.name, SUM(ul.credits_consumed) as credits
FROM usage_logs ul
JOIN organizations o ON o.id = ul.organization_id
WHERE ul.created_at > NOW() - INTERVAL '30 days'
GROUP BY o.name ORDER BY credits DESC;

-- Make a user admin:
UPDATE users SET is_admin = true WHERE email = 'admin@company.com';
```
