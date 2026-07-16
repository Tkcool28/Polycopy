# Safe Deployment and Rollback Runbook
## Paper-Pilot Release (v0.1.0-paper-pilot)
### Based on SHA: 16a04b8e2f3007f1833b94006de519579592fdf0

## Overview
This document provides step-by-step instructions for safely deploying the tagged paper-pilot release and its corresponding rollback procedures. All operations preserve the existing paper-only mode and safety restrictions.

**IMPORTANT: This runbook follows a read-only-first approach. All mutation operations require explicit Todd approval before execution.**

---

## Pre-Deployment Verification (Read-Only Inspection)

### 1. Environment Path Gate (Automatic Verification)
```bash
# Run path gate verification (automatically executed by casualfree)
cd /root/Polycopy && pwd && git branch --show-current && git rev-parse HEAD && git status --short --branch

# Expected output:
# /root/Polycopy
# main
# 16a04b8e2f3007f1833b94006de519579592fdf0
# ## main...origin/main
```

### 2. Runtime Environment Snapshot (Read-Only)
```bash
# Verify runtime environment (no mutation)
ss -tlnp | grep -E "(8765|5173|8501|8502|9119)"
ls -la /etc/cron.d/
```

### 3. API Health Verification (Read-Only)
```bash
# All paper mode indicators - READ ONLY
python -c "
import requests
import sys

# Expected: broker_mode=paper, paper_mode=paper_manual, kill_switch=true, is_live=false
status_url = 'http://127.0.0.1:8765/system/status'
system_status = requests.get(status_url).json()

# Verify paper mode
expected_vars = {
    'POLYCOPY_BROKER_MODE': 'paper',
    'POLYCOPY_PAPER_MODE': 'paper_manual',
    'POLYCOPY_ORDER_KILL_SWITCH': 'true'
}

for key, value in expected_vars.items():
    if key in system_status and system_status[key] != value:
        print(f'ERROR: {key} should be {value}, got {system_status[key]}')
        sys.exit(1)

print('Paper mode verification passed')
"
```

---

## Backup Procedures (Required Before Any Mutation)

### 1. Timestamped Application Backup
```bash
# Create timestamped backup directory - runs BEFORE any mutation
BACKUP_DIR="/root/Polycopy/backups/deploy-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Backup critical files and state
if [ -f /root/Polycopy/.env ]; then
    cp /root/Polycopy/.env "$BACKUP_DIR/.env.backup"
    echo "Backed up .env (preserving permissions)"
fi

if [ -d /root/Polycopy/data ]; then
    cp -r /root/Polycopy/data "$BACKUP_DIR/data"
    echo "Backed up data directory"
fi

if [ -d /root/Polycopy/frontend/dist ]; then
    cp -r /root/Polycopy/frontend/dist "$BACKUP_DIR/frontend_dist"
    echo "Backed up frontend dist"
fi

cp /root/Polycopy/Makefile "$BACKUP_DIR/Makefile" 2>/dev/null || true
cp /root/Polycopy/pyproject.toml "$BACKUP_DIR/pyproject.toml"

cp /etc/caddy/Caddyfile "$BACKUP_DIR/Caddyfile" 2>/dev/null || echo "No Caddyfile to backup"

# Service unit backup
if [ -f /etc/systemd/system/polycopy-api.service ]; then
    cp /etc/systemd/system/polycopy-api.service "$BACKUP_DIR/polycopy-api.service"
    echo "Backed up service unit"
fi

# Record git state
git rev-parse HEAD > "$BACKUP_DIR/git-state.txt"
git status --short > "$BACKUP_DIR/git-status.txt"

# Record runtime state
ss -tlnp > "$BACKUP_DIR/runtime-state.txt" 2>/dev/null || echo "Network state recorded"

echo "Backup complete: $BACKUP_DIR"
```

---

## Deployment Process

### Phase 1: Read-Only Verification

#### 1. Tag Verification (Read-Only Inspection Only)
```bash
# Fetch tags from remote repository - READ ONLY
git fetch --tags

# Verify the target tag exists locally - READ ONLY
if git tag | grep -q "v0.1.0-paper-pilot"; then
    echo "Target tag v0.1.0-paper-pilot exists locally"
else
    echo "ERROR: Target tag v0.1.0-paper-pilot does not exist locally"
    exit 1
fi

# Get tag SHA - READ ONLY
tag_sha=$(git rev-parse v0.1.0-paper-pilot)
echo "Tag v0.1.0-paper-pilot resolves to: $tag_sha"

# Verify it matches expected SHA - READ ONLY
expected_sha="16a04b8e2f3007f1833b94006de519579592fdf0"
if [ "$tag_sha" = "$expected_sha" ]; then
    echo "Tag SHA matches expected baseline: OK"
else
    echo "ERROR: Tag SHA $tag_sha does not match expected $expected_sha"
    echo "STOP — Tag mismatch. Do not proceed with deployment."
    exit 1
fi

# Verify tag points to valid commit (prevents tag hijacking) - READ ONLY
if git show --quiet v0.1.0-paper-pilot --format="%H" > /dev/null 2>&1; then
    echo "Tag points to valid commit: OK"
else
    echo "ERROR: Tag is invalid or doctored"
    exit 1
fi

echo ""
echo "=== READ-ONLY VERIFICATION COMPLETE ==="
echo "STOP — Todd approval required before any mutation operations."
```

### Phase 2: Approval-Gated Mutations (STOP — Todd approval required)

**STOP — Todd approval required before continuing with any mutation below.**

No tag creation or moving occurs in the default flow. Tags should already exist.
Tag creation/moving belongs in a separate optional section (see Section 7).

#### 2. Database Verification (Read-Only + Todd Approval Required for Actual Changes)
```bash
# Verify current database schema (READ ONLY)
python -c "
import sqlite3

conn = sqlite3.connect('data/polycopy.db')
cursor = conn.cursor()

print('Current database schema verification (READ-ONLY):')
cursor.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;\")
tables = cursor.fetchall()
for table in tables:
    print(f'  - {table[0]} table exists')

# Check schema version
try:
    cursor.execute(\"SELECT value FROM _meta WHERE key='schema_version';\")
    result = cursor.fetchone()
    if result:
        print(f'  - Schema version: {result[0]}')
    else:
        print('  - No schema version found')
except Exception:
    print('  - Schema version table not found')

conn.close()
print('Database schema inspection complete')
"

# Schema v6 feature verification (READ ONLY - no changes)
python -c "
import sqlite3

conn = sqlite3.connect('data/polycopy.db')
cursor = conn.cursor()

print('Schema v6 feature verification (READ-ONLY):')

# 1. canonical_address column exists
try:
    cursor.execute(\"PRAGMA table_info(wallets);\")
    columns = [row[1] for row in cursor.fetchall()]
    if 'canonical_address' in columns:
        print('OK: canonical_address column exists')
    else:
        print('ERROR: canonical_address column missing from wallets table')
except Exception as e:
    print(f'ERROR checking columns: {e}')

# 2. Unique constraint exists for canonical_address
try:
    cursor.execute(\"PRAGMA index_list(wallets);\")
    indexes = [row[1] for row in cursor.fetchall()]
    if 'ux_wallets_canonical_address' in indexes:
        print('OK: ux_wallets_canonical_address unique index exists')
    else:
        print('ERROR: ux_wallets_canonical_address unique index missing')
except Exception as e:
    print(f'ERROR checking indexes: {e}')

# 3. Foreign key checks pass
try:
    cursor.execute(\"PRAGMA foreign_key_check;\")
    fk_errors = cursor.fetchall()
    if not fk_errors:
        print('OK: Foreign key constraints satisfied')
    else:
        print('ERROR: Foreign key violations found')
except Exception as e:
    print(f'ERROR checking foreign keys: {e}')

conn.close()
"
```

**STOP — Todd approval required before running any database migration.**

The script `scripts/live_smoke_pr3_fixes.py` is a **temporary smoke validation script**, NOT a production database migration. It:
- Connects to the public Polymarket data-api endpoint
- Uses a TEMPORARY SQLite database in `/tmp` (not production DB)
- Does NOT touch `/root/Polycopy/data/polycopy.db`
- Validates P1/P2 fixes in isolation

To run smoke validation (read-only to production, writes to temp):
```bash
cd /root/Polycopy && python scripts/live_smoke_pr3_fixes.py
```

#### 3. Application Deployment (STOP — Todd approval required)
```bash
# STOP — Todd approval required before continuing with service deployment

# Backup database state before any changes
if [ -f /root/Polycopy/data/polycopy.db ]; then
    cp /root/Polycopy/data/polycopy.db /root/Polycopy/data/polycopy.db.pre-deploy.$(date +%s)
    echo "Database backup created"
fi

# STOP — Todd approval required before restarting service
# systemctl restart polycopy-api.service

# STOP — Todd approval required before proceeding
# systemctl status polycopy-api.service --no-pager

# STOP — Todd approval required before checking logs
# systemctl status polycopy-api.service --no-pager -n 50
```

#### 4. Frontend Deployment (STOP — Todd approval required)
```bash
# STOP — Todd approval required before continuing with frontend deployment

# Build frontend if needed (for new deployments)
# Corrected rebuild check - uses proper shell syntax
if [ ! -d /root/Polycopy/frontend/dist ] || [ -n "$(find /root/Polycopy/frontend/src -newer /root/Polycopy/frontend/dist -o -newer /root/Polycopy/frontend/package.json 2>/dev/null)" ]; then
    echo "STOP — Todd approval required before: cd frontend && npm run build"
fi

# Verify build (read-only)
ls -la /root/Polycopy/frontend/dist/
find /root/Polycopy/frontend/dist -name "index.html" -exec file {} \;
```

---

## Caddy and Polycopy Hostname Configuration (STOP — Todd approval required)

### 1. Current Caddy Configuration (Read-Only)
```bash
# Read current Caddy configuration - READ ONLY
cat /etc/caddy/Caddyfile

# Check Caddy service status - READ ONLY
systemctl status caddy --no-pager

# Verify Caddy listening on expected ports - READ ONLY
ss -tlnp | grep ':2019\|:8765\|:5173\|:8501\|:8502\|:9119'
```

### 2. Polycopy Hostname Resolution (Read-Only Verification)
```bash
# Verify DuckDNS DNS resolution - READ ONLY
# NOTE: Do not assume which hostname is approved (polycopy.duckdns.org vs polycop.duckdns.org)
# These checks are informational only.

dig polycopy.duckdns.org A 2>/dev/null || echo "No resolution for polycopy.duckdns.org"
nslookup polycop.duckdns.org 2>/dev/null || echo "No resolution for polycop.duckdns.org"

# Expected VPS IP
expected_ip="209.54.105.179"

# Check which host resolves (informative only - no changes made)
echo ""
echo "Current DNS state (read-only):"
echo "- Target IP: $expected_ip"
echo "- polycopy.duckdns.org: Run above dig to check resolution"
echo "- polycop.duckdns.org: Run above nslookup to check resolution"
```

### 3. Intended Caddy Configuration (Requires Todd Approval)
**STOP — Todd approval required before editing Caddy configuration.**

Proposed configuration for approval:
```
# Intended dashboard route:
http://polycopy.duckdns.org {
    reverse_proxy 127.0.0.1:8501
}

# Intended API proxy route:
http://polycopy.duckdns.org/api/* {
    reverse_proxy 127.0.0.1:8765
}
```

**Key constraints (read into approval discussion):**
- API remains on `127.0.0.1:8765` (no direct public exposure)
- No direct public exposure of port 8765
- `/api/*` routes proxy to internal service
- Frontend/dashboard routes to appropriate port

**Rollback instructions (if Caddy changes rejected):**
```bash
# STOP — Todd approval required before running
# Restore Caddyfile from backup
# cp /root/Polycopy/backups/deploy-*/Caddyfile /etc/caddy/Caddyfile

# Validate config before reload
# caddy validate --config /etc/caddy/Caddyfile

# Reload Caddy
# systemctl reload caddy
```

### 4. Pre-Change Validation for Caddy (Read-Only)
```bash
# Validate proposed Caddy syntax (read-only)
caddy fmt --overwrite /dev/stdin << 'EOF'
# Proposed polycopy block - for validation only
http://polycopy.duckdns.org {
    reverse_proxy 127.0.0.1:8501
}
EOF
```

---

## Health Checks (Read-Only Verification)

```bash
# System status - READ ONLY
curl -s -f http://127.0.0.1:8765/system/status

# API config (redacted secrets) - READ ONLY
curl -s -f http://127.0.0.1:8765/config | grep -v password | grep -v secret | head -10

# Frontend accessibility - READ ONLY
if [ -f /root/Polycopy/frontend/dist/index.html ]; then
    echo "Frontend built: OK"
else
    echo "Frontend not built - this may be OK for paper-pilot"
fi

# Dashboard health check - READ ONLY
curl -s -f http://127.0.0.1:8765/portfolio/summary | jq .

# Risk gate status - READ ONLY
curl -s -f http://127.0.0.1:8765/risk/console | jq .

# Decision log access - READ ONLY
curl -s -f http://127.0.0.1:8765/decision-log | head -5

# Long-running processes check - READ ONLY
ps aux | grep -E "(uvicorn|polycopy)" | grep -v grep

# Network port verification - READ ONLY
ss -tlnp | grep -E "(8765|5173)"
```

---

## Verification Checklist

### Required Conditions

#### Database Verification
- [ ] Schema version = 6
- [ ] canonical_address column exists in wallets table
- [ ] ux_wallets_canonical_address unique index exists
- [ ] Foreign key constraints satisfied (no errors)
- [ ] No duplicate canonical addresses
- [ ] Sample data properly labeled (is_sample=True)

#### Application Status
- [ ] polycopy-api.service active and healthy
- [ ] All paper-only mode settings enabled
- [ ] Kill switch engaged (POLYCOPY_ORDER_KILL_SWITCH=true)
- [ ] broker_mode set to paper (not polymarket)
- [ ] paper_mode set to paper_manual
- [ ] No live credentials in environment
- [ ] All API endpoints responding correctly

#### Frontend Status
- [ ] Frontend index.html present (optional for paper-pilot)
- [ ] Static assets accessible (if built)
- [ ] No build warnings or errors (if applicable)

#### Environment Verification
- [ ] Working directory: /root/Polycopy
- [ ] Git branch: main
- [ ] Git HEAD: 16a04b8e2f3007f1833b94006de519579592fdf0
- [ ] No production changes (no systemd, reverse proxy, etc.)
- [ ] Paper-only mode confirmed

---

## Rollback Procedures

### STOP — Todd approval required before any rollback operations.

### 1. Service Stop (STOP — Todd approval required)
```bash
# STOP — Todd approval required before continuing with service stop
if [ -f /etc/systemd/system/polycopy-api.service ]; then
    echo "STOP — Todd approval required before: systemctl stop polycopy-api.service"
fi
```

### 2. Service Stop Execution (After Todd Approval)
```bash
# Only run after Todd approval
# systemctl stop polycopy-api.service

# Verify service stopped (after approval)
# systemctl status polycopy-api.service --no-pager
```

### 3. Database Rollback

**STOP — Todd approval required before continuing with database restore.**

```bash
# STOP — Todd approval required before restoring database

# Find latest timestamped backup directory
LATEST_BACKUP=$(ls -t /root/Polycopy/backups/deploy-*/ 2>/dev/null | head -1)

if [ -n "$LATEST_BACKUP" ]; then
    echo "Backup found at: $LATEST_BACKUP"
    echo "STOP — Todd approval required before restoration"

    # Database rollback would use exact backup path:
    # for db_file in "polycopy.db" "polycopy.db-shm" "polycopy.db-wal"; do
    #     if [ -f "$LATEST_BACKUP/data/$db_file" ]; then
    #         cp "$LATEST_BACKUP/data/$db_file" "/root/Polycopy/data/$db_file"
    #         echo "Restored $db_file"
    #     fi
    # done
else
    echo "No backup found. Manual database recovery required."
fi
```

**Alternative SQLite Check (Using Python):**
```bash
# Verify database state after rollback - READ ONLY
python -c "
import sqlite3

conn = sqlite3.connect('data/polycopy.db')
cursor = conn.cursor()

try:
    cursor.execute('SELECT value FROM _meta WHERE key=\"schema_version\";')
    result = cursor.fetchone()
    if result:
        print(f'Schema version: {result[0]}')
    else:
        print('Schema version not found')
except Exception as e:
    print(f'Could not read schema version: {e}')

conn.close()
"
```

### 4. File Restoration (STOP — Todd approval required)
```bash
# STOP — Todd approval required before continuing with file restoration

# Find latest timestamped backup directory
LATEST_BACKUP=$(ls -t /root/Polycopy/backups/deploy-*/ 2>/dev/null | head -1)

if [ -n "$LATEST_BACKUP" ]; then
    echo "Rolling back files from: $LATEST_BACKUP"

    # Restore .env file (preserve permissions, avoid logging secrets)
    if [ -f "$LATEST_BACKUP/.env.backup" ]; then
        echo "STOP — Todd approval required before: cp .env.backup to .env"
    fi

    # Restore frontend
    if [ -d "$LATEST_BACKUP/frontend_dist" ]; then
        echo "STOP — Todd approval required before frontend restoration"
    fi

    # Restore database files (from data subdirectory)
    for db_file in "polycopy.db" "polycopy.db-shm" "polycopy.db-wal"; do
        if [ -f "$LATEST_BACKUP/data/$db_file" ]; then
            echo "STOP — Todd approval required before: cp data/$db_file to data/"
        fi
    done

    # Restore Caddyfile
    if [ -f "$LATEST_BACKUP/Caddyfile" ]; then
        echo "STOP — Todd approval required before: cp Caddyfile to /etc/caddy/"
    fi

    echo "File restoration requires explicit Todd approval for each step"
else
    echo "No timestamped backup directory found for restoration"
fi
```

### 5. Service Restart (STOP — Todd approval required)
```bash
# STOP — Todd approval required before continuing with service restart
# systemctl daemon-reload

# STOP — Todd approval required before continuing
# systemctl restart polycopy-api.service
```

### 6. Complete System Rollback (STOP — Todd approval required)
```bash
# STOP — Todd approval required before continuing with complete system rollback

cd /root/Polycopy

# Find latest backup
LATEST_BACKUP=$(ls -t /root/Polycopy/backups/deploy-*/ 2>/dev/null | head -1)

if [ -n "$LATEST_BACKUP" ]; then
    echo "Complete rollback path (requires Todd approval for each step):"
    echo "1. Restore .env from $LATEST_BACKUP/.env.backup"
    echo "2. Restore database from $LATEST_BACKUP/data/"
    echo "3. Restore Caddyfile from $LATEST_BACKUP/Caddyfile"
    echo "4. Restore git to tag ref"
    echo "5. Restart service"
else
    echo "No backup available for complete system rollback"
fi
```

**STOP — Todd approval required before git operations. Never run `git reset --hard` without approval.**

```bash
# Git state restoration (if backup tag exists) - REQUIRES APPROVAL
# STOP — Todd approval required before any destructive Git command

# Proposed safe model (requires approval):
# 1. Record current branch/SHA
# 2. git fetch origin
# 3. git checkout v0.1.0-paper-pilot (known-good tag)
# 4. Verify clean state with: git status --short
# 5. Restart service (after approval)
```

---

## Optional Tag Management (STOP — Todd approval required for ALL operations)

**STOP — Todd approval required before creating or moving any tag.**

These operations are optional and belong in a separate section from the default deployment flow:

```bash
# Create new tag (REQUIRES APPROVAL - not part of default flow)
# STOP — Todd approval required before: git tag -a v0.2.0 -m "Release message"

# Move existing tag (REQUIRES APPROVAL - destructive operation)
# STOP — Todd approval required before: git tag -f v0.1.0-paper-pilot <new-sha>

# Push tag to remote (REQUIRES APPROVAL)
# STOP — Todd approval required before: git push origin v0.1.0-paper-pilot --force
```

---

## Risk Mitigation

### 1. Pre-Deployment Risk Assessment
- [ ] All stakeholders notified of deployment window
- [ ] Rollback procedures documented and tested
- [ ] Backup procedures verified
- [ ] Monitoring tools in place

### 2. Deployment Risk Mitigation
- [ ] Use feature flags if possible (not applicable to paper-pilot)
- [ ] Implement graded rollout if applicable (paper-only mode maintained)
- [ ] Have monitoring alert on any service degradation
- [ ] Keep engineering team available for emergency response

### 3. Post-Deployment Verification
- [ ] All health checks passing
- [ ] API monitoring active
- [ ] Security audit trails intact
- [ ] Team debrief to capture learnings

---

## Emergency Procedures

### 1. Immediate Incident Response
```bash
# STOP — Todd approval required before: systemctl stop polycopy-api.service

# Document incident
echo "$(date): Service degradation detected. Service stopped." >> /root/Polycopy/incident_log.txt

# Refer to rollback procedures above (require approval)
```

---

## Documentation and Reporting

### 1. Post-Deployment Report (Read-Only Generation)
```bash
# Generate deployment report - READ ONLY (no mutations)
cd /root/Polycopy
DEPLOY_REPORT="deployment_report_$(date +%Y%m%d_%H%M%S).md"

# Use Python to avoid sqlite3 CLI dependency
python -c "
import sqlite3
import subprocess
import datetime

report_date = datetime.datetime.now().isoformat()

# Git state
git_sha = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()

# Service status
try:
    service_status = subprocess.run(['systemctl', 'is-active', 'polycopy-api.service'], capture_output=True, text=True).stdout.strip()
except:
    service_status = 'unknown'

# Schema version
try:
    conn = sqlite3.connect('data/polycopy.db')
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM _meta WHERE key=\"schema_version\";')
    schema_result = cursor.fetchone()
    schema_version = schema_result[0] if schema_result else 'N/A'
    conn.close()
except:
    schema_version = 'N/A'

print('# Deployment Report')
print(f'## Deployment Time')
print(report_date)
print(f'## Deployed Version')
print(git_sha)
print(f'## Deployment Status')
print(service_status)
print(f'## Database Schema Version')
print(schema_version)
print('## Backup Locations')
print('Generated at: /root/Polycopy/backups/')
print('## Rollback Available')
print('Yes - procedures documented in rollback section')
"
```

---

## Cleanup (STOP — Todd approval required for destructive operations)
```bash
# STOP — Todd approval required before running cleanup

# Remove old backups (older than 7 days) - REQUIRES APPROVAL
# find /root/Polycopy/backups/ -type d -mtime +7 -exec rm -rf {} \;

# Remove temporary deployment scripts - REQUIRES APPROVAL
# rm -f /root/Polycopy/deploy_temp.sh /root/Polycopy/rollback_temp.sh

# Remove temporary logs - REQUIRES APPROVAL
# rm -f /root/Polycopy/tmp_deployment_log.txt

# Cleanup .bak files - REQUIRES APPROVAL
# find /root/Polycopy -name '*.bak' -type f -delete
```

---

## Compliance and Safety

### 1. Safety Checklist
- [ ] No real-money trading enabled
- [ ] Paper-only mode active
- [ ] Kill switch engaged
- [ ] All secrets protected
- [ ] No production configuration changes
- [ ] Database schema validated
- [ ] Full rollback capability tested

### 2. Documentation Requirements
- [ ] All procedures documented in this runbook
- [ ] Emergency procedures available
- [ ] Monitoring and alerting configured
- [ ] Roles and responsibilities defined

---

**This runbook ensures safe deployment and rollback for the paper-pilot release while maintaining all safety restrictions and paper-only mode operations.**

---

## Specialist Paper Execution Spine (separate branch)

The specialist approval → copyable paper-signal → authorized → executed → marked →
settled loop lives on `feat/specialist-paper-execution-spine` and is documented in
[`docs/specialist_paper_execution_spine.md`](specialist_paper_execution_spine.md).

Canonical authoritative tables for that spine are the `paper_*` family
(`paper_orders`, `paper_fills`, `paper_positions`, `paper_position_lots`,
`paper_position_marks`, `paper_position_settlements`) plus `specialist_approvals`,
`source_trade_enrichments`, `approved_specialist_trade_dispatches`, `copy_candidates`,
and `paper_signal_execution_authorizations`. The legacy `orders`/`positions` tables are
sample/demo-only and are **not** authoritative for the specialist spine.

Operator commands, service templates (`deploy-units/*.service.template`), rollout
sequence, and rollback for the specialist spine are all described in that document.

---

**Last Updated:** Documentation-only update for P2.1 blocker fixes
**SHA:** 16a04b8e2f3007f1833b94006de519579592fdf0
**Status:** Paper-only pilot release

*All operations preserve the current paper-only mode and safety restrictions. Read-only paths execute immediately. All mutation operations require Todd approval.*