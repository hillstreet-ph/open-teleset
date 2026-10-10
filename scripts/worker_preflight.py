"""Validate the live backend contract before an explicit Worker-only rollout."""
import json
import time
import urllib.error
import urllib.request

ORIGIN = "https://open-teleset-prod.zeabur.app"


def status(path):
    request = urllib.request.Request(
        f"{ORIGIN}{path}?worker_preflight={time.time_ns()}",
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            # Operational responses must remain unread when anonymous access
            # unexpectedly succeeds. Only public health metadata is inspected.
            body = json.load(response) if path in {"/health", "/readyz"} else None
            return response.status, body
    except urllib.error.HTTPError as error:
        return error.code, None


def verify(probe=status):
    code, body = probe("/health")
    if code != 200 or not isinstance(body, dict) or body.get("service") != "open-teleset" or body.get("status") != "ok":
        raise RuntimeError("Origin liveness contract failed")
    from datetime import datetime, timezone
    try:
        timestamp = datetime.fromisoformat(body["ts"].replace("Z", "+00:00"))
        age = abs((datetime.now(timezone.utc) - timestamp).total_seconds())
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("Origin liveness timestamp is invalid") from None
    if age > 120:
        raise RuntimeError("Origin liveness timestamp is stale")
    code, body = probe("/readyz")
    if code != 200 or not isinstance(body, dict) or body.get("status") != "ok" or body.get("checks", {}).get("database") is not True:
        raise RuntimeError("Origin database readiness contract failed")
    for path in ("/api/accounts", "/api/accounts/test/export-session", "/api/schedules"):
        if probe(path)[0] != 401:
            raise RuntimeError("Origin anonymous authorization contract failed")


if __name__ == "__main__":
    try:
        verify()
    except Exception:
        raise SystemExit("Worker-only deployment blocked: live origin contract not verified") from None
    print("Live origin health, database readiness, and anonymous access denial verified")
