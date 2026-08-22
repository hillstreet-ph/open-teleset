#!/usr/bin/env bash
# Autonomous bootstrap — secrets must come from environment or .env (never commit .env)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements-prod.txt

if [[ ! -f .env ]]; then cp .env.example .env; fi

python - <<'PY'
import os, secrets
from pathlib import Path
from dotenv import dotenv_values
from cryptography.fernet import Fernet
p = Path(".env")
cur = dict(dotenv_values(p))
for k in ["SUPABASE_URL","SUPABASE_SERVICE_ROLE_KEY","DATABASE_URL","TELEGRAM_API_ID","TELEGRAM_API_HASH"]:
    if os.environ.get(k): cur[k] = os.environ[k]
if not cur.get("SESSION_ENCRYPTION_KEY"):
    cur["SESSION_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
if not cur.get("APP_SECRET_KEY") or "change-me" in (cur.get("APP_SECRET_KEY") or ""):
    cur["APP_SECRET_KEY"] = secrets.token_urlsafe(48)
if not cur.get("DASHBOARD_PASSWORD") or "change-me" in (cur.get("DASHBOARD_PASSWORD") or ""):
    cur["DASHBOARD_PASSWORD"] = secrets.token_urlsafe(16)
    print("DASHBOARD_PASSWORD:", cur["DASHBOARD_PASSWORD"])
p.write_text("\n".join(f"{k}={v}" for k,v in cur.items() if v is not None) + "\n")
print("Updated .env")
PY

set -a; source .env; set +a
PYTHONPATH=src python scripts/apply_migrations.py || true
echo "Bootstrap finished — ensure secrets are set for full migrate"
