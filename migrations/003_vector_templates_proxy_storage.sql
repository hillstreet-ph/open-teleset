-- Migration 003: vector embeddings, message_templates, proxy_configs, storage buckets
-- Applied: 2026-08-31

-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS http;

-- Vector embeddings
CREATE TABLE IF NOT EXISTS public.embeddings (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_type text NOT NULL,
  source_id   uuid,
  owner_id    uuid REFERENCES public.profiles(id) ON DELETE CASCADE,
  content     text NOT NULL,
  embedding   vector(1536),
  metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS embeddings_owner_idx  ON public.embeddings(owner_id);
CREATE INDEX IF NOT EXISTS embeddings_source_idx ON public.embeddings(source_type, source_id);
CREATE INDEX IF NOT EXISTS embeddings_hnsw_idx
  ON public.embeddings USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
ALTER TABLE public.embeddings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "embeddings_owner" ON public.embeddings;
CREATE POLICY "embeddings_owner" ON public.embeddings USING (owner_id = auth.uid());

-- Message templates
CREATE TABLE IF NOT EXISTS public.message_templates (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  name        text NOT NULL,
  content     text NOT NULL,
  category    text NOT NULL DEFAULT 'general',
  variables   jsonb NOT NULL DEFAULT '[]'::jsonb,
  use_count   integer NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS templates_owner_idx    ON public.message_templates(owner_id);
CREATE INDEX IF NOT EXISTS templates_category_idx ON public.message_templates(category);
ALTER TABLE public.message_templates ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "templates_owner" ON public.message_templates;
CREATE POLICY "templates_owner" ON public.message_templates USING (owner_id = auth.uid());
DROP TRIGGER IF EXISTS touch_templates_updated_at ON public.message_templates;
CREATE TRIGGER touch_templates_updated_at
  BEFORE UPDATE ON public.message_templates
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

-- Proxy configs
CREATE TABLE IF NOT EXISTS public.proxy_configs (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  label       text NOT NULL,
  protocol    text NOT NULL DEFAULT 'socks5'
                CHECK (protocol IN ('socks5','http','https','socks4')),
  host        text NOT NULL,
  port        integer NOT NULL CHECK (port BETWEEN 1 AND 65535),
  username    text,
  encrypted_password text,
  is_global   boolean NOT NULL DEFAULT false,
  last_tested_at timestamptz,
  last_ok     boolean,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_id, label)
);
CREATE INDEX IF NOT EXISTS proxy_owner_idx ON public.proxy_configs(owner_id);
ALTER TABLE public.proxy_configs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "proxy_owner" ON public.proxy_configs;
CREATE POLICY "proxy_owner" ON public.proxy_configs USING (owner_id = auth.uid());

-- Semantic search function
CREATE OR REPLACE FUNCTION public.match_embeddings(
  query_embedding vector(1536),
  match_threshold float DEFAULT 0.7,
  match_count     int DEFAULT 10,
  filter_type     text DEFAULT NULL
) RETURNS TABLE (id bigint, source_type text, source_id uuid, content text, metadata jsonb, similarity float)
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  RETURN QUERY
  SELECT e.id, e.source_type, e.source_id, e.content, e.metadata,
         1 - (e.embedding <=> query_embedding) AS similarity
  FROM public.embeddings e
  WHERE (filter_type IS NULL OR e.source_type = filter_type)
    AND e.owner_id = auth.uid()
    AND 1 - (e.embedding <=> query_embedding) > match_threshold
  ORDER BY e.embedding <=> query_embedding
  LIMIT match_count;
END;
$$;

-- Storage buckets
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES
  ('session-backups','session-backups',false,10485760,ARRAY['application/octet-stream','application/json']),
  ('public-assets','public-assets',true,5242880,ARRAY['image/png','image/jpeg','image/webp','image/svg+xml']),
  ('exports','exports',false,52428800,ARRAY['application/json','text/csv','application/zip'])
ON CONFLICT (id) DO NOTHING;

-- Storage policies
DROP POLICY IF EXISTS "backups_owner_only"     ON storage.objects;
DROP POLICY IF EXISTS "exports_owner_only"     ON storage.objects;
DROP POLICY IF EXISTS "public_assets_read"     ON storage.objects;
DROP POLICY IF EXISTS "public_assets_write_auth" ON storage.objects;
CREATE POLICY "backups_owner_only"     ON storage.objects FOR ALL USING (bucket_id='session-backups' AND auth.uid()::text=(storage.foldername(name))[1]);
CREATE POLICY "exports_owner_only"     ON storage.objects FOR ALL USING (bucket_id='exports' AND auth.uid()::text=(storage.foldername(name))[1]);
CREATE POLICY "public_assets_read"     ON storage.objects FOR SELECT USING (bucket_id='public-assets');
CREATE POLICY "public_assets_write_auth" ON storage.objects FOR INSERT WITH CHECK (bucket_id='public-assets' AND auth.role()='authenticated');
