"""Server-owned runtime settings in operations_shared.app_settings.

Lets an operator configure a runtime value (e.g. the TGStat token for
account-free channel discovery) without a redeploy. Read with the service role
only; values are never exposed to the browser. An environment variable of the
same purpose always takes precedence, so container secrets keep working.
"""

from __future__ import annotations

import os
import time

import httpx

from open_teleset.security import _settings

_SCHEMA = "operations_shared"
_ENDPOINT = "/rest/v1/app_settings"
_TIMEOUT = 5.0
_CACHE_TTL = 300.0
_cache: dict[str, tuple[float, str]] = {}


def _service_headers() -> dict[str, str]:
    settings = _settings()
    return {
        "apikey": settings["SUPABASE_SERVICE_ROLE_KEY"],
        "Authorization": f"Bearer {settings['SUPABASE_SERVICE_ROLE_KEY']}",
        "Accept-Profile": _SCHEMA,
    }


async def get_setting(key: str, env_var: str | None = None) -> str:
    """Return a runtime setting value.

    Resolution order: the named environment variable (if set and non-empty),
    then the stored value in operations_shared.app_settings, then "". Results
    are cached briefly so a search does not hit the database every time. Any
    lookup failure degrades to the last known value, or "".
    """
    if env_var:
        env_value = os.getenv(env_var, "").strip()
        if env_value:
            return env_value

    cached = _cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    value = ""
    try:
        settings = _settings()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(
                f"{settings['SUPABASE_URL']}{_ENDPOINT}",
                headers=_service_headers(),
                params={"key": f"eq.{key}", "select": "value", "limit": "1"},
            )
            response.raise_for_status()
            rows = response.json()
            if rows:
                value = str(rows[0].get("value") or "").strip()
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        # No service config, DB unreachable, or malformed response: keep the
        # last known value if we have one, otherwise treat it as unset.
        return _cache.get(key, (0.0, ""))[1]

    _cache[key] = (now, value)
    return value
