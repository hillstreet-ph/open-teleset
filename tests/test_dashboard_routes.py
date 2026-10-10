"""Exercise direct-host login routes without live accounts or credentials."""

import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_direct_host_login_shell_config_and_auth_boundary(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "static", tmp_path / "static")
    script = '''
import os
from fastapi.testclient import TestClient
import dashboard

with TestClient(dashboard.app, base_url="https://open-teleset.site") as http:
    response = http.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard"
    for method in ["GET", "HEAD"]:
        response = http.request(method, "/dashboard")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
    assert 'src="/config.js"' in http.get("/dashboard").text
    for invalid in ["", "sb_secret_synthetic", "eyJ.synthetic.service-role", 'sb_publishable_\\";alert(1)//']:
        os.environ["SUPABASE_PUBLISHABLE_KEY"] = invalid
        response = http.get("/config.js")
        assert response.status_code == 503
        assert invalid not in response.text if invalid else True
    os.environ["SUPABASE_PUBLISHABLE_KEY"] = "sb_publishable_synthetic-public-key"
    os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "synthetic-private-never-publish"
    os.environ["DASHBOARD_PASSWORD"] = "synthetic-legacy-never-publish"
    os.environ["DASHBOARD_USER"] = "admin"
    os.environ["DASHBOARD_LOGIN_EMAIL"] = "synthetic@example.com"
    response = http.get("/config.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["cache-control"] == "no-store"
    assert "sb_publishable_synthetic-public-key" in response.text
    assert "__SUPABASE_PUBLISHABLE_KEY__" not in response.text
    assert "synthetic-private-never-publish" not in response.text
    assert "synthetic-legacy-never-publish" not in response.text
    assert 'apiBase: window.location.origin' in response.text
    assert '"admin": "synthetic@example.com"' in response.text
    assert http.head("/config.js").status_code == 200
    for path in ["/api/accounts", "/api/auth/me", "/mcp", "/dashboard-extra", "/config.js/extra"]:
        assert http.get(path).status_code == 401
    for path in ["/", "/dashboard", "/config.js"]:
        assert http.post(path).status_code == 401
    response = http.options("/api/auth/me", headers={
        "Origin": "https://app.open-teleset.site",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.open-teleset.site"
    response = http.options("/api/auth/me", headers={
        "Origin": "https://untrusted.example",
        "Access-Control-Request-Method": "GET",
    })
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
    assert http.get("/readyz").status_code == 503
http = TestClient(dashboard.app, base_url="https://app.open-teleset.site")
for method in ["GET", "HEAD"]:
    for path in ["/", "/dashboard"]:
        response = http.request(method, path + "?code=synthetic-code", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "https://open-teleset.site/dashboard?code=synthetic-code"
        assert response.headers["cache-control"] == "no-store"
# Redirects apply only to the public shell; private endpoints retain their gate.
assert http.get("/api/auth/me").status_code == 401
assert http.post("/dashboard").status_code == 401
http = TestClient(dashboard.app, base_url="http://localhost:8080")
assert http.get("/", follow_redirects=False).headers["location"] == "/dashboard"
assert http.get("/dashboard").status_code == 200
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith((
        "SUPABASE_", "DATABASE_", "SENTRY_", "TELEGRAM_",
    ))}
    env.update(APP_ENV="production", PYTHONPATH=f"{root}:{root / 'src'}")
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
