"""Fail-closed authentication for Open-Teleset's shared operational dashboard."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
from jwt import PyJWKClient
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

PUBLIC_PATHS = frozenset({"/health", "/healthz", "/readyz", "/"})
PROJECT_KEY = "open-teleset"
ADMIN_ROLES = frozenset({"owner", "admin"})
WS_PROTOCOL = "open-teleset.v1"
WS_TOKEN_PREFIX = "bearer."
PUBLIC_ORIGINS = frozenset({
    "https://open-teleset.site", "https://www.open-teleset.site",
    "https://open-teleset-dashboard.pages.dev",
})


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str | None
    role: str


@lru_cache(maxsize=1)
def _settings() -> dict[str, str]:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or not parsed.netloc
        or parsed.username or parsed.password or parsed.path
        or parsed.query or parsed.fragment
    ):
        raise RuntimeError("Valid HTTPS SUPABASE_URL required")
    issuer = os.getenv("SUPABASE_JWT_ISSUER", f"{url}/auth/v1")
    audience = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if issuer != f"{url}/auth/v1" or audience != "authenticated" or not service_key:
        raise RuntimeError("Invalid Supabase authentication configuration")
    return {
        "SUPABASE_URL": url,
        "SUPABASE_SERVICE_ROLE_KEY": service_key,
        "SUPABASE_JWT_ISSUER": issuer,
        "SUPABASE_JWT_AUDIENCE": audience,
    }


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(
        f"{_settings()['SUPABASE_URL']}/auth/v1/.well-known/jwks.json",
        cache_keys=False,
        lifespan=300,
        timeout=5,
    )


def _decode_access_token(token: str) -> dict[str, Any]:
    settings = _settings()
    signing_key = _jwks_client().get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256", "ES256"],
        audience=settings["SUPABASE_JWT_AUDIENCE"],
        issuer=settings["SUPABASE_JWT_ISSUER"],
        options={"require": ["exp", "iat", "sub", "role"]},
    )
    if claims["role"] != "authenticated":
        raise jwt.InvalidTokenError("User access token required")
    UUID(claims["sub"])
    return claims


async def _load_profile(user_id: str) -> dict[str, Any]:
    """Use existing server-owned shared roles; never trust user metadata.

    Until account ownership is verified, both approved global administration and
    an explicit Open-Teleset administrator assignment are required. Missing
    schema exposure or assignments denies access without a fallback.
    """
    settings = _settings()
    headers = {
        "apikey": settings["SUPABASE_SERVICE_ROLE_KEY"],
        "Authorization": f"Bearer {settings['SUPABASE_SERVICE_ROLE_KEY']}",
        "Accept-Profile": "operations_shared",
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        account_response = await client.get(
            f"{settings['SUPABASE_URL']}/rest/v1/account_roles",
            headers=headers,
            params={"user_id": f"eq.{user_id}", "select": "user_id,role,approved", "limit": "1"},
        )
        account_response.raise_for_status()
        accounts = account_response.json()
        if not isinstance(accounts, list) or len(accounts) != 1:
            raise PermissionError("Approved administrator required")
        account = accounts[0]
        if (
            not isinstance(account, dict) or account.get("user_id") != user_id
            or account.get("approved") is not True or account.get("role") not in ADMIN_ROLES
        ):
            raise PermissionError("Approved administrator required")
        access_response = await client.get(
            f"{settings['SUPABASE_URL']}/rest/v1/project_access",
            headers=headers,
            params={
                "user_id": f"eq.{user_id}", "project_key": f"eq.{PROJECT_KEY}",
                "select": "user_id,project_key,role", "limit": "1",
            },
        )
        access_response.raise_for_status()
        assignments = access_response.json()
    if not isinstance(assignments, list) or len(assignments) != 1:
        raise PermissionError("Project administrator required")
    assignment = assignments[0]
    if (
        not isinstance(assignment, dict) or assignment.get("user_id") != user_id
        or assignment.get("project_key") != PROJECT_KEY
        or assignment.get("role") not in ADMIN_ROLES
    ):
        raise PermissionError("Project administrator required")
    return {"id": user_id, "role": assignment["role"]}


def _extract_bearer(scope: Scope) -> str:
    header = Headers(scope=scope).get("authorization", "")
    protocols = scope.get("subprotocols", []) if scope["type"] == "websocket" else []
    if protocols:
        tokens = [p[len(WS_TOKEN_PREFIX):] for p in protocols if p.startswith(WS_TOKEN_PREFIX)]
        if header or len(protocols) != 2 or protocols.count(WS_PROTOCOL) != 1 or len(tokens) != 1:
            raise jwt.InvalidTokenError("Invalid WebSocket authentication protocols")
        token = tokens[0]
    else:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer":
            raise jwt.InvalidTokenError("Bearer token required")
    if not token or len(token) > 16384 or any(char.isspace() for char in token):
        raise jwt.InvalidTokenError("Bearer token required")
    return token


async def _authenticate(scope: Scope) -> AuthenticatedUser:
    claims = await run_in_threadpool(_decode_access_token, _extract_bearer(scope))
    profile = await _load_profile(claims["sub"])
    return AuthenticatedUser(id=claims["sub"], email=claims.get("email"), role=profile["role"])


class SupabaseAuthMiddleware:
    """Protect HTTP and WS traffic, revalidating before every outgoing WS update.

    Browser clients offer a fixed subprotocol and bearer token as separate
    protocols. Only the fixed protocol is echoed. Tokens in URLs are unsupported.
    """

    def __init__(self, app: ASGIApp, allowed_origins=PUBLIC_ORIGINS):
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            path = scope["path"]
            if scope["method"] in {"GET", "HEAD"} and (
                path in PUBLIC_PATHS or path.startswith("/static/")
            ):
                await self.app(scope, receive, send)
                return
        try:
            if scope["type"] == "websocket":
                origin = Headers(scope=scope).get("origin")
                if (origin is not None and origin not in self.allowed_origins) or (
                    scope.get("subprotocols") and origin not in self.allowed_origins
                ):
                    raise PermissionError("WebSocket origin denied")
            user = await _authenticate(scope)
        except Exception as exc:
            # Provider responses and token details never belong in public errors.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                status = 403 if isinstance(exc, PermissionError) else 401
                response = JSONResponse(
                    {"detail": "Administrator authentication required"}, status_code=status
                )
                await response(scope, receive, send)
            return
        scope.setdefault("state", {})["user"] = user
        if scope["type"] == "http":
            await self.app(scope, receive, send)
            return

        closed = False

        async def authenticated_send(message: Message) -> None:
            nonlocal closed
            if closed:
                raise WebSocketDisconnect(code=1008)
            if message["type"] in {"websocket.accept", "websocket.send"}:
                try:
                    await _authenticate(scope)
                except Exception:
                    closed = True
                    await send({"type": "websocket.close", "code": 1008})
                    raise WebSocketDisconnect(code=1008)
            if message["type"] == "websocket.accept" and scope.get("subprotocols"):
                message = {**message, "subprotocol": WS_PROTOCOL}
            await send(message)

        try:
            await self.app(scope, receive, authenticated_send)
        except WebSocketDisconnect:
            if not closed:
                raise
