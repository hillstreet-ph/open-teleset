"""Small remote MCP surface protected by the dashboard's administrator gate.

This server deliberately does not import or mount the stdio Telegram tool catalog.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from open_teleset.security import ADMIN_ROLES, AuthenticatedUser, PUBLIC_ORIGINS


def create_read_only_mcp(
    account_snapshot: Callable[[], list[dict[str, Any]]],
    readiness: Callable[[], Awaitable[dict[str, Any]]],
    *,
    development: bool = False,
) -> FastMCP:
    """Use per-request ASGI identity; never persist MCP sessions or credentials."""
    hosts = ["open-teleset.site", "www.open-teleset.site", "app.open-teleset.site"]
    origins = list(PUBLIC_ORIGINS)
    if development:
        hosts += ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
        origins += ["http://localhost:8080", "http://127.0.0.1:8080"]
    mcp = FastMCP(
        "Open-Teleset Read Only",
        instructions="Read-only administrator status. No messaging or session access.",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
    )
    annotations = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    def user_for_request(ctx: Context) -> AuthenticatedUser:
        request = ctx.request_context.request
        user = getattr(getattr(request, "state", None), "user", None)
        if not isinstance(user, AuthenticatedUser) or user.role not in ADMIN_ROLES:
            raise ToolError("Administrator authentication required")
        return user

    @mcp.tool(annotations=annotations)
    async def authenticated_identity(ctx: Context) -> dict[str, str]:
        """Return the current administrator's verified ID and project role."""
        user = user_for_request(ctx)
        return {"id": user.id, "role": user.role, "project": "open-teleset"}

    @mcp.tool(annotations=annotations)
    async def runtime_readiness(ctx: Context) -> dict[str, Any]:
        """Read the existing database readiness probe without exposing configuration."""
        user_for_request(ctx)
        try:
            payload = await readiness()
            result = {
                "status": "ok" if payload.get("status") == "ok" else "degraded",
                "checks": {
                    "service": payload.get("checks", {}).get("service") is True,
                    "database": payload.get("checks", {}).get("database") is True,
                },
            }
            if payload.get("reason") in {"database_not_configured", "database_unreachable"}:
                result["reason"] = payload["reason"]
            return result
        except Exception:
            raise ToolError("Runtime readiness unavailable") from None

    @mcp.tool(annotations=annotations)
    async def account_status_summary(ctx: Context) -> dict[str, int]:
        """Read aggregate account counts, excluding phone numbers, IDs and session data."""
        user_for_request(ctx)
        try:
            accounts = account_snapshot()
            online = sum(account.get("status") == "online" for account in accounts)
            return {"total": len(accounts), "online": online, "offline": len(accounts) - online}
        except Exception:
            raise ToolError("Account status unavailable") from None

    return mcp
