"""Write one server-owned runtime setting without printing its value.

Usage::

    SETTING_VALUE=<value> python scripts/store_runtime_setting.py tgstat_token

The value comes from ``SETTING_VALUE`` so it never appears in a command line.
Reads ``SUPABASE_URL`` and ``SUPABASE_SERVICE_ROLE_KEY`` and upserts into
``operations_shared.app_settings`` — the same row
``src/open_teleset/app_settings.py`` reads at runtime. An environment variable
of the same purpose still takes precedence inside the app.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


def store(key: str, value: str, base_url: str, service_role_key: str) -> int:
    body = json.dumps([{
        "key": key,
        "value": value,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }]).encode()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/rest/v1/app_settings",
        data=body,
        method="POST",
        headers={
            "apikey": service_role_key,
            "Authorization": "Bearer " + service_role_key,
            "Content-Type": "application/json",
            "Content-Profile": "operations_shared",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("usage: store_runtime_setting.py <key>")
    key = sys.argv[1]
    value = os.getenv("SETTING_VALUE", "").strip()
    if not value:
        sys.exit(f"{key}: SETTING_VALUE is empty")
    base_url = os.getenv("SUPABASE_URL", "").strip()
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not base_url or not service_role_key:
        sys.exit(f"{key}: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
    try:
        status = store(key, value, base_url, service_role_key)
    except urllib.error.HTTPError as error:
        sys.exit(f"{key}: store failed (HTTP {error.code})")
    print(f"{key}: stored (HTTP {status})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
