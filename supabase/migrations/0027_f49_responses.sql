-- F49 private model exchanges; never expose request bodies through public Trace.
begin;
alter table app.model_policy add column if not exists reasoning boolean not null default false;
alter table app.model_policy add column if not exists context_window integer not null default 32768 check(context_window between 1024 and 2000000);
alter table app.model_policy add column if not exists cached_input_price_micros_per_million bigint check(cached_input_price_micros_per_million>=0);
create or replace function app.lock_responses_model_policy(input_id uuid) returns setof app.model_policy
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if not app.is_member(app.current_organization_id(),app.current_actor_id()) then
  raise exception 'model policy outside scope' using errcode='42501'; end if;
 return query select * from app.model_policy where id=input_id and organization_id=app.current_organization_id() and active for update;
end $$;
revoke all on function app.lock_responses_model_policy(uuid) from public;
grant execute on function app.lock_responses_model_policy(uuid) to supplyagent_api;
create table if not exists app.model_exchange(
 reservation_id uuid primary key references app.model_reservation(id),
 organization_id uuid not null references app.organization(id), trace_run_id uuid not null references app.trace_run(id),
 actor_id uuid not null, invocation_id uuid not null, call_ordinal integer not null check(call_ordinal between 1 and 1000),
 schema_version integer not null default 1 check(schema_version=1),
 request jsonb not null check(jsonb_typeof(request)='object' and octet_length(request::text)<=131072),
 policy_snapshot jsonb not null check(jsonb_typeof(policy_snapshot)='object'),
 response jsonb check(response is null or (jsonb_typeof(response)='object' and octet_length(response::text)<=1048576)),
 state text not null default 'running' check(state in ('running','completed','incomplete','failed','interrupted')),
 created_at timestamptz not null default clock_timestamp(), finished_at timestamptz,
 unique(organization_id,invocation_id,call_ordinal)
);
alter table app.model_exchange enable row level security;
alter table app.model_exchange force row level security;
revoke all on app.model_exchange from public;
grant select,insert,update on app.model_exchange to supplyagent_api;
drop policy if exists model_exchange_owner on app.model_exchange;
create policy model_exchange_owner on app.model_exchange to supplyagent_api
 using(organization_id=app.current_organization_id() and actor_id=app.current_actor_id() and app.is_member(organization_id,actor_id))
 with check(organization_id=app.current_organization_id() and actor_id=app.current_actor_id() and app.is_member(organization_id,actor_id)
  and exists(select 1 from app.trace_run t where t.id=trace_run_id and t.organization_id=app.current_organization_id() and (t.actor_id=app.current_actor_id() or app.is_runtime_worker()))
  and exists(select 1 from app.model_reservation r where r.id=reservation_id and r.organization_id=app.current_organization_id() and r.run_id=trace_run_id));
create or replace function app.guard_model_exchange() returns trigger language plpgsql set search_path=app,pg_temp as $$
begin
 if tg_op='UPDATE' and (old.state<>'running' or new.state='running' or
    (to_jsonb(new)-array['response','state','finished_at']) is distinct from (to_jsonb(old)-array['response','state','finished_at'])) then
   raise exception 'model exchange can only be finalized once' using errcode='22023'; end if;
 return new;
end $$;
drop trigger if exists model_exchange_guard on app.model_exchange;
create trigger model_exchange_guard before update on app.model_exchange for each row execute function app.guard_model_exchange();
commit;
