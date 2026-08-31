// Edge function: export account data as JSON and upload to storage
import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "https://open-teleset.site",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: CORS });
  const authHeader = req.headers.get("Authorization");
  if (!authHeader) return new Response("Unauthorized", { status: 401, headers: CORS });

  const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const { data: { user }, error: authErr } = await sb.auth.getUser(authHeader.replace("Bearer ", ""));
  if (authErr || !user) return new Response("Unauthorized", { status: 401, headers: CORS });

  const [accounts, templates, schedules] = await Promise.all([
    sb.from("telegram_accounts").select("id,label,phone_masked,username,status,created_at").eq("owner_id", user.id),
    sb.from("message_templates").select("id,name,content,category,created_at").eq("owner_id", user.id),
    sb.from("schedules").select("id,name,task_type,enabled,created_at").eq("created_by", user.id),
  ]);

  const exportData = { exported_at: new Date().toISOString(), user_id: user.id,
    accounts: accounts.data ?? [], message_templates: templates.data ?? [], schedules: schedules.data ?? [] };

  const fileName = `${user.id}/export_${Date.now()}.json`;
  await sb.storage.from("exports").upload(fileName, JSON.stringify(exportData, null, 2), { contentType: "application/json", upsert: true });
  await sb.from("audit_events").insert({ actor_id: user.id, event_type: "data_export", resource_type: "export", metadata: { file: fileName } });
  const { data: urlData } = await sb.storage.from("exports").createSignedUrl(fileName, 600);

  return new Response(JSON.stringify({ success: true, download_url: urlData?.signedUrl, expires_in: 600 }),
    { headers: { ...CORS, "Content-Type": "application/json" } });
});
