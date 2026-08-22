// Edge cron entry: mark due schedules (actual Telegram send stays on worker)
// Schedule via Dashboard → Edge Functions → Cron or pg_cron calling this URL

import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

serve(async (req) => {
  const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
  const sb = createClient(supabaseUrl, serviceKey);

  const now = new Date().toISOString();
  const { data, error } = await sb
    .from("schedules")
    .select("id, name, next_run_at, enabled")
    .eq("enabled", true)
    .lte("next_run_at", now)
    .limit(50);

  if (error) {
    return new Response(JSON.stringify({ error: error.message }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }

  return new Response(
    JSON.stringify({
      due: data?.length ?? 0,
      schedules: data ?? [],
      note: "Worker/API should execute Telegram actions for these IDs",
    }),
    { headers: { "Content-Type": "application/json" } },
  );
});
