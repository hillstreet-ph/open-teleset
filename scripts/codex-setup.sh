#!/usr/bin/env bash
# Development-only bootstrap. Does not invoke autonomous_setup.sh or migrations.
set -Eeuo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
MODE="${1:-install}"
case "$MODE" in install|check) ;; *) echo 'Usage: bash scripts/codex-setup.sh [install|check]' >&2; exit 2;; esac
if [[ "$MODE" == install ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements-prod.txt pytest pytest-asyncio ruff
  echo 'Dependencies installed from existing ranges; this repository has no complete dependency lock.'
else
  [[ -x .venv/bin/python ]] || { echo 'Run install mode first' >&2; exit 1; }
  # A disposable test key only; never overwrite the persistent production key.
  SESSION_ENCRYPTION_KEY="$(.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
    PYTHONPATH=src .venv/bin/python -m pytest tests/ -q --tb=short
  .venv/bin/ruff check src scripts
fi
