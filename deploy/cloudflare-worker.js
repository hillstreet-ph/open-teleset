const SITE = "https://open-teleset.site";
const HEALTH_TIMEOUT_MS = 10_000;
const HEALTH_MAX_AGE_MS = 2 * 60_000;

const ALLOWED_ORIGINS = new Set([SITE, "https://www.open-teleset.site", "https://open-teleset-dashboard.pages.dev"]);

const CORS_HEADERS = {
  "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,Authorization,apikey,x-client-info",
  "Access-Control-Max-Age": "86400",
};

function cors(resp, request) {
  if (resp.status === 101) return resp; // Preserve the WebSocket upgrade/socket.
  const r = new Response(resp.body, resp);
  Object.entries(CORS_HEADERS).forEach(([k, v]) => r.headers.set(k, v));
  r.headers.delete("Access-Control-Allow-Origin");
  const origin = request?.headers.get("Origin");
  if (origin && ALLOWED_ORIGINS.has(origin)) r.headers.set("Access-Control-Allow-Origin", origin);
  r.headers.append("Vary", "Origin");
  return r;
}

function healthResponse(body, status = 200, request) {
  return cors(Response.json(body, {
    status,
    headers: { "Cache-Control": "no-store" },
  }), request);
}

async function fetchOriginHealth(origin, request) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS);
  try {
    const target = new URL("/health", origin);
    target.searchParams.set("probe", Date.now().toString());
    const response = await fetch(target, {
      headers: { Accept: "application/json", "Cache-Control": "no-cache" },
      cache: "no-store",
      cf: { cacheTtl: 0, cacheEverything: false },
      signal: controller.signal,
    });

    if (!response.ok) {
      return healthResponse({ status: "degraded", reason: "upstream_http_error" }, 503, request);
    }

    let payload;
    try {
      payload = await response.json();
    } catch {
      return healthResponse({ status: "degraded", reason: "invalid_upstream_health" }, 503, request);
    }

    const timestamp = Date.parse(payload.ts);
    const stale = !Number.isFinite(timestamp) || Math.abs(Date.now() - timestamp) > HEALTH_MAX_AGE_MS;
    if (payload.status !== "ok" || stale) {
      return healthResponse({
        status: "degraded",
        reason: stale ? "stale_upstream_health" : "upstream_unhealthy",
      }, 503, request);
    }

    return healthResponse(payload, 200, request);
  } catch {
    return healthResponse({ status: "degraded", reason: "upstream_unavailable" }, 503, request);
  } finally {
    clearTimeout(timeout);
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Handle preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors(new Response(null), request).headers });
    }

    // Health must fail closed when the origin is missing, stale, or unavailable.
    if (url.pathname === "/api/health" || url.pathname === "/health") {
      if (!env.ORIGIN) {
        return healthResponse({ status: "degraded", reason: "origin_unconfigured" }, 503, request);
      }
      return fetchOriginHealth(env.ORIGIN, request);
    }

    // Proxy all other requests to ORIGIN backend
    if (env.ORIGIN) {
      const target = new URL(url.pathname + url.search, env.ORIGIN);
      const init = {
        method: request.method,
        headers: request.headers,
        redirect: "manual",
      };
      if (request.method !== "GET" && request.method !== "HEAD") {
        init.body = request.body;
      }
      try {
        const resp = await fetch(target.toString(), init);
        return cors(resp, request);
      } catch {
        return cors(Response.json({ error: "upstream unavailable" }, { status: 502 }), request);
      }
    }

    // No ORIGIN set — return status page
    return cors(Response.json({
      service: "open-teleset",
      status: "edge-only",
      site: env.SITE_URL || SITE,
      message: "Set ORIGIN secret to enable backend proxying",
      health: url.origin + "/health",
    }), request);
  },
};
