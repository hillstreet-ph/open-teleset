import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "https://open-teleset.site",
  "Access-Control-Allow-Methods": "POST,OPTIONS",
  "Access-Control-Allow-Headers": "Authorization,Content-Type,apikey,x-client-info",
};

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

  const auth = req.headers.get("Authorization") ?? "";
  const sb = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_ANON_KEY")!,
    { global: { headers: { Authorization: auth } } }
  );

  const { data: { user }, error: authErr } = await sb.auth.getUser();
  if (authErr || !user) {
    return new Response(JSON.stringify({ error: "Unauthorized" }), {
      status: 401, headers: { "Content-Type": "application/json", ...CORS }
    });
  }

  const body = await req.json().catch(() => ({}));
  const { account_id, recipients, message, schedule_id } = body;

  if (!account_id || !recipients?.length || !message) {
    return new Response(JSON.stringify({ error: "account_id, recipients[], message required" }), {
      status: 400, headers: { "Content-Type": "application/json", ...CORS }
    });
  }

  const serviceSb = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
  );

  await serviceSb.from("audit_events").insert({
    actor_id: user.id,
    event_type: "message_send_requested",
    resource_id: account_id,
    metadata: { recipients_count: recipients.length, message_length: message.length, schedule_id: schedule_id ?? null },
  });

  return new Response(JSON.stringify({
    queued: true, account_id, recipients_count: recipients.length,
    message: "Send request logged. Backend MCP worker will process.",
    ts: new Date().toISOString(),
  }), { headers: { "Content-Type": "application/json", ...CORS } });
});
