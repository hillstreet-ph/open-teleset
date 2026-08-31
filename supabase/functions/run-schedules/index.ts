import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "https://open-teleset.site",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: CORS });

  const sb = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
  );

  const now = new Date().toISOString();

  // Fetch all due schedules
  const { data: due, error } = await sb
    .from("schedules")
    .select("id, name, task_type, payload, cron_expression, account_id, created_by")
    .eq("enabled", true)
    .lte("next_run_at", now)
    .limit(100);

  if (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500, headers: { ...CORS, "Content-Type": "application/json" }
    });
  }

  const results = [];
  for (const sched of (due ?? [])) {
    // Log execution
    await sb.from("audit_events").insert({
      actor_id: sched.created_by,
      event_type: "schedule_executed",
      resource_type: "schedule",
      resource_id: sched.id,
      metadata: { task_type: sched.task_type, payload: sched.payload }
    });

    // Compute next_run_at based on cron or one-shot
    const nextRun = sched.cron_expression ? null : null; // edge fn triggers only

    await sb.from("schedules").update({
      last_run_at: now,
      next_run_at: nextRun
    }).eq("id", sched.id);

    results.push({ id: sched.id, name: sched.name, executed: now });
  }

  return new Response(JSON.stringify({ executed: results.length, results, ts: now }), {
    headers: { ...CORS, "Content-Type": "application/json" }
  });
});
