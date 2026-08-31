"""Production authentication and authorization middleware for Open-Teleset."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
import jwt
from fastapi import Request
# PyJWKClient is part of PyJWT[crypto] — imported via jwt.PyJWKClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from starlette.responses import JSONResponse, Response

PUBLIC_PATHS = frozenset({"/healthz", "/readyz"})
PRIVILEGED_PATHS = (
    "/api/accounts/batch-import",
    "/api/batch/delete-accounts",
    "/api/logs/clear",
)


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str | None
    role: str


@lru_cache(maxsize=1)
def _settings() -> dict[str, str]:
    required = {
        "SUPABASE_URL": os.getenv("SUPABASE_URL", "").rstrip("/"),
        "SUPABASE_SERVICE_ROLE_KEY": os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        "SUPABASE_JWT_ISSUER": os.getenv("SUPABASE_JWT_ISSUER", ""),
        "SUPABASE_JWT_AUDIENCE": os.getenv(
            "SUPABASE_JWT_AUDIENCE", "authenticated"
        ),
    }
    missing = sorted(name for name, value in required.items() if not value)
    if missing:
        raise RuntimeError(
            "Missing production authentication settings: " + ", ".join(missing)
        )
    return required


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    settings = _settings()
    return PyJWKClient(
        f"{settings['SUPABASE_URL']}/auth/v1/.well-known/jwks.json",
        cache_keys=True,
        lifespan=300,
    )


def _decode_access_token(token: str) -> dict[str, Any]:
    settings = _settings()
    signing_key = _jwks_client().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256", "ES256"],
        audience=settings["SUPABASE_JWT_AUDIENCE"],
        issuer=settings["SUPABASE_JWT_ISSUER"],
        options={"require": ["exp", "iat", "sub"]},
    )


async def _load_profile(user_id: str) -> dict[str, Any]:
    settings = _settings()
    headers = {
        "apikey": settings["SUPABASE_SERVICE_ROLE_KEY"],
        "Authorization": f"Bearer {settings['SUPABASE_SERVICE_ROLE_KEY']}",
    }
    params = {
        "id": f"eq.{user_id}",
        "select": "id,role,disabled_at",
        "limit": "1",
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(
            f"{settings['SUPABASE_URL']}/rest/v1/profiles",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
    profiles = response.json()
    if not profiles:
        raise PermissionError("Profile not provisioned")
    profile = profiles[0]
    if profile.get("disabled_at"):
        raise PermissionError("Account disabled")
    return profile


def _extract_bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise PermissionError("Bearer token required")
    return token


def _required_roles(request: Request) -> set[str]:
    if request.url.path.startswith(PRIVILEGED_PATHS):
        return {"owner", "admin"}
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        return {"owner", "admin", "operator"}
    return {"owner", "admin", "operator", "viewer"}


class SupabaseAuthMiddleware(BaseHTTPMiddleware):
    """Fail-closed Supabase JWT validation and database-backed RBAC."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        try:
            claims = _decode_access_token(_extract_bearer(request))
            profile = await _load_profile(str(claims["sub"]))
            user = AuthenticatedUser(
                id=str(claims["sub"]),
                email=claims.get("email"),
                role=str(profile["role"]),
            )
            if user.role not in _required_roles(request):
                return JSONResponse({"detail": "Insufficient role"}, status_code=403)
            request.state.user = user
            return await call_next(request)
        except PermissionError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
        except (jwt.PyJWTError, httpx.HTTPError, RuntimeError, KeyError):
            return JSONResponse({"detail": "Authentication failed"}, status_code=401)
