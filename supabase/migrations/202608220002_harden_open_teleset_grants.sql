-- Security-advisor remediation for Open-Teleset.
revoke all on public.profiles from anon;
revoke execute on function public.current_app_role() from public, anon;
grant execute on function public.current_app_role() to authenticated;
revoke execute on function public.is_admin() from public, anon;
grant execute on function public.is_admin() to authenticated;
revoke execute on function public.handle_new_user() from public, anon, authenticated;
revoke execute on function public.touch_updated_at() from public, anon, authenticated;

create index if not exists account_access_user_idx on public.account_access(user_id);
create index if not exists approvals_account_idx on public.action_approvals(account_id);
create index if not exists approvals_requested_by_idx on public.action_approvals(requested_by);
create index if not exists approvals_reviewed_by_idx on public.action_approvals(reviewed_by);
create index if not exists audit_events_actor_idx on public.audit_events(actor_id);
create index if not exists schedules_account_idx on public.schedules(account_id);
create index if not exists schedules_created_by_idx on public.schedules(created_by);
