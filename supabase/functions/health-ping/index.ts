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

  const { count: accountCount } = await sb
    .from("telegram_accounts")
    .select("*", { count: "exact", head: true });

  const { count: scheduleCount } = await sb
    .from("schedules")
    .select("*", { count: "exact", head: true })
    .eq("enabled", true);

  const { count: pendingApprovals } = await sb
    .from("action_approvals")
    .select("*", { count: "exact", head: true })
    .eq("status", "pending");

  return new Response(JSON.stringify({
    status: "ok",
    service: "open-teleset-edge",
    site: "https://open-teleset.site",
    ts: new Date().toISOString(),
    stats: {
      accounts: accountCount ?? 0,
      active_schedules: scheduleCount ?? 0,
      pending_approvals: pendingApprovals ?? 0,
    }
  }), { headers: { ...CORS, "Content-Type": "application/json" } });
});
