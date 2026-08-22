export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/api/health" || url.pathname === "/health") {
      if (env.ORIGIN) {
        try {
          const r = await fetch(`${env.ORIGIN}/api/health`, {
            headers: { Accept: "application/json" },
          });
          return new Response(await r.text(), {
            status: r.status,
            headers: { "content-type": "application/json" },
          });
        } catch (e) {
          return Response.json({ status: "degraded", error: String(e) }, { status: 503 });
        }
      }
      return Response.json({ status: "ok", edge: "cloudflare" });
    }
    if (env.ORIGIN) {
      const target = new URL(url.pathname + url.search, env.ORIGIN);
      const init = { method: request.method, headers: request.headers, redirect: "manual" };
      if (request.method !== "GET" && request.method !== "HEAD") init.body = request.body;
      return fetch(target.toString(), init);
    }
    return new Response("open-teleset edge — set ORIGIN to your API host", { status: 200 });
  },
};
