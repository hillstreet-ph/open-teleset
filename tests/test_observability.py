"""Verify monitoring cannot forward private runtime data."""

import json

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from open_teleset.observability import initialize_error_monitoring, private_error_event


def test_monitoring_is_disabled_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: pytest.fail("unexpected init"))
    assert initialize_error_monitoring() is False


def test_private_event_excludes_nested_payloads():
    event = {
        "event_id": "synthetic",
        "level": "error",
        "request": {"headers": {"Authorization": "private-token"}, "data": "telegram-content"},
        "user": {"email": "private-email"},
        "extra": {"session": "private-session"},
        "breadcrumbs": [{"message": "private-log"}],
        "message": "private-message",
        "tags": {"account_id": "private-account"},
        "exception": {"values": [{
            "type": "RuntimeError", "value": "private-password",
            "stacktrace": {"frames": [{
                "filename": "dashboard.py", "function": "handler", "lineno": 3,
                "vars": {"key": "private-key"}, "pre_context": ["private-source"],
            }]},
        }]},
    }
    safe = private_error_event(event, {})
    assert "private-" not in json.dumps(safe)
    assert "telegram-content" not in json.dumps(safe)
    assert safe["exception"]["values"][0]["type"] == "RuntimeError"
    assert safe["exception"]["values"][0]["stacktrace"]["frames"][0]["lineno"] == 3
    assert safe["tags"] == {"service": "open-teleset"}


def test_fastapi_error_reaches_transport_without_private_data(monkeypatch):
    envelopes = []

    class RecordingTransport(Transport):
        def capture_envelope(self, envelope):
            envelopes.append(envelope)

    actual_init = sentry_sdk.init

    def recording_init(**options):
        options["transport"] = RecordingTransport
        return actual_init(**options)

    monkeypatch.setenv("SENTRY_DSN", "https://example@example.com/1")
    monkeypatch.setattr(sentry_sdk, "init", recording_init)
    try:
        assert initialize_error_monitoring() is True
        app = FastAPI()

        @app.get("/test-error")
        def error():
            secret_in_local_variable = "private-session-key"
            raise RuntimeError(secret_in_local_variable)

        response = TestClient(app, raise_server_exceptions=False).get(
            "/test-error?token=private-query",
            headers={"Authorization": "Bearer private-token", "Cookie": "session=private-cookie"},
        )
        assert response.status_code == 500
        sentry_sdk.flush(timeout=2)
        events = [item.payload.json for envelope in envelopes for item in envelope.items if item.type == "event"]
        assert events
        assert "private-" not in json.dumps(events)
        assert events[0]["exception"]["values"][-1]["type"] == "RuntimeError"
        assert "request" not in events[0]
        assert "breadcrumbs" not in events[0]
    finally:
        sentry_sdk.get_client().close()
        actual_init(dsn="", default_integrations=False, auto_enabling_integrations=False)
