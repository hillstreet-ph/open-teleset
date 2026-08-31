import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

const CORS = {
  "Access-Control-Allow-Origin": "https://open-teleset.site",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Authorization,Content-Type,apikey",
};

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  return new Response(JSON.stringify({
    status: "ok",
    service: "open-teleset-edge",
    function: "health-ping",
    region: Deno.env.get("SUPABASE_URL")?.includes("ap-southeast") ? "ap-southeast-1" : "unknown",
    ts: new Date().toISOString(),
  }), { headers: { "Content-Type": "application/json", ...CORS } });
});
