#!/usr/bin/env python3
"""Idempotent: ensure dashboard.py has GET /api/health."""
from pathlib import Path
MARKER = "open_teleset_prod_health_v1"
SNIPPET = '''
# --- open_teleset_prod_health_v1 ---
@app.get("/api/health")
async def api_health():
    return {"status": "ok", "service": "open-teleset"}
# --- end open_teleset_prod_health_v1 ---
'''

def main() -> int:
    path = Path("dashboard.py")
    if not path.exists():
        print("dashboard.py not found")
        return 1
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print("already patched")
        return 0
    path.write_text(text.rstrip() + "\n" + SNIPPET, encoding="utf-8")
    print("patched dashboard.py")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
