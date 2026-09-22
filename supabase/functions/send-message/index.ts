import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

const CORS = {
  "Access-Control-Allow-Origin": "https://open-teleset.site",
  "Access-Control-Allow-Methods": "POST,OPTIONS",
  "Access-Control-Allow-Headers": "Authorization,Content-Type,apikey,x-client-info",
};
const reply = (body: object, status: number) => new Response(JSON.stringify(body), {
  status, headers: { ...CORS, "Content-Type": "application/json", "Cache-Control": "no-store" },
});

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  if (req.method !== "POST") return reply({ error: "method_not_allowed" }, 405);
  const authorization = req.headers.get("Authorization") || "";
  if (!/^Bearer \S+$/i.test(authorization)) return reply({ error: "unauthorized" }, 401);
  let origin: URL;
  try {
    origin = new URL(Deno.env.get("TELESET_API_ORIGIN") || "");
    if (origin.protocol !== "https:" || origin.username || origin.password || origin.pathname !== "/" || origin.search || origin.hash) throw new Error();
  } catch {
    return reply({ error: "runtime_not_configured", queued: false }, 503);
  }
  const body = await req.json().catch(() => null);
  if (!body || !body.account_id || !Array.isArray(body.recipients) || !body.recipients.length || body.recipients.length > 20 || !body.message || !body.approval_id) {
    return reply({ error: "account_recipients_message_and_approval_required", queued: false }, 400);
  }
  try {
    // The canonical runtime validates the user/project, consent and every send.
    // No service-role credential, audit-only fake queue or alternate send path.
    const response = await fetch(new URL("/api/bulk/send-personal", origin), {
      method: "POST", redirect: "error", signal: AbortSignal.timeout(90000),
      headers: { Authorization: authorization, "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: body.account_id, targets: body.recipients,
        message: body.message, approval_id: body.approval_id, delay: body.delay ?? 3 }),
    });
    return new Response(await response.text(), { status: response.status,
      headers: { ...CORS, "Content-Type": "application/json", "Cache-Control": "no-store" } });
  } catch {
    // A timeout has unknown delivery outcome; callers must reconcile, not blindly retry.
    return reply({ error: "runtime_unavailable_or_timeout", delivery_status: "unknown", retry_safe: false }, 502);
  }
});
