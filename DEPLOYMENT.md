## AgentIQ v2 — Complete Deployment Guide
## Phase-by-phase: Local → Production

```
══════════════════════════════════════════════════════════
PHASE 1: LOCAL SETUP
══════════════════════════════════════════════════════════
```

### Prerequisites
```bash
python 3.11+     # python --version
redis-server     # brew install redis  (Mac) or sudo apt install redis (Ubuntu)
```

### 1. Clone and install
```bash
git clone <your-repo> && cd agentiq_v2

# Create virtualenv
python3 -m venv venv && source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate  # Windows

# Install dependencies
cd backend
pip install -r requirements.txt

# Test: should print versions
python -c "import fastapi, sqlalchemy, groq; print('✓ all packages installed')"
```

### 2. Configure .env
```bash
cp backend/.env.example backend/.env
# Edit .env — minimum required:

# GROQ_API_KEY — free at https://console.groq.com
GROQ_API_KEY=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

# DATABASE_URL — free at https://neon.tech
DATABASE_URL=
DATABASE_URL_SYNC=
# SECRET_KEY — generate with:
# python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=4e5da20ab23a36d082828634b814c54cd63b34a8271f4f323bdac33562c26738

# SMTP — for email campaigns (optional locally)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your_app_password  # NOT your real password
```

### 3. Run database migrations
```bash
cd backend

# Option A: Auto-create tables (uses SQLAlchemy CREATE TABLE IF NOT EXISTS)

python -c "
import asyncio
from db.database import init_db
asyncio.run(init_db())
print('✓ Tables created successfully!')"



# Opticd backend && python -c "import asyncio; from db.database import init_db; asyncio.run(init_db()); print('✓ Tables created successfully!')"on B: Alembic migrations (for production schema changes)
alembic upgrade head

# Verify tables were created:
python -c "
import asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()

async def check():
    conn = await asyncpg.connect(os.getenv('DATABASE_URL').replace('+asyncpg',''))
    rows = await conn.fetch(("\"SELECT tablename FROM pg_tables WHERE schemaname='public'\""))
    
    for r in rows: print('✓ Table:', r['tablename'])
    await conn.close()

asyncio.run(check())
"
```

### 4. Run the API
```bash
cd backend
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

# Expected output:
# ✓ Config validated (env=development, warnings=0)
# AgentIQ v2 started | env=development | model=llama-3.3-70b-versatile | db=✓

# Test:
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
# → {"status":"ok","version":"2.0.0","environment":"development"}

curl http://localhost:8000/docs
# → Swagger UI with all 50+ endpoints
```

### 5. Run Celery worker (for batch AI jobs)
```bash
# In a separate terminal:
cd backend
 \
  -Q enrichment,priority \
  --loglevel=info \
  --concurrency=2

# Expected output:
# [2024-01-15 09:00:00,000: INFO/MainProcess] celery@localhost ready.
# [2024-01-15 09:00:00,000: INFO/MainProcess] Connected to redis://localhost:6379/1

# Monitor tasks (optional):
pip install flower
celery -A services.worker flower --port=5555
# → http://localhost:5555
```

```
══════════════════════════════════════════════════════════
PHASE 2: TEST ALL FEATURES
══════════════════════════════════════════════════════════
```

### Register and get token
```bash
# Register
http://localhost:8000/docs
# Login
curl -X POST http://localhost:8000/leads \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Stripe", "email": "jane@stripe.com"}'
# → {"access_token": "eyJ...", "token_type": "bearer"}

export TOKEN="eyJ..."  # save it
```

### Test leads (CRM)
```bash
# Create lead
curl -X POST http://localhost:8000/leads \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"company_name": "Stripe", "email": "jane@stripe.com"}'

# List leads
curl http://localhost:8000/leads -H "Authorization: Bearer $TOKEN"

# Import CSV
curl -X POST http://localhost:8000/leads/import \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/path/to/leads.csv"

# Enrich with AI
curl -X POST http://localhost:8000/leads/{lead_id}/enrich \
  -H "Authorization: Bearer $TOKEN"
# Takes] 20-60 seconds

# Update status
curl -X PATCH http://localhost:8000/leads/{lead_id}/status \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status": "contacted"}'
```

### Test email campaign
```bash
# Test SMTP first
curl -X POST http://localhost:8000/email/test \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"to_email": "you@gmail.com"}'

# Create campaign
curl -X POST http://localhost:8000/campaigns \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Campaign",
    "subject": "Hey {{first_name}}, quick question",
    "body_template": "<p>Hi {{first_name}},</p><p>We help {{industry}} companies...</p>",
    "send_rate": 10
  }'

# Add leads to campaign
curl -X POST http://localhost:8000/campaigns/{campaign_id}/leads \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '["lead-uuid-1", "lead-uuid-2"]'

# Preview rendered email
curl "http://localhost:8000/campaigns/{campaign_id}/preview" \
  -H "Authorization: Bearer $TOKEN"

# Send!
curl -X POST http://localhost:8000/campaigns/{campaign_id}/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### Test AI enrichment batch
```bash
curl -X POST http://localhost:8000/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Batch",
    "companies": ["Stripe", "Notion", "Linear"],
    "websites": {"Stripe": "https://stripe.com"}
  }'
# → {"job_id": "..."}

# Monitor progress
curl http://localhost:8000/jobs/{job_id} \
  -H "Authorization: Bearer $TOKEN"
# → {"status": "running", "progress_pct": 33.3, "completed_items": 1}
```

```
══════════════════════════════════════════════════════════
PHASE 3: DEPLOY TO RENDER (FREE TIER)
══════════════════════════════════════════════════════════
```

### Render.com setup (recommended for beginners)

```bash
# 1. Push your code to GitHub first
git init && git add . && git commit -m "Initial commit"
git remote add origin https://github.com/yourname/agentiq
git push origin main
```

**On render.com:**
1. New → Web Service → Connect GitHub repo
2. Settings:
   - **Build Command:** `pip install -r backend/requirements.txt`
   - **Start Command:** `cd backend && uvicorn api.main:app --host 0.0.0.0 --port $PORT`
   - **Root Directory:** (leave blank)
3. Add environment variables (copy from your .env):
   - `GROQ_API_KEY`, `DATABASE_URL`, `SECRET_KEY`, `SMTP_*`, `REDIS_URL`
   - `ENVIRONMENT=production`

**For Celery worker on Render:**
1. New → Background Worker
2. Start Command: `cd backend && celery -A services.worker worker -Q enrichment,priority --concurrency=2`

**Redis on Render:**
1. New → Redis (or use Upstash free tier at upstash.com)
2. Copy the Redis URL to `REDIS_URL` / `CELERY_BROKER_URL`

```
══════════════════════════════════════════════════════════
PHASE 4: DEPLOY TO RAILWAY
══════════════════════════════════════════════════════════
```

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login and deploy
railway login
railway new
railway link  # link to your project

# Set environment variables
railway variables set GROQ_API_KEY=gsk_...
railway variables set DATABASE_URL=postgresql+asyncpg://...
railway variables set SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
railway variables set ENVIRONMENT=production

# Deploy
railway up

# View logs
railway logs
```

```
══════════════════════════════════════════════════════════
PHASE 5: DOCKER (OPTIONAL)
══════════════════════════════════════════════════════════
```

```bash
# Build and run with docker-compose
docker-compose up --build

# Services started:
#   api      → http://localhost:8000
#   worker   → background Celery worker
#   redis    → localhost:6379
#   flower   → http://localhost:5555 (task monitor)

# Run migrations inside container
docker-compose exec api python -c "
import asyncio
from db.database import init_db
asyncio.run(init_db())
"

# Stop
docker-compose down
```

```
══════════════════════════════════════════════════════════
PHASE 6: CUSTOM DOMAIN + HTTPS
══════════════════════════════════════════════════════════
```

**Render / Railway:** Automatic HTTPS via Let's Encrypt.
- Settings → Custom Domain → Add `api.yourdomain.com`
- Add CNAME record in your DNS: `api → yourapp.onrender.com`

**VPS (manual HTTPS with nginx):**
```bash
# Install nginx + certbot
sudo apt install nginx certbot python3-certbot-nginx

# Configure nginx
sudo nano /etc/nginx/sites-available/agentiq
```

```nginx
server {
    listen 80;
    server_name api.yourdomain.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;  # for long AI enrichment requests
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/agentiq /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Get SSL certificate (free)
sudo certbot --nginx -d api.yourdomain.com
# → HTTPS now active. Auto-renews every 90 days.

# Run API as systemd service
sudo nano /etc/systemd/system/agentiq.service
```

```ini
[Unit]
Description=AgentIQ API
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/agentiq/backend
Environment="PATH=/home/ubuntu/agentiq/venv/bin"
EnvironmentFile=/home/ubuntu/agentiq/backend/.env
ExecStart=/home/ubuntu/agentiq/venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable agentiq
sudo systemctl start agentiq
sudo systemctl status agentiq
```

```
══════════════════════════════════════════════════════════
COMMON DEPLOYMENT ERRORS + FIXES
══════════════════════════════════════════════════════════
```

```
ERROR: ModuleNotFoundError: No module named 'groq'
FIX:   pip install -r backend/requirements.txt
       Check you're in the correct venv: which python

ERROR: asyncpg.exceptions.InvalidPasswordError: password authentication failed
FIX:   Check DATABASE_URL in .env — user/password must match Neon dashboard

ERROR: RuntimeError: STARTUP ABORTED — Fix these .env issues
FIX:   Read the error message — it tells you exactly which .env keys are missing

ERROR: redis.exceptions.ConnectionError: Error 111 connecting to localhost:6379
FIX:   Start Redis: redis-server
       Or set CELERY_BROKER_URL to Upstash Redis URL

ERROR: smtplib.SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted')
FIX:   Gmail requires App Password (not real password)
       Google Account → Security → 2-Step Verification → App Passwords → Create

ERROR: uvicorn: error: unrecognized arguments: --reload
FIX:   Remove --reload in production. Use: uvicorn api.main:app --host 0.0.0.0 --port 8000
```
