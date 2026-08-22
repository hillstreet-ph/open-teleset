-- open-teleset production schema
create extension if not exists "pgcrypto";

create table if not exists proxies (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  protocol text not null check (protocol in ('http', 'socks5', 'socks4')),
  host text not null,
  port integer not null check (port > 0 and port < 65536),
  username text,
  password_encrypted text,
  is_global boolean not null default false,
  is_active boolean not null default true,
  last_checked_at timestamptz,
  last_latency_ms integer,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists accounts (
  id uuid primary key default gen_random_uuid(),
  name text,
  phone text,
  username text,
  session_encrypted text,
  status text not null default 'pending'
    check (status in ('pending', 'active', 'banned', 'error', 'disabled')),
  proxy_id uuid references proxies(id) on delete set null,
  metadata jsonb not null default '{}'::jsonb,
  last_active_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_accounts_status on accounts(status);
create index if not exists idx_accounts_proxy on accounts(proxy_id);

create table if not exists templates (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  content text not null,
  variables jsonb not null default '[]'::jsonb,
  category text,
  use_count integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists schedules (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  cron text,
  execute_at timestamptz,
  action text not null default 'send_message',
  message text,
  target jsonb not null default '{}'::jsonb,
  account_ids uuid[] not null default '{}',
  enabled boolean not null default true,
  repeat text not null default 'once'
    check (repeat in ('once', 'daily', 'weekly', 'workday', 'cron')),
  run_count integer not null default 0,
  fail_count integer not null default 0,
  last_run_at timestamptz,
  next_run_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_schedules_next on schedules(next_run_at) where enabled;

create table if not exists operation_logs (
  id bigserial primary key,
  level text not null default 'info',
  action text not null,
  account_id uuid references accounts(id) on delete set null,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_logs_created on operation_logs(created_at desc);
create index if not exists idx_logs_account on operation_logs(account_id);

create table if not exists health_checks (
  id bigserial primary key,
  account_id uuid not null references accounts(id) on delete cascade,
  is_healthy boolean not null,
  latency_ms integer,
  error_message text,
  checked_at timestamptz not null default now()
);

create index if not exists idx_health_account on health_checks(account_id, checked_at desc);

create table if not exists stats_daily (
  day date not null,
  account_id uuid not null references accounts(id) on delete cascade,
  messages_sent integer not null default 0,
  messages_received integer not null default 0,
  api_calls integer not null default 0,
  errors integer not null default 0,
  primary key (day, account_id)
);

create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_accounts_updated on accounts;
create trigger trg_accounts_updated before update on accounts
  for each row execute function set_updated_at();

drop trigger if exists trg_proxies_updated on proxies;
create trigger trg_proxies_updated before update on proxies
  for each row execute function set_updated_at();

drop trigger if exists trg_templates_updated on templates;
create trigger trg_templates_updated before update on templates
  for each row execute function set_updated_at();

drop trigger if exists trg_schedules_updated on schedules;
create trigger trg_schedules_updated before update on schedules
  for each row execute function set_updated_at();

alter table accounts enable row level security;
alter table proxies enable row level security;
alter table templates enable row level security;
alter table schedules enable row level security;
alter table operation_logs enable row level security;
alter table health_checks enable row level security;
alter table stats_daily enable row level security;
