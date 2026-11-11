import importlib.util
import json
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status, payload=b"{}"):
        self.status = status
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_store_upserts_the_row_with_the_service_role_key(monkeypatch):
    module = load("store_runtime_setting")
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.data)
        assert timeout == 15
        return FakeResponse(201)

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    status = module.store(
        "tgstat_token", "secret-value", "https://example.supabase.co/", "service-key")

    assert status == 201
    assert seen["url"] == "https://example.supabase.co/rest/v1/app_settings"
    assert seen["method"] == "POST"
    assert seen["body"][0]["key"] == "tgstat_token"
    assert seen["body"][0]["value"] == "secret-value"
    assert seen["headers"]["Authorization"] == "Bearer service-key"
    assert seen["headers"]["Apikey"] == "service-key"
    assert seen["headers"]["Content-profile"] == "operations_shared"
    assert "merge-duplicates" in seen["headers"]["Prefer"]


def test_main_fails_loudly_without_a_value(monkeypatch):
    module = load("store_runtime_setting")
    monkeypatch.setattr(module.sys, "argv", ["store_runtime_setting.py", "tgstat_token"])
    monkeypatch.delenv("SETTING_VALUE", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        module.main()
    assert "SETTING_VALUE is empty" in str(exit_info.value)


def test_main_reports_success_without_printing_the_value(monkeypatch, capsys):
    module = load("store_runtime_setting")
    monkeypatch.setattr(module.sys, "argv", ["store_runtime_setting.py", "tgstat_token"])
    monkeypatch.setenv("SETTING_VALUE", "secret-value")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setattr(
        module.urllib.request, "urlopen", lambda request, timeout: FakeResponse(201))

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "tgstat_token: stored (HTTP 201)" in output
    assert "secret-value" not in output


def test_check_reports_counts_without_the_token(monkeypatch, capsys):
    module = load("check_tgstat_token")
    payload = {"status": "ok", "response": {"items": [{"username": "a"}, {"username": "b"}]}}
    monkeypatch.setattr(
        module.urllib.request, "urlopen",
        lambda request, timeout: FakeResponse(200, json.dumps(payload).encode()))
    monkeypatch.setenv("TGSTAT_TOKEN", "secret-value")
    monkeypatch.setattr(module.sys, "argv", ["check_tgstat_token.py", ""])

    assert module.main() == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "http_status": 200, "status": "ok", "error": None, "items": 2}
    assert "secret-value" not in output


def test_check_surfaces_tgstats_own_error_and_fails(monkeypatch, capsys):
    module = load("check_tgstat_token")
    payload = {"status": "error", "error": "no_active_subscription"}
    monkeypatch.setattr(
        module.urllib.request, "urlopen",
        lambda request, timeout: FakeResponse(200, json.dumps(payload).encode()))
    monkeypatch.setenv("TGSTAT_TOKEN", "secret-value")
    monkeypatch.setattr(module.sys, "argv", ["check_tgstat_token.py", "news"])

    assert module.main() == 1
    output = capsys.readouterr().out
    report = json.loads(output.splitlines()[0])
    assert report["status"] == "error"
    assert report["error"] == "no_active_subscription"
    assert "no active API subscription" in output
