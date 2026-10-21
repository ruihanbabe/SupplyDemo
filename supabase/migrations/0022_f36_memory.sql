-- F36 P3: only confirmed or deterministic long-term memory is usable.
begin;

create table if not exists app.long_term_memory (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  content jsonb not null check (jsonb_typeof(content) = 'object' and octet_length(content::text) between 2 and 16000),
  source_ref jsonb not null default '{"legacy":true}'::jsonb check (jsonb_typeof(source_ref) = 'object' and jsonb_object_length(source_ref) > 0),
  source_kind text not null check (source_kind in ('model_candidate', 'deterministic')),
  source_version_id uuid references app.knowledge_document_version(id) on delete restrict,
  status text not null check (status in ('candidate', 'confirmed', 'revoked', 'pending_revalidation')),
  proposed_by uuid not null,
  confirmed_by uuid,
  created_at timestamptz not null default now(),
  check ((status = 'confirmed') = (confirmed_by is not null))
);
alter table app.long_term_memory add column if not exists source_ref jsonb not null default '{"legacy":true}'::jsonb;
create index if not exists long_term_memory_org_status_idx on app.long_term_memory(organization_id, status);

alter table app.long_term_memory enable row level security;
alter table app.long_term_memory force row level security;
drop policy if exists long_term_memory_isolation on app.long_term_memory;
create policy long_term_memory_isolation on app.long_term_memory for select to supplyagent_api
  using (organization_id = app.current_organization_id() and app.is_member(organization_id, app.current_actor_id()));

create or replace function app.propose_long_term_memory(input_content jsonb, input_source_ref jsonb, input_trace_run_id uuid, input_case_id uuid default null, input_source_version_id uuid default null)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare memory_id uuid;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null
    or not app.is_member(app.current_organization_id(), app.current_actor_id()) then
    raise exception 'missing organization membership' using errcode = '42501';
  end if;
  if input_source_version_id is not null and not exists (
    select 1 from app.knowledge_document_version
    where id=input_source_version_id and organization_id=app.current_organization_id()
  ) then
    raise exception 'memory source version outside organization' using errcode = '42501';
  end if;
  insert into app.long_term_memory(organization_id,content,source_ref,source_kind,source_version_id,status,proposed_by)
  values(app.current_organization_id(), input_content, input_source_ref, 'model_candidate', input_source_version_id, 'candidate', app.current_actor_id())
  returning id into memory_id;
  perform app.append_trace_event(input_trace_run_id, input_case_id, 'memory.candidate', 'Long-term memory candidate saved',
    jsonb_build_object('memoryId', memory_id, 'status', 'candidate'), null);
  return memory_id;
end;
$$;

create or replace function app.record_deterministic_long_term_memory(input_content jsonb, input_source_ref jsonb, input_trace_run_id uuid, input_case_id uuid default null, input_source_version_id uuid default null)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare memory_id uuid;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null
    or not app.is_member(app.current_organization_id(), app.current_actor_id()) then
    raise exception 'missing organization membership' using errcode = '42501';
  end if;
  if input_source_version_id is not null and not exists (select 1 from app.knowledge_document_version where id=input_source_version_id and organization_id=app.current_organization_id()) then
    raise exception 'memory source version outside organization' using errcode = '42501';
  end if;
  insert into app.long_term_memory(organization_id,content,source_ref,source_kind,source_version_id,status,proposed_by,confirmed_by)
  values(app.current_organization_id(), input_content, input_source_ref, 'deterministic', input_source_version_id, 'confirmed', app.current_actor_id(), app.current_actor_id())
  returning id into memory_id;
  perform app.append_trace_event(input_trace_run_id, input_case_id, 'memory.confirmed', 'Deterministic long-term memory saved', jsonb_build_object('memoryId', memory_id, 'status', 'confirmed'), null);
  return memory_id;
end;
$$;

create or replace function app.confirm_long_term_memory(input_memory_id uuid, input_trace_run_id uuid, input_case_id uuid default null)
returns void language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if not exists (select 1 from app.organization_member where organization_id=app.current_organization_id()
    and user_id=app.current_actor_id() and role in ('organization_admin','sales_manager','buyer','approver','hardware_engineer','planner')) then
    raise exception 'human confirmation capability required' using errcode = '42501';
  end if;
  update app.long_term_memory set status='confirmed', confirmed_by=app.current_actor_id()
  where id=input_memory_id and organization_id=app.current_organization_id() and status='candidate';
  if not found then raise exception 'candidate memory not found in organization' using errcode = '42501'; end if;
  perform app.append_trace_event(input_trace_run_id, input_case_id, 'memory.confirmed', 'Long-term memory confirmed',
    jsonb_build_object('memoryId', input_memory_id, 'status', 'confirmed'), null);
end;
$$;

create or replace function app.revoke_long_term_memory(input_memory_id uuid, input_trace_run_id uuid, input_case_id uuid default null)
returns void language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if not exists (select 1 from app.organization_member where organization_id=app.current_organization_id()
    and user_id=app.current_actor_id() and role in ('organization_admin','sales_manager','buyer','approver','hardware_engineer','planner')) then
    raise exception 'human revocation capability required' using errcode = '42501';
  end if;
  update app.long_term_memory set status='revoked', confirmed_by=null
  where id=input_memory_id and organization_id=app.current_organization_id() and status <> 'revoked';
  if not found then raise exception 'memory not found in organization' using errcode = '42501'; end if;
  perform app.append_trace_event(input_trace_run_id, input_case_id, 'memory.revoked', 'Long-term memory revoked',
    jsonb_build_object('memoryId', input_memory_id, 'status', 'revoked'), null);
end;
$$;

create or replace function app.mark_memory_pending_revalidation(input_version_id uuid, input_trace_run_id uuid, input_case_id uuid default null)
returns integer language plpgsql security definer set search_path = app, pg_temp as $$
declare affected integer;
begin
  update app.long_term_memory set status='pending_revalidation', confirmed_by=null
  where organization_id=app.current_organization_id() and source_version_id=input_version_id and status='confirmed';
  get diagnostics affected = row_count;
  if affected > 0 then
    perform app.append_trace_event(input_trace_run_id, input_case_id, 'memory.pending_revalidation', 'Long-term memory requires revalidation',
      jsonb_build_object('sourceVersionId', input_version_id, 'affected', affected), null);
  end if;
  return affected;
end;
$$;

create or replace function app.invalidate_memory_for_knowledge_change()
returns trigger language plpgsql security definer set search_path = app, pg_temp as $$
declare affected integer; declare trace_id uuid;
begin
  if tg_table_name = 'knowledge_document_version' then
    update app.long_term_memory set status='pending_revalidation', confirmed_by=null
    where organization_id=new.organization_id and status='confirmed' and source_version_id in (
      select id from app.knowledge_document_version where document_id=new.document_id and id <> new.id
    );
  else
    update app.long_term_memory set status='pending_revalidation', confirmed_by=null
    where organization_id=new.organization_id and status='confirmed' and source_version_id=new.version_id;
  end if;
  get diagnostics affected = row_count;
  if affected > 0 then
    trace_id := app.begin_trace_run('api', null);
    perform app.append_trace_event(trace_id, null, 'memory.pending_revalidation', 'Knowledge change requires memory revalidation',
      jsonb_build_object('affected', affected), null);
  end if;
  return new;
end;
$$;

drop trigger if exists long_term_memory_knowledge_version_changed on app.knowledge_document_version;
create trigger long_term_memory_knowledge_version_changed after insert on app.knowledge_document_version
  for each row execute function app.invalidate_memory_for_knowledge_change();
drop trigger if exists long_term_memory_knowledge_version_revoked on app.knowledge_version_revocation;
create trigger long_term_memory_knowledge_version_revoked after insert on app.knowledge_version_revocation
  for each row execute function app.invalidate_memory_for_knowledge_change();

revoke all on app.long_term_memory from public;
grant select on app.long_term_memory to supplyagent_api;
revoke all on function app.propose_long_term_memory(jsonb,jsonb,uuid,uuid,uuid), app.record_deterministic_long_term_memory(jsonb,jsonb,uuid,uuid,uuid), app.confirm_long_term_memory(uuid,uuid,uuid),
  app.revoke_long_term_memory(uuid,uuid,uuid), app.mark_memory_pending_revalidation(uuid,uuid,uuid) from public;
grant execute on function app.propose_long_term_memory(jsonb,jsonb,uuid,uuid,uuid), app.record_deterministic_long_term_memory(jsonb,jsonb,uuid,uuid,uuid), app.confirm_long_term_memory(uuid,uuid,uuid),
  app.revoke_long_term_memory(uuid,uuid,uuid), app.mark_memory_pending_revalidation(uuid,uuid,uuid) to supplyagent_api;
commit;
