-- Canonical migration applied to Supabase project wkewimymzbhgbkumlxmg.
-- Never store Telegram session strings in plaintext. The application must encrypt
-- sessions before writing encrypted_session and keep the encryption key outside Postgres.
create extension if not exists pgcrypto;

create type public.app_role as enum ('owner','admin','operator','viewer');
create type public.approval_status as enum ('pending','approved','rejected','expired','executed');
create type public.account_status as enum ('pending','active','disabled','risk','revoked');

create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  role public.app_role not null default 'viewer',
  disabled_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.telegram_accounts (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references public.profiles(id) on delete restrict,
  label text not null,
  phone_masked text,
  telegram_user_id bigint,
  username text,
  status public.account_status not null default 'pending',
  encrypted_session bytea,
  encryption_key_version integer not null default 1 check (encryption_key_version > 0),
  proxy_config jsonb not null default '{}'::jsonb,
  last_health_check_at timestamptz,
  last_seen_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(owner_id, label)
);

create table public.account_access (
  account_id uuid not null references public.telegram_accounts(id) on delete cascade,
  user_id uuid not null references public.profiles(id) on delete cascade,
  role public.app_role not null default 'viewer',
  created_at timestamptz not null default now(),
  primary key(account_id, user_id)
);

create table public.schedules (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references public.telegram_accounts(id) on delete cascade,
  created_by uuid not null references public.profiles(id) on delete restrict,
  name text not null,
  task_type text not null,
  payload jsonb not null default '{}'::jsonb,
  cron_expression text,
  run_at timestamptz,
  timezone text not null default 'Asia/Manila',
  enabled boolean not null default true,
  last_run_at timestamptz,
  next_run_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (cron_expression is not null or run_at is not null)
);

create table public.action_approvals (
  id uuid primary key default gen_random_uuid(),
  account_id uuid references public.telegram_accounts(id) on delete cascade,
  requested_by uuid not null references public.profiles(id) on delete restrict,
  reviewed_by uuid references public.profiles(id) on delete restrict,
  action_name text not null,
  action_hash text not null,
  arguments jsonb not null default '{}'::jsonb,
  status public.approval_status not null default 'pending',
  expires_at timestamptz not null,
  reviewed_at timestamptz,
  executed_at timestamptz,
  created_at timestamptz not null default now()
);

create table public.audit_events (
  id bigint generated always as identity primary key,
  actor_id uuid references public.profiles(id) on delete set null,
  account_id uuid references public.telegram_accounts(id) on delete set null,
  request_id uuid,
  action text not null,
  resource_type text,
  resource_id text,
  result text not null,
  ip_hash text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table public.backup_catalog (
  id uuid primary key default gen_random_uuid(),
  object_key text not null unique,
  storage_provider text not null,
  checksum_sha256 text not null,
  encrypted boolean not null default true,
  size_bytes bigint not null check (size_bytes >= 0),
  status text not null,
  created_at timestamptz not null default now(),
  verified_at timestamptz,
  restored_at timestamptz
);

create index telegram_accounts_owner_idx on public.telegram_accounts(owner_id);
create index telegram_accounts_status_idx on public.telegram_accounts(status);
create index schedules_due_idx on public.schedules(enabled, next_run_at);
create index approvals_pending_idx on public.action_approvals(status, expires_at);
create index audit_events_created_idx on public.audit_events(created_at desc);
create index audit_events_account_idx on public.audit_events(account_id, created_at desc);

create or replace function public.current_app_role()
returns public.app_role language sql stable security definer set search_path = public
as $$ select coalesce((select role from public.profiles where id = auth.uid() and disabled_at is null), 'viewer'::public.app_role) $$;

create or replace function public.is_admin()
returns boolean language sql stable security definer set search_path = public
as $$ select public.current_app_role() in ('owner'::public.app_role, 'admin'::public.app_role) $$;

create or replace function public.touch_updated_at()
returns trigger language plpgsql set search_path = public
as $$ begin new.updated_at = now(); return new; end $$;

create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public
as $$
begin
  insert into public.profiles(id, display_name)
  values(new.id, coalesce(new.raw_user_meta_data ->> 'display_name', new.email))
  on conflict (id) do nothing;
  return new;
end
$$;

create trigger on_auth_user_created after insert on auth.users
for each row execute function public.handle_new_user();
create trigger profiles_touch before update on public.profiles
for each row execute function public.touch_updated_at();
create trigger telegram_accounts_touch before update on public.telegram_accounts
for each row execute function public.touch_updated_at();
create trigger schedules_touch before update on public.schedules
for each row execute function public.touch_updated_at();

alter table public.profiles enable row level security;
alter table public.telegram_accounts enable row level security;
alter table public.account_access enable row level security;
alter table public.schedules enable row level security;
alter table public.action_approvals enable row level security;
alter table public.audit_events enable row level security;
alter table public.backup_catalog enable row level security;

create policy profiles_read_self_or_admin on public.profiles for select to authenticated
using (id = auth.uid() or public.is_admin());
create policy profiles_update_self on public.profiles for update to authenticated
using (id = auth.uid()) with check (id = auth.uid());

create policy accounts_read_authorized on public.telegram_accounts for select to authenticated
using (owner_id = auth.uid() or public.is_admin() or exists(
  select 1 from public.account_access aa where aa.account_id = id and aa.user_id = auth.uid()
));
create policy accounts_manage_owner_admin on public.telegram_accounts for all to authenticated
using (owner_id = auth.uid() or public.is_admin())
with check (owner_id = auth.uid() or public.is_admin());

create policy access_read_authorized on public.account_access for select to authenticated
using (user_id = auth.uid() or public.is_admin() or exists(
  select 1 from public.telegram_accounts ta where ta.id = account_id and ta.owner_id = auth.uid()
));
create policy access_manage_owner_admin on public.account_access for all to authenticated
using (public.is_admin() or exists(
  select 1 from public.telegram_accounts ta where ta.id = account_id and ta.owner_id = auth.uid()
))
with check (public.is_admin() or exists(
  select 1 from public.telegram_accounts ta where ta.id = account_id and ta.owner_id = auth.uid()
));

create policy schedules_read_authorized on public.schedules for select to authenticated
using (public.is_admin() or created_by = auth.uid() or exists(
  select 1 from public.telegram_accounts ta where ta.id = account_id and ta.owner_id = auth.uid()
) or exists(
  select 1 from public.account_access aa where aa.account_id = schedules.account_id and aa.user_id = auth.uid()
));
create policy schedules_manage_operator on public.schedules for all to authenticated
using (public.is_admin() or created_by = auth.uid() or exists(
  select 1 from public.telegram_accounts ta where ta.id = account_id and ta.owner_id = auth.uid()
) or exists(
  select 1 from public.account_access aa where aa.account_id = schedules.account_id and aa.user_id = auth.uid()
    and aa.role in ('owner','admin','operator')
))
with check (public.is_admin() or created_by = auth.uid() or exists(
  select 1 from public.telegram_accounts ta where ta.id = account_id and ta.owner_id = auth.uid()
) or exists(
  select 1 from public.account_access aa where aa.account_id = schedules.account_id and aa.user_id = auth.uid()
    and aa.role in ('owner','admin','operator')
));

create policy approvals_read_authorized on public.action_approvals for select to authenticated
using (requested_by = auth.uid() or reviewed_by = auth.uid() or public.is_admin());
create policy approvals_create_authenticated on public.action_approvals for insert to authenticated
with check (requested_by = auth.uid());
create policy approvals_review_admin on public.action_approvals for update to authenticated
using (public.is_admin()) with check (public.is_admin());

create policy audit_read_admin on public.audit_events for select to authenticated
using (public.is_admin());
create policy backup_read_admin on public.backup_catalog for select to authenticated
using (public.is_admin());

revoke all on public.telegram_accounts from anon;
revoke all on public.account_access from anon;
revoke all on public.schedules from anon;
revoke all on public.action_approvals from anon;
revoke all on public.audit_events from anon;
revoke all on public.backup_catalog from anon;
