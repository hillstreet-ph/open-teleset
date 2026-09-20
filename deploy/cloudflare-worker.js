const SITE = "https://open-teleset.site";

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
  const origin = request.headers.get("Origin");
  if (ALLOWED_ORIGINS.has(origin)) r.headers.set("Access-Control-Allow-Origin", origin);
  r.headers.append("Vary", "Origin");
  return r;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Handle preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors(new Response(null), request).headers });
    }

    // Health check — always responds, even without ORIGIN
    if (url.pathname === "/api/health" || url.pathname === "/health") {
      if (env.ORIGIN) {
        try {
          const r = await fetch(`${env.ORIGIN}/health`, {
            headers: { Accept: "application/json" },
            cf: { cacheTtl: 0 },
          });
          return cors(new Response(await r.text(), {
            status: r.status,
            headers: { "content-type": "application/json" },
          }), request);
        } catch (e) {
          return cors(Response.json({ status: "degraded", error: "origin_unavailable" }, { status: 503 }), request);
        }
      }
      return cors(Response.json({
        status: "degraded",
        edge: "cloudflare",
        site: env.SITE_URL || SITE,
        ts: new Date().toISOString(),
      }, { status: 503 }), request);
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
      } catch (e) {
        return cors(Response.json({ error: "upstream unavailable", detail: "origin_unavailable" }, { status: 502 }), request);
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
