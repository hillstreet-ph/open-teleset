"""Probe the actual runtime and anonymous authorization boundary without secrets."""
import json
import time
import urllib.error
import urllib.request


def status(path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None


for attempt in range(30):
    try:
        code, body = status("/health")
        if code == 200 and body.get("service") == "open-teleset":
            break
    except (OSError, ValueError):
        pass
    time.sleep(1)
else:
    raise SystemExit("Runtime failed its liveness probe")

for path in ("/api/accounts", "/api/accounts/test/export-session", "/api/schedules"):
    assert status(path)[0] == 401, f"Anonymous access not denied: {path}"

assert status("/readyz")[0] == 503, "Unconfigured dependencies must not report readiness"
print("Runtime startup, anonymous access denial, and degraded readiness verified")
