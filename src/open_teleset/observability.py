"""Opt-in server error monitoring without operational or account payloads."""

import os

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration


def private_error_event(event, hint):
    """Allow only error classification and source locations to leave the app."""
    safe = {
        key: event[key]
        for key in ("event_id", "timestamp", "level", "platform", "release", "environment")
        if key in event
    }
    safe["tags"] = {"service": "open-teleset"}
    exceptions = []
    for error in event.get("exception", {}).get("values", []):
        item = {key: error[key] for key in ("type", "module") if key in error}
        item["value"] = "[redacted]"
        frames = [
            {
                key: frame[key]
                for key in ("filename", "function", "module", "lineno", "in_app")
                if key in frame
            }
            for frame in error.get("stacktrace", {}).get("frames", [])
        ]
        if frames:
            item["stacktrace"] = {"frames": frames}
        exceptions.append(item)
    if exceptions:
        safe["exception"] = {"values": exceptions}
    return safe


def initialize_error_monitoring():
    """Disabled without a DSN; enable only explicit FastAPI error integrations."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("APP_ENV", "production"),
        release=os.getenv("SENTRY_RELEASE") or None,
        integrations=[FastApiIntegration(), StarletteIntegration()],
        default_integrations=False,
        auto_enabling_integrations=False,
        send_default_pii=False,
        include_local_variables=False,
        include_source_context=False,
        max_request_body_size="never",
        max_breadcrumbs=0,
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        before_send=private_error_event,
    )
    return True
