#!/bin/bash
# =============================================================================
#  AgentIQ v2 — Full Test Suite
#  Covers: Auth, Jobs, CRM, Leads, Campaigns, Email, Monitoring, Admin
#
#  Usage:
#    chmod +x test.sh && ./test.sh
#    BASE=https://your-api.railway.app ./test.sh   # against production
# =============================================================================

BASE="${BASE:-http://localhost:8000}"
GREEN='\033[0;32m'
RED='\033[0;31m'
AMBER='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

pass=0; fail=0; skip=0
TOKEN=""
LEAD_ID=""
CAMPAIGN_ID=""
JOB_ID=""

header() {
  echo ""
  echo -e "${BLUE}${BOLD}── $1 ──────────────────────────────${NC}"
}

check() {
  local label=$1 cmd=$2 expect=$3
  result=$(eval "$cmd" 2>/dev/null)
  if echo "$result" | grep -q "$expect"; then
    echo -e "  ${GREEN}✓${NC} $label"
    ((pass++))
  else
    echo -e "  ${RED}✗${NC} $label"
    echo -e "    ${AMBER}Expected:${NC} $expect"
    echo -e "    ${AMBER}Got:${NC}      ${result:0:200}"
    ((fail++))
  fi
}

check_skip() {
  echo -e "  ${AMBER}↷${NC} $1 (skipped)"
  ((skip++))
}

echo ""
echo -e "${BOLD}════════════════════════════════════${NC}"
echo -e "${BOLD}  AgentIQ v2 — Full Test Suite${NC}"
echo -e "${BOLD}  Target: ${BLUE}$BASE${NC}"
echo -e "${BOLD}════════════════════════════════════${NC}"

# ── Phase 1: Health ───────────────────────────────────────────────────────────
header "Phase 1: System Health"

check "Health endpoint" \
  "curl -s $BASE/health" \
  '"status"'

check "Swagger UI accessible" \
  "curl -s $BASE/docs" \
  "swagger"

# ── Phase 2: Authentication ───────────────────────────────────────────────────
header "Phase 2: Authentication (§4)"

TS=$(date +%s)
REG=$(curl -s -X POST $BASE/auth/register \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"test_${TS}@agentiq.test\",\"password\":\"Test1234!\",\"full_name\":\"Test User\",\"org_name\":\"TestOrg_${TS}\"}")

TOKEN=$(echo $REG | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token',''))" 2>/dev/null)

if [ -n "$TOKEN" ]; then
  echo -e "  ${GREEN}✓${NC} Register + receive JWT"
  ((pass++))
else
  echo -e "  ${RED}✗${NC} Register failed — Response: $REG"
  ((fail++))
fi

check "GET /auth/me returns user data" \
  "curl -s $BASE/auth/me -H 'Authorization: Bearer $TOKEN'" \
  '"email"'

check "POST /auth/refresh-token issues new token" \
  "curl -s -X POST $BASE/auth/refresh-token -H 'Authorization: Bearer $TOKEN'" \
  '"access_token"'

check "POST /auth/change-password — wrong current password → 400" \
  "curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/auth/change-password \
    -H 'Authorization: Bearer $TOKEN' \
    -H 'Content-Type: application/json' \
    -d '{\"current_password\":\"WrongPass!\",\"new_password\":\"NewPass2@\"}'" \
  "400"

check "Unauthenticated request → 401" \
  "curl -s -o /dev/null -w '%{http_code}' $BASE/dashboard/stats" \
  "401"

# API key test
KEY_RESP=$(curl -s -X POST $BASE/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Suite Test Key"}')
API_KEY=$(echo $KEY_RESP | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('key',''))" 2>/dev/null)

if [ -n "$API_KEY" ]; then
  echo -e "  ${GREEN}✓${NC} Create API key"
  ((pass++))
  check "API key auth works" \
    "curl -s $BASE/auth/me -H 'X-API-Key: $API_KEY'" \
    '"email"'
else
  echo -e "  ${RED}✗${NC} Create API key — Response: $KEY_RESP"
  ((fail++))
fi

check "Invalid JWT → 401" \
  "curl -s -o /dev/null -w '%{http_code}' $BASE/dashboard/stats \
    -H 'Authorization: Bearer invalid.jwt.token'" \
  "401"

# ── Phase 3: Enrichment Jobs (§3 DB, §2 AI) ──────────────────────────────────
header "Phase 3: Enrichment Jobs (§2 §3)"

JOB_RESP=$(curl -s -X POST $BASE/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Test Batch","companies":["Stripe","Notion"]}')

JOB_ID=$(echo $JOB_RESP | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('job_id',''))" 2>/dev/null)

if [ -n "$JOB_ID" ]; then
  echo -e "  ${GREEN}✓${NC} Create batch job (2 companies)"
  ((pass++))
else
  echo -e "  ${RED}✗${NC} Create job — Response: $JOB_RESP"
  ((fail++))
fi

check "List jobs returns jobs array" \
  "curl -s $BASE/jobs -H 'Authorization: Bearer $TOKEN'" \
  '"jobs"'

if [ -n "$JOB_ID" ]; then
  check "Get job by ID" \
    "curl -s $BASE/jobs/$JOB_ID -H 'Authorization: Bearer $TOKEN'" \
    '"status"'

  check "Get job results" \
    "curl -s $BASE/jobs/$JOB_ID/results -H 'Authorization: Bearer $TOKEN'" \
    '"results"'

  check "Cancel job" \
    "curl -s -X POST $BASE/jobs/$JOB_ID/cancel -H 'Authorization: Bearer $TOKEN'" \
    '"message"'

  check "Export job CSV → 200" \
    "curl -s -o /dev/null -w '%{http_code}' $BASE/jobs/$JOB_ID/export \
      -H 'Authorization: Bearer $TOKEN'" \
    "200"
fi

# ── Phase 4: Dashboard (§10) ──────────────────────────────────────────────────
header "Phase 4: Dashboard & Stats (§10)"

check "Dashboard stats returns enrichment count" \
  "curl -s $BASE/dashboard/stats -H 'Authorization: Bearer $TOKEN'" \
  '"total_enrichments"'

check "Billing usage endpoint" \
  "curl -s '$BASE/billing/usage?days=30' -H 'Authorization: Bearer $TOKEN'" \
  '"credits_used"'

# ── Phase 5: Leads CRM (§5) ───────────────────────────────────────────────────
header "Phase 5: CRM — Leads (§5)"

LEAD_RESP=$(curl -s -X POST $BASE/leads \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"company_name":"TestCorp","contact_name":"Jane Smith","email":"jane@testcorp.com","industry":"SaaS","phone":"+1 415 000 0001","website":"https://testcorp.com","headquarters":"San Francisco, CA","notes":"Automated test lead"}')

LEAD_ID=$(echo $LEAD_RESP | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null)

if [ -n "$LEAD_ID" ]; then
  echo -e "  ${GREEN}✓${NC} Create lead"
  ((pass++))
else
  echo -e "  ${RED}✗${NC} Create lead — Response: $LEAD_RESP"
  ((fail++))
fi

check "List leads returns paginated array" \
  "curl -s $BASE/leads -H 'Authorization: Bearer $TOKEN'" \
  '"leads"'

check "Pipeline summary returns status dict" \
  "curl -s $BASE/leads/pipeline -H 'Authorization: Bearer $TOKEN'" \
  '"new"'

check "Filter leads by status" \
  "curl -s '$BASE/leads?status=new' -H 'Authorization: Bearer $TOKEN'" \
  '"leads"'

check "Search leads by company name" \
  "curl -s '$BASE/leads?search=TestCorp' -H 'Authorization: Bearer $TOKEN'" \
  '"leads"'

if [ -n "$LEAD_ID" ]; then
  check "Get lead by ID" \
    "curl -s $BASE/leads/$LEAD_ID -H 'Authorization: Bearer $TOKEN'" \
    '"company_name"'

  check "PATCH lead — advance to contacted" \
    "curl -s -X PATCH $BASE/leads/$LEAD_ID \
      -H 'Authorization: Bearer $TOKEN' \
      -H 'Content-Type: application/json' \
      -d '{\"status\":\"contacted\"}'" \
    '"contacted"'

  check "Add outbound conversation to lead" \
    "curl -s -X POST $BASE/leads/$LEAD_ID/conversations \
      -H 'Authorization: Bearer $TOKEN' \
      -H 'Content-Type: application/json' \
      -d '{\"channel\":\"email\",\"direction\":\"outbound\",\"subject\":\"Test Email\",\"body\":\"Hello!\"}'" \
    '"id"'

  check "Get lead conversations" \
    "curl -s $BASE/leads/$LEAD_ID/conversations -H 'Authorization: Bearer $TOKEN'" \
    '"conversations"'

  check "Lead cross-org access → 404" \
    "curl -s -o /dev/null -w '%{http_code}' \
      $BASE/leads/00000000-0000-0000-0000-000000000000 \
      -H 'Authorization: Bearer $TOKEN'" \
    "404"
fi

check_skip "Lead AI enrichment — run: curl -X POST $BASE/leads/${LEAD_ID:-<id>}/enrich -H 'Authorization: Bearer <token>'"

# ── Phase 5b: CSV Import/Export (§5) ─────────────────────────────────────────
header "Phase 5b: CSV Import / Export (§5)"

CSV_FILE=$(mktemp /tmp/test_leads_XXXX.csv)
cat > "$CSV_FILE" << 'CSVEOF'
company name,contact name,email,phone,website,industry,location
AlphaCorp,Alice Jones,alice@alphacorp.com,+1 555 1001,https://alphacorp.com,Fintech,New York NY
BetaLabs,Bob Smith,bob@betalabs.io,+1 555 1002,https://betalabs.io,AI/ML,Austin TX
GammaSaaS,,info@gammasaas.com,,https://gammasaas.com,SaaS,
CSVEOF

IMPORT_RESP=$(curl -s -X POST $BASE/leads/import/csv \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@$CSV_FILE")

CREATED=$(echo $IMPORT_RESP | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('created',0))" 2>/dev/null)

if [ "${CREATED:-0}" -ge "1" ] 2>/dev/null; then
  echo -e "  ${GREEN}✓${NC} CSV import — created $CREATED leads"
  ((pass++))
else
  echo -e "  ${RED}✗${NC} CSV import — Response: $IMPORT_RESP"
  ((fail++))
fi

rm -f "$CSV_FILE"

check "CSV export returns text/csv content-type" \
  "curl -s -o /dev/null -w '%{content_type}' '$BASE/leads/export/csv' \
    -H 'Authorization: Bearer $TOKEN'" \
  "text/csv"

# ── Phase 6: Campaigns & Email (§6) ──────────────────────────────────────────
header "Phase 6: Campaigns & Email (§6)"

CAMP_RESP=$(curl -s -X POST $BASE/campaigns \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Test Campaign","subject":"Hello {{company}}!","body_template":"Hi {{first_name}},\n\nSaw you work at {{company}} in {{industry}}.\n\nBest,","send_rate":10}')

CAMPAIGN_ID=$(echo $CAMP_RESP | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null)

if [ -n "$CAMPAIGN_ID" ]; then
  echo -e "  ${GREEN}✓${NC} Create campaign with template variables"
  ((pass++))
else
  echo -e "  ${RED}✗${NC} Create campaign — Response: $CAMP_RESP"
  ((fail++))
fi

check "List campaigns" \
  "curl -s $BASE/campaigns -H 'Authorization: Bearer $TOKEN'" \
  '"campaigns"'

if [ -n "$CAMPAIGN_ID" ]; then
  check "Get campaign by ID" \
    "curl -s $BASE/campaigns/$CAMPAIGN_ID -H 'Authorization: Bearer $TOKEN'" \
    '"name"'

  check "Template preview (sample data)" \
    "curl -s $BASE/campaigns/$CAMPAIGN_ID/preview -H 'Authorization: Bearer $TOKEN'" \
    '"rendered_subject"'

  if [ -n "$LEAD_ID" ]; then
    check "Template preview with specific lead" \
      "curl -s '$BASE/campaigns/$CAMPAIGN_ID/preview?lead_id=$LEAD_ID' \
        -H 'Authorization: Bearer $TOKEN'" \
      '"rendered_subject"'

    check "Add lead to campaign" \
      "curl -s -X POST $BASE/campaigns/$CAMPAIGN_ID/leads \
        -H 'Authorization: Bearer $TOKEN' \
        -H 'Content-Type: application/json' \
        -d '[\"$LEAD_ID\"]'" \
      '"added"'
  fi
fi

# Email SMTP test — expects 503 when SMTP_HOST not configured
check "POST /email/test → 503 when SMTP not configured" \
  "curl -s -o /dev/null -w '%{http_code}' -X POST $BASE/email/test \
    -H 'Authorization: Bearer $TOKEN' \
    -H 'Content-Type: application/json' \
    -d '{\"to_email\":\"test@example.com\"}'" \
  "503"

check "GET /email/logs accessible" \
  "curl -s '$BASE/email/logs' -H 'Authorization: Bearer $TOKEN'" \
  '"logs"'

# ── Phase 7: Monitoring (§10) ─────────────────────────────────────────────────
header "Phase 7: Monitoring & Metrics (§10)"

check "GET /logs returns usage log array" \
  "curl -s '$BASE/logs' -H 'Authorization: Bearer $TOKEN'" \
  '"logs"'

check "GET /logs filter by action" \
  "curl -s '$BASE/logs?action=enrichment' -H 'Authorization: Bearer $TOKEN'" \
  '"logs"'

check "GET /metrics returns aggregated metrics" \
  "curl -s '$BASE/metrics?days=30' -H 'Authorization: Bearer $TOKEN'" \
  '"credits_consumed"'

check "GET /metrics 7-day window" \
  "curl -s '$BASE/metrics?days=7' -H 'Authorization: Bearer $TOKEN'" \
  '"period_days"'

# ── Phase 8: Security (§9) ────────────────────────────────────────────────────
header "Phase 8: Security (§9)"

check "Invalid API key → 401" \
  "curl -s -o /dev/null -w '%{http_code}' $BASE/auth/me \
    -H 'X-API-Key: aiq_sk_fakekeyfakekeyfake'" \
  "401"

check "Admin endpoints → 403 for non-admin user" \
  "curl -s -o /dev/null -w '%{http_code}' $BASE/admin/users \
    -H 'Authorization: Bearer $TOKEN'" \
  "403"

check "GET /admin/stats → 403 for non-admin" \
  "curl -s -o /dev/null -w '%{http_code}' $BASE/admin/stats \
    -H 'Authorization: Bearer $TOKEN'" \
  "403"

# ── Phase 9: Cleanup ──────────────────────────────────────────────────────────
header "Phase 9: Cleanup"

if [ -n "$LEAD_ID" ]; then
  check "Delete test lead" \
    "curl -s -X DELETE $BASE/leads/$LEAD_ID -H 'Authorization: Bearer $TOKEN'" \
    '"message"'
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}════════════════════════════════════${NC}"
echo -e "  ${GREEN}Passed:${NC}  ${BOLD}$pass${NC}"
echo -e "  ${RED}Failed:${NC}  ${BOLD}$fail${NC}"
echo -e "  ${AMBER}Skipped:${NC} ${BOLD}$skip${NC}"
echo -e "${BOLD}════════════════════════════════════${NC}"

if [ $fail -eq 0 ]; then
  echo -e "\n${GREEN}${BOLD}All tests passed! 🎉${NC}"
else
  echo -e "\n${RED}${BOLD}$fail test(s) failed.${NC}"
  echo "  Check logs: docker compose logs api  (or journalctl -u agentiq-api -n 50)"
fi

echo ""
echo -e "${AMBER}Manual tests (run separately — need live Groq key):${NC}"
echo "  # Single enrichment:"
echo "  curl -X POST $BASE/enrich/single -H 'Authorization: Bearer $TOKEN' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"company_name\":\"Notion\",\"website\":\"https://notion.so\"}'"
echo ""
echo "  # Lead AI enrichment (requires GROQ_API_KEY in .env):"
[ -n "$LEAD_ID" ] \
  && echo "  curl -X POST $BASE/leads/$LEAD_ID/enrich -H 'Authorization: Bearer $TOKEN'" \
  || echo "  curl -X POST $BASE/leads/<lead_id>/enrich -H 'Authorization: Bearer $TOKEN'"
echo ""
echo "  # SMTP test (requires SMTP_HOST in .env):"
echo "  curl -X POST $BASE/email/test -H 'Authorization: Bearer $TOKEN' \\"
echo "    -H 'Content-Type: application/json' -d '{\"to_email\":\"you@example.com\"}'"
echo ""

[ $fail -ne 0 ] && exit 1 || exit 0
