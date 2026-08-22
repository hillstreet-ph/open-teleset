// Supabase Edge Function: health ping + optional account wake
// Deploy: supabase functions deploy health-ping
// Secrets: set via `supabase secrets set`

import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

serve(async (req) => {
  const url = new URL(req.url);
  if (req.method === "GET" || req.method === "POST") {
    return new Response(
      JSON.stringify({
        status: "ok",
        service: "open-teleset-edge",
        path: url.pathname,
        ts: new Date().toISOString(),
      }),
      { headers: { "Content-Type": "application/json" } },
    );
  }
  return new Response("Method not allowed", { status: 405 });
});
