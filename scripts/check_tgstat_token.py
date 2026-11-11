"""Check a TGStat API token without printing it.

Usage::

    TGSTAT_TOKEN=... python scripts/check_tgstat_token.py [keyword]

Asks the same endpoint the app uses and prints only the HTTP status, TGStat's
own status/error string and the number of items. The token and the request URL
are never printed.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SEARCH_URL = "https://api.tgstat.ru/channels/search"


def check(token: str, keyword: str) -> tuple[int, dict]:
    query = urllib.parse.urlencode({
        "token": token,
        "q": keyword,
        "search_by_description": "1",
        "peer_type": "all",
        "limit": "5",
    })
    request = urllib.request.Request(
        f"{SEARCH_URL}?{query}", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        return 1, {"http_status": error.code, "status": "http_error"}
    except (urllib.error.URLError, ValueError):
        return 1, {"http_status": 0, "status": "unreachable"}
    if not isinstance(payload, dict):
        return 1, {"http_status": status, "status": "bad_response"}
    response_body = payload.get("response")
    items = response_body.get("items") if isinstance(response_body, dict) else None
    report = {
        "http_status": status,
        "status": payload.get("status"),
        "error": payload.get("error"),
        "items": len(items) if isinstance(items, list) else None,
    }
    return (0 if payload.get("status") == "ok" else 1), report


def main() -> int:
    token = os.getenv("TGSTAT_TOKEN", "").strip()
    if not token:
        sys.exit("TGSTAT_TOKEN is empty")
    keyword = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    keyword = keyword or "news"
    code, report = check(token, keyword)
    print(json.dumps(report))
    if report.get("error") == "no_active_subscription":
        print("tgstat: the token is valid but its account has no active API subscription")
    elif report.get("status") != "ok":
        print("tgstat: the token did not answer with status ok")
    return code


if __name__ == "__main__":
    sys.exit(main())
