// Edge function: generate pgvector embeddings for semantic search
import { serve } from "https://deno.land/std@0.177.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "https://open-teleset.site",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: CORS });
  if (req.method !== "POST") return new Response("Method not allowed", { status: 405, headers: CORS });

  const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const { data: { user } } = await sb.auth.getUser((req.headers.get("Authorization") ?? "").replace("Bearer ", ""));
  if (!user) return new Response("Unauthorized", { status: 401, headers: CORS });

  const { content, source_type, source_id, metadata = {} } = await req.json();
  if (!content || !source_type) return new Response(JSON.stringify({ error: "content and source_type required" }), { status: 400, headers: CORS });

  let embedding = null;
  const openaiKey = Deno.env.get("OPENAI_API_KEY");
  if (openaiKey) {
    const embRes = await fetch("https://api.openai.com/v1/embeddings", {
      method: "POST", headers: { Authorization: `Bearer ${openaiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: "text-embedding-3-small", input: content })
    });
    if (embRes.ok) embedding = (await embRes.json()).data[0].embedding;
  }

  const { data, error } = await sb.from("embeddings").upsert(
    { source_type, source_id, owner_id: user.id, content, embedding, metadata },
    { onConflict: "source_type,source_id" }
  ).select().single();

  if (error) return new Response(JSON.stringify({ error: error.message }), { status: 500, headers: CORS });
  return new Response(JSON.stringify({ success: true, id: data.id, embedded: !!embedding }), { headers: { ...CORS, "Content-Type": "application/json" } });
});
