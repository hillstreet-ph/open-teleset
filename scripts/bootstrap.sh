#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
source .venv/bin/activate
pip install -q -r requirements-prod.txt
if [[ ! -f .env ]]; then cp .env.example .env; echo "Created .env — fill secrets"; fi
echo "Run: ./scripts/autonomous_setup.sh"
