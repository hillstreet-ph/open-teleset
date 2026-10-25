-- Server-owned runtime settings (service-role only; never exposed to the browser).
-- Lets an operator configure a runtime value, e.g. the TGStat token for
-- account-free Channel & Group Finder discovery, without a redeploy.
create table if not exists operations_shared.app_settings (
    key text primary key,
    value text not null,
    updated_at timestamptz not null default now()
);

alter table operations_shared.app_settings enable row level security;

-- Intentionally no RLS policies: anon/authenticated roles cannot read or write.
-- Only the server (service-role, which bypasses RLS) reads these settings.
comment on table operations_shared.app_settings is
    'Server-owned runtime settings (service-role only; never exposed to browser). e.g. tgstat_token for account-free channel discovery.';
