import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "https://open-teleset.site",
  "Access-Control-Allow-Methods": "POST,OPTIONS",
  "Access-Control-Allow-Headers": "Authorization,Content-Type,apikey",
};

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

  const sb = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
  );

  const now = new Date().toISOString();
  const { data: due, error } = await sb
    .from("schedules")
    .select("id,name,task_type,payload,account_id,next_run_at")
    .eq("enabled", true)
    .not("next_run_at", "is", null)
    .lte("next_run_at", now)
    .limit(100);

  if (error) return new Response(JSON.stringify({ error: error.message }), {
    status: 500, headers: { "Content-Type": "application/json", ...CORS }
  });

  let executed = 0;
  for (const s of due ?? []) {
    const intervals: Record<string, number> = { daily: 86400000, weekly: 604800000, hourly: 3600000 };
    const nextRun = intervals[s.task_type] ? new Date(Date.now() + intervals[s.task_type]).toISOString() : null;
    await sb.from("schedules").update({
      last_run_at: now, next_run_at: nextRun, enabled: s.task_type !== "once",
    }).eq("id", s.id);
    await sb.from("audit_events").insert({
      event_type: "schedule_executed", resource_id: s.id,
      metadata: { schedule_name: s.name, task_type: s.task_type },
    });
    executed++;
  }
  return new Response(JSON.stringify({ due: due?.length ?? 0, executed, ts: now }), {
    headers: { "Content-Type": "application/json", ...CORS }
  });
});
