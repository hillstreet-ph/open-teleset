import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

// The persistent backend scheduler owns execution. This former audit-only path
// must never advance a schedule or report Telegram delivery that did not occur.
serve(() => new Response(JSON.stringify({
  error: "execution_moved_to_backend_scheduler", executed: 0,
}), { status: 410, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } }));
