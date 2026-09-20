"""Read-only provider inventory using scoped CI secrets; emit metadata only."""
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

REF = "hoseohvgoiarxluxqwqv"
PROJECT = "6a9570faa5e5232732f41cda"


def request(url, token, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, {}
    except (OSError, ValueError):
        return 0, {}


result = {"read_only": True, "canonical_supabase_ref": REF,
          "docker_namespace_matches": os.getenv("DOCKERHUB_USERNAME", "") == "hillstreet",
          "supabase_url_matches": os.getenv("SUPABASE_URL", "").rstrip("/") == f"https://{REF}.supabase.co",
          "secret_presence": {name: bool(os.getenv(name)) for name in
                              ("SUPABASE_PUBLISHABLE_KEY", "SESSION_ENCRYPTION_KEY", "ZEABUR_TOKEN")}}
status, jwks = request(f"https://{REF}.supabase.co/auth/v1/.well-known/jwks.json", "")
result["supabase_jwks"] = {"http_status": status, "supported_signing_keys": sum(
    1 for key in jwks.get("keys", []) if key.get("alg") in {"RS256", "ES256"})}
token = os.getenv("SUPABASE_ACCESS_TOKEN", "")
if token:
    status, config = request(f"https://api.supabase.com/v1/projects/{REF}/config/auth", token)
    result["supabase_auth"] = {"http_status": status, **{k: config[k] for k in
        ("external_github_enabled", "external_google_enabled", "disable_signup") if k in config}}
    if config.get("site_url"):
        site = urlsplit(config["site_url"])
        result["supabase_auth"]["site_hostname"] = site.hostname
token = os.getenv("ZEABUR_TOKEN", "")
if token:
    query = 'query { project(_id: "' + PROJECT + '") { _id name services { _id name } } }'
    status, data = request("https://api.zeabur.com/graphql", token, {"query": query})
    project = (data.get("data") or {}).get("project") or {}
    result["zeabur"] = {"http_status": status, "query_valid": not bool(data.get("errors")),
                         "project_id": project.get("_id"), "project_name": project.get("name"),
                         "services": [{"id": s.get("_id"), "name": s.get("name")}
                                      for s in project.get("services", [])]}
print(json.dumps(result, indent=2))
