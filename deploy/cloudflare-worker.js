const SITE = "https://open-teleset.site";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": SITE,
  "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,Authorization,apikey,x-client-info",
  "Access-Control-Max-Age": "86400",
};

function cors(resp) {
  const r = new Response(resp.body, resp);
  Object.entries(CORS_HEADERS).forEach(([k, v]) => r.headers.set(k, v));
  return r;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Handle preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    // Health check — always responds, even without ORIGIN
    if (url.pathname === "/api/health" || url.pathname === "/health") {
      if (env.ORIGIN) {
        try {
          const r = await fetch(`${env.ORIGIN}/api/health`, {
            headers: { Accept: "application/json" },
            cf: { cacheTtl: 0 },
          });
          return cors(new Response(await r.text(), {
            status: r.status,
            headers: { "content-type": "application/json" },
          }));
        } catch (e) {
          return cors(Response.json({ status: "degraded", error: String(e) }, { status: 503 }));
        }
      }
      return cors(Response.json({
        status: "ok",
        edge: "cloudflare",
        site: env.SITE_URL || SITE,
        ts: new Date().toISOString(),
      }));
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
        return cors(resp);
      } catch (e) {
        return cors(Response.json({ error: "upstream unavailable", detail: String(e) }, { status: 502 }));
      }
    }

    // No ORIGIN set — return status page
    return cors(Response.json({
      service: "open-teleset",
      status: "edge-only",
      site: env.SITE_URL || SITE,
      message: "Set ORIGIN secret to enable backend proxying",
      health: url.origin + "/health",
    }));
  },
};
