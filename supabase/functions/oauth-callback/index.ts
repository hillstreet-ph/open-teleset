// Edge function: OAuth callback handler — redirects to app with session
import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

const SITE = "https://open-teleset.site";

serve(async (req) => {
  const url = new URL(req.url);
  const code = url.searchParams.get("code");
  const error = url.searchParams.get("error");
  const next = url.searchParams.get("next") ?? "/dashboard.html";

  if (error) {
    return Response.redirect(`${SITE}/dashboard.html?auth_error=${encodeURIComponent(error)}`, 302);
  }

  if (!code) {
    return Response.redirect(`${SITE}/dashboard.html`, 302);
  }

  // Supabase handles the token exchange internally via auth redirect
  // Redirect back to dashboard — Supabase JS SDK picks up the session from URL hash
  return Response.redirect(`${SITE}${next}`, 302);
});
