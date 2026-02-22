## AgentIQ v2 — Debug Guide
## Every common error, its cause, and exact fix

```
══════════════════════════════════════════════════════════
§1 STARTUP ERRORS
══════════════════════════════════════════════════════════
```

### RuntimeError: STARTUP ABORTED
```
🚨 STARTUP ABORTED — Fix these .env issues:
  ✗  No AI API key. Set GROQ_API_KEY=gsk_...
```
**Cause:** validate_config() ran and found missing required .env values.
**Fix:** Open `.env`, fill in the missing values shown in the error.
**Test:**
```bash
python -c "from core.startup_check import validate_config; validate_config()"
```

### ImportError: No module named 'groq'
**Cause:** Dependencies not installed, or wrong virtualenv active.
**Fix:**
```bash
source venv/bin/activate   # activate venv first
pip install -r backend/requirements.txt
python -c "import groq; print(groq.__version__)"
```

### Port already in use: [Errno 98] Address already in use
**Cause:** Something else running on port 8000.
**Fix:**
```bash
lsof -ti:8000 | xargs kill -9  # kill whatever's on 8000
uvicorn api.main:app --reload   # restart
```

```
══════════════════════════════════════════════════════════
§2 DATABASE ERRORS
══════════════════════════════════════════════════════════
```

### asyncpg.exceptions.InvalidPasswordError
```
asyncpg.exceptions.InvalidPasswordError: password authentication failed for user "neon"
```
**Cause:** Wrong credentials in DATABASE_URL.
**Fix:**
```bash
# Go to Neon dashboard → your project → Connection Details → copy exact URL
# Paste into .env as DATABASE_URL (asyncpg) and DATABASE_URL_SYNC (psycopg2)
DATABASE_URL=postgresql+asyncpg://user:CORRECT_PASS@host/db?sslmode=require
```

### asyncpg.exceptions.TooManyConnectionsError
```
asyncpg.exceptions.TooManyConnectionsError: too many connections
```
**Cause:** Neon free tier has a 10-connection hard cap. Pool exhausted.
**Fix:**
```bash
# In .env — reduce pool size:
DB_POOL_SIZE=3
DB_MAX_OVERFLOW=1
# Restart API and Celery worker
```
**Debug:**
```bash
# Check active connections in Neon:
curl http://localhost:8000/health | python3 -m json.tool
# Look at: db_pool.checked_out
```

### SSL: CERTIFICATE_VERIFY_FAILED
**Cause:** Missing `?sslmode=require` in DATABASE_URL.
**Fix:**
```bash
DATABASE_URL=postgresql+asyncpg://user:pass@host/db?sslmode=require
#                                                         ^^^^^^^^^^ add this
```

### sqlalchemy.exc.ProgrammingError: relation "leads" does not exist
**Cause:** Tables not created yet.
**Fix:**
```bash
python -c "
import asyncio
from db.database import init_db
asyncio.run(init_db())
print('Tables created')
"
# Or with Alembic:
alembic upgrade head
```

### asyncpg.exceptions.UniqueViolationError: duplicate key value
```
asyncpg.exceptions.UniqueViolationError: duplicate key value violates unique constraint "uq_results_job_company"
```
**Cause:** Trying to enrich the same company in the same job twice.
**Fix:** This is normal — the constraint prevents duplicate work. Job will continue.

```
══════════════════════════════════════════════════════════
§3 AI / GROQ ERRORS
══════════════════════════════════════════════════════════
```

### groq.AuthenticationError: Invalid API Key
**Cause:** GROQ_API_KEY is wrong, expired, or not set.
**Fix:**
```bash
# Check .env:
grep GROQ_API_KEY backend/.env

# Get new key: https://console.groq.com → API Keys → Create

# Test key directly:
python -c "
from groq import Groq
client = Groq(api_key='gsk_YOUR_KEY')
r = client.chat.completions.create(
    model='llama-3.3-70b-versatile',
    messages=[{'role':'user','content':'Say hi'}],
    max_tokens=10
)
print('✓ Key works:', r.choices[0].message.content)
"
```

### groq.RateLimitError: rate_limit_exceeded
```
groq.RateLimitError: Rate limit reached for model `llama-3.3-70b-versatile`
```
**Cause:** Groq free tier: ~30 requests/min, ~6,000 tokens/min.
**Fix:** Already handled with exponential backoff (2/4/8s). No action needed.
**Reduce frequency:**
```bash
# In .env — reduce token usage per call:
GROQ_MAX_TOKENS=2048       # was 4096
GROQ_MAX_TOOL_ITERATIONS=8 # was 15
```

### groq.APITimeoutError: Request timed out
**Cause:** Groq took >60s — usually when max_tokens is too high or during high load.
**Fix:**
```bash
# In .env:
GROQ_MAX_TOKENS=2048
# Reduce iterations:
GROQ_MAX_TOOL_ITERATIONS=8
```

### JSON parse error: No JSON found. Preview: ...
```
Parse error for Stripe: No JSON found. Preview: Based on my research, here's what I found...
```
**Cause:** LLM returned prose instead of JSON — usually when it ran out of tokens.
**Fix:**
```bash
# Increase token budget:
GROQ_MAX_TOKENS=4096

# Enable debug logging to see raw output:
LOG_LEVEL=DEBUG
```
**Debug:**
```bash
python -c "
from agents.enrichment_agent import EnrichmentAgent
agent = EnrichmentAgent()
result = agent._extract_json('Based on my research...')
print('Extracted:', result)
"
```

### ValueError: No Groq API key found
```
ValueError: No Groq API key found. Set GROQ_API_KEY=gsk_... or API_KEY=gsk_... in .env
```
**Cause:** Neither GROQ_API_KEY nor API_KEY is set.
**Fix:** Add to .env and restart.

```
══════════════════════════════════════════════════════════
§4 AUTH ERRORS
══════════════════════════════════════════════════════════
```

### HTTP 401: Could not validate credentials
**Cause 1:** Token expired (default: 24h).
**Fix:** Re-login to get a new token.
```bash
curl -X POST http://localhost:8000/auth/login \
  -d "username=you@company.com&password=yourpass"
```

**Cause 2:** Wrong SECRET_KEY — token signed with different key.
**Fix:** Don't change SECRET_KEY in production (invalidates all existing tokens).

**Cause 3:** Token not in header.
**Fix:**
```bash
# Always include:
curl -H "Authorization: Bearer eyJ..."
#       ^^^ capital B, space between Bearer and token
```

### HTTP 403: Admin access required
**Cause:** Route needs `is_admin=True` but user isn't admin.
**Fix:**
```sql
-- Run in Neon SQL editor:
UPDATE users SET is_admin = true WHERE email = 'your@email.com';
```

### jose.exceptions.JWTError: Signature verification failed
**Cause:** SECRET_KEY changed after tokens were issued.
**Fix:** Users need to log in again. All old tokens are invalid.

```
══════════════════════════════════════════════════════════
§5 EMAIL ERRORS
══════════════════════════════════════════════════════════
```

### smtplib.SMTPAuthenticationError: (535, ...) Username and Password not accepted
**Cause:** Gmail requires App Password (not account password) when 2FA is on.
**Fix:**
```
1. Go to: myaccount.google.com → Security
2. Enable 2-Step Verification (if not already)
3. Search for "App Passwords"
4. Create → Mail → Other → "AgentIQ"
5. Copy the 16-char password → paste into SMTP_PASSWORD in .env
```

### smtplib.SMTPConnectError: Connection refused
**Cause:** Wrong SMTP_HOST / SMTP_PORT.
**Common settings:**
```bash
# Gmail (STARTTLS):
SMTP_HOST=smtp.gmail.com  SMTP_PORT=587  SMTP_USE_TLS=false

# Gmail (SSL):
SMTP_HOST=smtp.gmail.com  SMTP_PORT=465  SMTP_USE_TLS=true

# SendGrid:
SMTP_HOST=smtp.sendgrid.net  SMTP_PORT=587  SMTP_USERNAME=apikey

# Mailgun:
SMTP_HOST=smtp.mailgun.org  SMTP_PORT=587
```

### smtplib.SMTPRecipientsRefused
**Cause:** Invalid recipient email or your IP is rate-limited by the receiving server.
**Fix:** Check the email address is valid. Lower SMTP_RATE_LIMIT.

### HTTP 503: Email not configured
**Cause:** SMTP_HOST not set in .env.
**Fix:** Add SMTP settings to .env, restart API.

### Campaign runs but 0 emails sent
**Cause 1:** No leads added to campaign. Check total_leads > 0.
**Cause 2:** All leads already have status != "pending" in campaign_leads.
**Fix:**
```bash
# Debug — check campaign_leads:
curl http://localhost:8000/campaigns/{id} -H "Authorization: Bearer $TOKEN"
# Look at total_leads, and check leads are in "pending" status
```

```
══════════════════════════════════════════════════════════
§6 CELERY / WORKER ERRORS
══════════════════════════════════════════════════════════
```

### redis.exceptions.ConnectionError: Error 111 connecting to localhost:6379
**Cause:** Redis not running.
**Fix:**
```bash
redis-server   # start Redis
# Or on Linux:
sudo systemctl start redis
# Test:
redis-cli ping  # should return PONG
```

### celery.exceptions.NotRegistered: 'services.worker.enrich_job_task'
**Cause:** Worker started with wrong module reference.
**Fix:**
```bash
# Must use -A services.worker (not -A worker):
celery -A services.worker worker -Q enrichment,priority -l info

# If still failing, check:
cd backend
python -c "from services.worker import enrich_job_task; print('✓ task found')"
```

### Job stuck in "running" status forever
**Cause:** Worker process was killed (OOM, deploy restart, SIGKILL) mid-job.
**Fix:**
```bash
# Option 1: Auto-cleanup via job_recovery.py (runs on /health):
curl http://localhost:8000/health  # triggers stuck_job_cleanup()

# Option 2: Manual SQL:
# In Neon SQL editor:
UPDATE jobs
SET status = 'failed', error_message = 'Manually reset stuck job', completed_at = NOW()
WHERE status IN ('running', 'queued')
  AND started_at < NOW() - INTERVAL '2 hours';
```

### SoftTimeLimitExceeded (before fix)
**Cause:** Job ran for >1 hour — hit Celery soft time limit.
**Current behavior (after fix):** Job is marked `partial` with results saved so far.
**Action:** Re-launch the job with the remaining companies.

```
══════════════════════════════════════════════════════════
§7 LEAD / CSV IMPORT ERRORS
══════════════════════════════════════════════════════════
```

### CSV import: 0 rows created
**Cause 1:** CSV has no recognized column headers.
**Fix:** Check your CSV has these headers (case-insensitive):
```
company, email, name, contact name, phone, website, industry, linkedin
```

**Cause 2:** All rows have no email AND no company_name → all skipped.
**Fix:** Ensure at least one of email or company column is populated.

### UnicodeDecodeError when importing CSV
**Cause:** CSV is not UTF-8 (common with Excel exports).
**Fix:** The importer tries latin-1 as fallback. If still failing:
```bash
# Convert CSV to UTF-8 first:
iconv -f cp1252 -t utf-8 input.csv > output_utf8.csv
```

### Duplicate leads after import
**Cause:** Leads without email can't be deduplicated (email is the dedup key).
**Fix:** Ensure email column is populated. Leads with email are upserted (not duplicated).

```
══════════════════════════════════════════════════════════
§8 RATE LIMITING
══════════════════════════════════════════════════════════
```

### HTTP 429: Rate limit exceeded
**Cause:** >60 requests/minute from your IP.
**Fix for development:**
```bash
# Increase limit in .env:
RATE_LIMIT_PER_MINUTE=300

# Or whitelist your own IP (add to rate_limit middleware):
EXEMPT_IPS=127.0.0.1,::1
```

```
══════════════════════════════════════════════════════════
§9 LOGGING QUICK REFERENCE
══════════════════════════════════════════════════════════
```

```python
import logging
logger = logging.getLogger(__name__)

# Different log levels:
logger.debug("Dev-only detail: %s", value)        # LOG_LEVEL=DEBUG only
logger.info("Normal operation: %s", action)        # default
logger.warning("Recoverable issue: %s", issue)     # something wrong but ok
logger.error("Operation failed: %s", error)        # action needed
logger.critical("System failure: %s", critical)    # wake up at 3am

# With exception traceback (use in except blocks):
logger.error("Job %s failed", job_id, exc_info=True)
```

```bash
# Set log level in .env:
LOG_LEVEL=DEBUG   # show everything including DB queries
LOG_LEVEL=INFO    # normal (default)
LOG_LEVEL=WARNING # quiet — only warnings and above

# View logs on Render/Railway:
# Render: Dashboard → your service → Logs tab
# Railway: railway logs --follow

# Local — filter to errors only:
uvicorn api.main:app --reload 2>&1 | grep -E "ERROR|WARNING"
```

```
══════════════════════════════════════════════════════════
§10 HEALTH CHECK
══════════════════════════════════════════════════════════
```

```bash
# Full system check:
curl http://localhost:8000/health | python3 -m json.tool

# Expected healthy response:
{
  "status": "ok",
  "version": "2.0.0",
  "environment": "development",
  "issues": [],
  "db_pool": {"size": 5, "checked_out": 1, "overflow": 0},
  "ai_key": "configured",
  "jobs": {"queued": 0, "running": 0, "stuck": 0, "failed_24h": 1, "completed_24h": 5},
  "smtp": "configured"
}

# Degraded response example:
{
  "status": "degraded",
  "issues": ["2_stuck_jobs", "ai_key_missing"],
  ...
}
```
