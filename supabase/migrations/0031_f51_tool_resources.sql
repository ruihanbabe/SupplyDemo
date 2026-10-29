-- F51 P3: lease-fenced shared capacity. Expiry quarantines; never auto-releases.
begin;
create table if not exists app.tool_resource_slot(
 id uuid primary key default gen_random_uuid(), resource_key text not null,
 organization_id uuid not null references app.organization(id), execution_id uuid not null references app.tool_execution(id),
 worker_actor_id uuid not null, owner_token uuid not null, units integer not null check(units>0),
 capacity integer not null check(capacity>0 and units<=capacity),
 state text not null check(state in ('held','quarantined','released')),
 expires_at timestamptz not null, created_at timestamptz not null default clock_timestamp(), released_at timestamptz,
 unique(execution_id,resource_key)
);
create index if not exists tool_resource_active on app.tool_resource_slot(resource_key) where state<>'released';
create or replace function app.tool_resource_acquire(input_execution uuid,input_owner text,input_token uuid,input_resources jsonb,input_seconds integer)
returns boolean language plpgsql security definer set search_path=app,pg_temp as $$
declare e app.tool_execution%rowtype; declare r jsonb; declare k text; declare used integer;
begin
 select * into e from app.tool_execution where id=input_execution and organization_id=app.current_organization_id();
 if e.id is null or e.worker_actor_id<>app.current_actor_id() then raise exception 'lease_lost' using errcode='42501'; end if;
 perform app.tool_guard(e.job_id,input_owner,e.generation);
 if e.state<>'prepared' or input_seconds not between 1 and 3600 or jsonb_typeof(input_resources)<>'array' then
  raise exception 'invalid admission' using errcode='22023'; end if;
 if exists(select 1 from app.tool_resource_slot where execution_id=e.id) then
  raise exception 'duplicate admission' using errcode='22023'; end if;
 -- Acquire every pool lock in stable order; no partial reservations on capacity failure.
 for r in select value from jsonb_array_elements(input_resources) order by value->>'key' loop
  k:=r->>'key';
  if k !~ '^(deployment|organization:[0-9a-f-]{36}):[a-z][a-z0-9_.-]{1,95}$'
    or (k like 'organization:%' and k not like 'organization:'||e.organization_id::text||':%')
    or (r->>'units')::integer<1 or (r->>'capacity')::integer<(r->>'units')::integer then
   raise exception 'invalid resource scope' using errcode='22023'; end if;
  perform pg_advisory_xact_lock(hashtextextended('f51:'||k,0));
 end loop;
 for r in select value from jsonb_array_elements(input_resources) loop
  k:=r->>'key';
  if exists(select 1 from app.tool_resource_slot where resource_key=k and state<>'released' and capacity<>(r->>'capacity')::integer) then
   raise exception 'resource capacity mismatch' using errcode='22023'; end if;
  select coalesce(sum(units),0) into used from app.tool_resource_slot where resource_key=k and state<>'released';
  if used+(r->>'units')::integer>(r->>'capacity')::integer then return false; end if;
 end loop;
 for r in select value from jsonb_array_elements(input_resources) loop
  insert into app.tool_resource_slot(resource_key,organization_id,execution_id,worker_actor_id,owner_token,units,capacity,state,expires_at)
  values(r->>'key',e.organization_id,e.id,e.worker_actor_id,input_token,(r->>'units')::integer,(r->>'capacity')::integer,
   'held',least(clock_timestamp()+make_interval(secs=>input_seconds),(select lease_expires_at from app.runtime_job where id=e.job_id)));
 end loop;
 return true;
end $$;
create or replace function app.tool_resource_renew(input_execution uuid,input_owner text,input_token uuid,input_seconds integer)
returns void language plpgsql security definer set search_path=app,pg_temp as $$
declare e app.tool_execution%rowtype;
begin
 select * into e from app.tool_execution where id=input_execution and organization_id=app.current_organization_id();
 if e.id is null or e.worker_actor_id<>app.current_actor_id() then raise exception 'lease_lost' using errcode='42501'; end if;
 perform app.tool_guard(e.job_id,input_owner,e.generation);
 if input_seconds not between 1 and 3600 or not exists(select 1 from app.tool_resource_slot where execution_id=e.id and owner_token=input_token)
 or exists(select 1 from app.tool_resource_slot where execution_id=e.id and
  (owner_token<>input_token or state<>'held' or expires_at<=clock_timestamp())) then
  raise exception 'lease_lost' using errcode='42501'; end if;
 update app.tool_resource_slot set expires_at=least(clock_timestamp()+make_interval(secs=>input_seconds),
  (select lease_expires_at from app.runtime_job where id=e.job_id)) where execution_id=e.id and owner_token=input_token;
end $$;
create or replace function app.tool_resource_release(input_execution uuid,input_token uuid,input_clean boolean)
returns void language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if not app.is_runtime_worker() then raise exception 'worker required' using errcode='42501'; end if;
 update app.tool_resource_slot set state=case when input_clean then 'released' else 'quarantined' end,
 released_at=case when input_clean then clock_timestamp() end
 where execution_id=input_execution and owner_token=input_token and organization_id=app.current_organization_id()
 and worker_actor_id=app.current_actor_id() and state<>'released';
end $$;
create or replace function app.tool_resource_reap() returns integer
language plpgsql security definer set search_path=app,pg_temp as $$
declare n integer;
begin
 if not app.is_runtime_worker() then raise exception 'worker required' using errcode='42501'; end if;
 update app.tool_resource_slot s set state='quarantined' from app.tool_execution e join app.runtime_job j on j.id=e.job_id
 where s.execution_id=e.id and s.organization_id=app.current_organization_id() and s.state='held'
 and (s.expires_at<=clock_timestamp() or j.attempts<>e.generation or j.status<>'leased' or j.lease_expires_at<=clock_timestamp());
 get diagnostics n=row_count; return n;
end $$;
alter table app.tool_resource_slot enable row level security;
alter table app.tool_resource_slot force row level security;
revoke all on app.tool_resource_slot from public;
grant select on app.tool_resource_slot to supplyagent_api;
drop policy if exists tool_resource_worker on app.tool_resource_slot;
create policy tool_resource_worker on app.tool_resource_slot for select to supplyagent_api using(organization_id=app.current_organization_id() and app.is_runtime_worker());
revoke all on function app.tool_resource_acquire(uuid,text,uuid,jsonb,integer),app.tool_resource_renew(uuid,text,uuid,integer),app.tool_resource_release(uuid,uuid,boolean),app.tool_resource_reap() from public;
grant execute on function app.tool_resource_acquire(uuid,text,uuid,jsonb,integer),app.tool_resource_renew(uuid,text,uuid,integer),app.tool_resource_release(uuid,uuid,boolean),app.tool_resource_reap() to supplyagent_api;
commit;
