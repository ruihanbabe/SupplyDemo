-- F51 P2: private, lease-fenced tool ledger. No business payload duplication.
begin;
create table if not exists app.tool_execution(
 id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id),
 requested_by uuid not null, worker_actor_id uuid not null,
 mission_id uuid not null references app.runtime_mission(id), run_id uuid not null references app.runtime_run(id),
 trace_run_id uuid not null references app.trace_run(id), job_id uuid not null references app.runtime_job(id),
 generation integer not null check(generation>0), invocation_id uuid not null,
 batch_id integer not null check(batch_id>0), call_id text not null check(char_length(call_id) between 1 and 200),
 call_index integer not null check(call_index between 0 and 15), model_call_key text,
 requested_name text not null, canonical_name text, spec_version integer, binding_hash text not null,
 arguments_hash text not null, scheduling_snapshot jsonb not null default '{}', schema_version integer not null default 1,
 state text not null check(state in ('prepared','running','succeeded','rejected','failed','cancelled','abandoned')),
 error_code text, result_kind text, result_id uuid, result_version text, result_hash text,
 created_at timestamptz not null default clock_timestamp(), started_at timestamptz, finished_at timestamptz,
 unique(invocation_id,call_id), unique(invocation_id,batch_id,call_index)
);
create index if not exists tool_execution_job on app.tool_execution(job_id,generation);

create or replace function app.tool_user_access(input_job uuid,input_user uuid) returns boolean
language plpgsql stable security definer set search_path=app,pg_temp as $$
declare m app.runtime_mission%rowtype; declare member_role text; declare o app.crm_opportunity%rowtype;
declare c app.crm_customer%rowtype; declare ref jsonb;
begin
 select x.* into m from app.runtime_mission x join app.runtime_run r on r.mission_id=x.id
 join app.runtime_job j on j.run_id=r.id where j.id=input_job and j.organization_id=app.current_organization_id()
 and r.organization_id=j.organization_id and x.organization_id=j.organization_id;
 if m.id is null or m.created_by<>input_user then return false; end if;
 select role into member_role from app.organization_member where organization_id=m.organization_id and user_id=input_user;
 if member_role is null then return false; end if;
 ref:=m.context_ref;
 if ref->>'opportunityId' is not null then
  if member_role not in ('organization_admin','sales_manager','approver','auditor') then return false; end if;
  select * into o from app.crm_opportunity where id=(ref->>'opportunityId')::uuid and organization_id=m.organization_id;
  select * into c from app.crm_customer where id=o.customer_id and organization_id=m.organization_id;
  if o.id is null or c.id is null or
   (o.scope ? 'member_ids' and not coalesce(o.scope->'member_ids' @> to_jsonb(array[input_user::text]),false)) or
   (c.scope ? 'member_ids' and not coalesce(c.scope->'member_ids' @> to_jsonb(array[input_user::text]),false)) then return false; end if;
 else
  if member_role not in ('organization_admin','sales_manager','buyer','approver','hardware_engineer','planner','auditor') then return false; end if;
 end if;
 return true;
end $$;

create or replace function app.tool_guard(input_job uuid,input_owner text,input_generation integer) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
declare worker uuid:=app.current_actor_id();
declare j app.runtime_job%rowtype; declare ref jsonb; declare u uuid; declare o app.crm_opportunity%rowtype;
begin
 select * into j from app.runtime_job where id=input_job and organization_id=app.current_organization_id() for update;
 if not app.is_runtime_worker() or j.id is null or j.status<>'leased' or j.lease_owner is distinct from input_owner
 or j.attempts<>input_generation or j.lease_expires_at<=clock_timestamp() then
  raise exception 'lease_lost' using errcode='42501'; end if;
 select m.context_ref,m.created_by into ref,u from app.runtime_mission m join app.runtime_run r on r.mission_id=m.id where r.id=j.run_id;
 if not app.tool_user_access(j.id,u) then raise exception 'permission_revoked' using errcode='42501'; end if;
 if ref->>'opportunityId' is not null then
  select * into o from app.crm_opportunity where id=(ref->>'opportunityId')::uuid;
  if o.supply_case_id is distinct from (ref->>'caseId')::uuid then raise exception 'binding_invalid' using errcode='42501'; end if;
 end if;
 if ref->>'caseId' is not null and not exists(select 1 from app.supply_case c where c.id=(ref->>'caseId')::uuid
  and c.organization_id=j.organization_id and c.scope->'partNumbers' ? (ref->>'partNumber')) then
  raise exception 'binding_invalid' using errcode='42501'; end if;
 if ref->'partNumbers' is not null and ref->'partNumbers'<>'null'::jsonb and
  not exists(select 1 from app.supply_case c where c.id=(ref->>'caseId')::uuid and
   (c.scope->'partNumbers') @> (ref->'partNumbers') and (ref->'partNumbers') @> (c.scope->'partNumbers')) then
  raise exception 'binding_invalid' using errcode='42501'; end if;
 if coalesce((ref->>'commercialNodes')::boolean,false) then
  if (select b.bom_revision_id from app.opportunity_bom_binding b where b.opportunity_id=(ref->>'opportunityId')::uuid)
    is distinct from (ref->>'bomRevisionId')::uuid then
    raise exception 'binding_invalid' using errcode='42501'; end if;
  if ref->>'bomRevisionId' is not null and not exists(
    select 1 from app.opportunity_bom_binding b join app.bom_revision v on v.id=b.bom_revision_id
    where b.opportunity_id=(ref->>'opportunityId')::uuid and b.bom_revision_id=(ref->>'bomRevisionId')::uuid
    and b.version=(ref->>'bomBindingVersion')::integer and v.revoked_at is null) then
    raise exception 'binding_invalid' using errcode='42501'; end if;
  if ref->>'bomRevisionId' is not null then
   perform set_config('app.actor_id',u::text,true);
   if not app.can_read_bom_revision((ref->>'bomRevisionId')::uuid) then
    raise exception 'permission_revoked' using errcode='42501'; end if;
   perform set_config('app.actor_id',worker::text,true);
  end if;
 end if;
end $$;

create or replace function app.tool_begin(input_job uuid,input_owner text,input_generation integer,input_meta jsonb,input_args jsonb)
returns table(execution_id uuid,requested_by uuid) language plpgsql security definer set search_path=app,pg_temp as $$
declare j app.runtime_job%rowtype; declare r app.runtime_run%rowtype; declare m app.runtime_mission%rowtype; declare eid uuid;
begin
 perform app.tool_guard(input_job,input_owner,input_generation);
 select * into j from app.runtime_job where id=input_job;
 select * into r from app.runtime_run where id=j.run_id;
 select * into m from app.runtime_mission where id=r.mission_id;
 if exists(select 1 from app.tool_execution e where e.invocation_id=(input_meta->>'invocationId')::uuid and
 (e.job_id<>j.id or e.generation<>input_generation)) then raise exception 'duplicate_call' using errcode='22023'; end if;
 if to_regclass('app.intelligence_attempt') is not null then
 if exists(select 1 from app.intelligence_attempt a where a.id=(input_meta->>'invocationId')::uuid and
 (a.job_id<>j.id or a.generation<>input_generation or a.organization_id<>j.organization_id)) then
 raise exception 'duplicate_call' using errcode='22023'; end if;
 end if;
 insert into app.tool_execution(organization_id,requested_by,worker_actor_id,mission_id,run_id,trace_run_id,job_id,generation,
 invocation_id,batch_id,call_id,call_index,model_call_key,requested_name,canonical_name,spec_version,binding_hash,
 arguments_hash,scheduling_snapshot,state,error_code,finished_at)
 values(j.organization_id,m.created_by,app.current_actor_id(),m.id,r.id,r.trace_run_id,j.id,input_generation,
 (input_meta->>'invocationId')::uuid,(input_meta->>'batchId')::integer,input_meta->>'callId',(input_meta->>'index')::integer,
 input_meta->>'modelCallKey',input_meta->>'requestedName',input_meta->>'canonicalName',(input_meta->>'specVersion')::integer,
 input_meta->>'bindingHash',encode(public.digest(convert_to(input_args::text,'UTF8'),'sha256'),'hex'),
 input_meta->'scheduling',case when input_meta->>'error' is null then 'prepared' else 'rejected' end,input_meta->>'error',
 case when input_meta->>'error' is not null then clock_timestamp() end) returning id into eid;
 return query select eid,m.created_by;
exception when unique_violation then raise exception 'duplicate_call' using errcode='22023';
end $$;

create or replace function app.tool_finish(input_id uuid,input_owner text,input_state text,input_error text,input_result jsonb)
returns void language plpgsql security definer set search_path=app,pg_temp as $$
declare e app.tool_execution%rowtype; declare ref jsonb; declare valid boolean; declare source jsonb;
declare worker uuid:=app.current_actor_id();
begin
 select * into e from app.tool_execution where id=input_id and organization_id=app.current_organization_id();
 if e.id is null or e.worker_actor_id<>worker then raise exception 'lease_lost' using errcode='42501'; end if;
 perform app.tool_guard(e.job_id,input_owner,e.generation);
 select * into e from app.tool_execution where id=input_id for update;
 if e.state not in ('prepared','running') then return; end if;
 if input_state not in ('running','succeeded','failed','cancelled') or (input_state='succeeded' and e.state<>'running') then
  raise exception 'invalid tool transition' using errcode='22023'; end if;
 if input_state='succeeded' then
  if to_regclass('app.tool_resource_slot') is not null then
   if exists(select 1 from app.tool_resource_slot where execution_id=e.id and
    (state='quarantined' or (state='held' and expires_at<=clock_timestamp()))) then
    raise exception 'lease_lost' using errcode='42501'; end if;
  end if;
  ref:=input_result->'citation';
  if ref->>'sourceKind'='intelligence_observation' then
   select exists(select 1 from app.intelligence_observation x join app.intelligence_attempt a on a.id=x.attempt_id
    where x.id=(ref->>'sourceId')::uuid and x.organization_id=e.organization_id and a.id=e.invocation_id
    and a.job_id=e.job_id and a.generation=e.generation and x.tool_name=e.canonical_name
    and x.payload->'execution'->>'toolCallId'=e.call_id
    and (x.payload->'execution'->>'batchId')::integer=e.batch_id
    and (x.payload->'execution'->>'index')::integer=e.call_index
    and x.content_hash=ref->>'fragmentHash' and ref->>'version'='1' and ref->>'locator'='payload') into valid;
  elsif ref->>'sourceKind'='trace_artifact' then
   select exists(select 1 from app.trace_artifact a where a.id=(ref->>'sourceId')::uuid and a.run_id=e.trace_run_id
    and a.organization_id=e.organization_id and a.artifact_type='tool_result' and a.version::text=ref->>'version' and a.content_hash=ref->>'fragmentHash'
    and ref->>'locator'='redacted_payload'
    and a.redacted_payload->'execution'->>'invocationId'=e.invocation_id::text
    and a.redacted_payload->'execution'->>'toolCallId'=e.call_id
    and (a.redacted_payload->'execution'->>'batchId')::integer=e.batch_id
    and (a.redacted_payload->'execution'->>'index')::integer=e.call_index) into valid;
  end if;
  if not coalesce(valid,false) then raise exception 'invalid result reference' using errcode='42501'; end if;
  -- Knowledge is checked as the original user, never using Worker visibility.
  perform set_config('app.actor_id',e.requested_by::text,true);
  for source in select value from jsonb_array_elements(coalesce(input_result->'sourceCitations','[]'::jsonb) || coalesce((select jsonb_agg(x->'citation') from jsonb_array_elements(coalesce(input_result->'hits','[]'::jsonb)) x),'[]'::jsonb)) loop
   if source->>'sourceKind'='knowledge_chunk' and not app.knowledge_citation_is_valid(
    (source->>'version')::uuid,source->>'locator',source->>'fragmentHash') then
     raise exception 'permission_revoked' using errcode='42501'; end if;
  end loop;
  perform set_config('app.actor_id',worker::text,true);
 end if;
 update app.tool_execution set state=input_state,error_code=input_error,
 started_at=case when input_state='running' then clock_timestamp() else started_at end,
 finished_at=case when input_state<>'running' then clock_timestamp() else null end,
 result_kind=ref->>'sourceKind',result_id=(ref->>'sourceId')::uuid,result_version=ref->>'version',result_hash=ref->>'fragmentHash'
 where id=input_id;
end $$;

create or replace function app.tool_reap() returns integer language plpgsql security definer set search_path=app,pg_temp as $$
declare n integer;
begin
 if not app.is_runtime_worker() then raise exception 'worker required' using errcode='42501'; end if;
 update app.tool_execution e set state='abandoned',error_code='lease_lost',finished_at=clock_timestamp()
 from app.runtime_job j where e.job_id=j.id and e.organization_id=app.current_organization_id()
 and e.state in ('prepared','running') and (j.attempts<>e.generation or j.status<>'leased' or j.lease_expires_at<=clock_timestamp());
 get diagnostics n=row_count; return n;
end $$;
alter table app.tool_execution enable row level security;
alter table app.tool_execution force row level security;
revoke all on app.tool_execution from public;
grant select on app.tool_execution to supplyagent_api;
drop policy if exists tool_execution_read on app.tool_execution;
create policy tool_execution_read on app.tool_execution for select to supplyagent_api using(
 organization_id=app.current_organization_id() and (app.is_runtime_worker() or
 (requested_by=app.current_actor_id() and app.tool_user_access(job_id,requested_by))));
revoke all on function app.tool_guard(uuid,text,integer),app.tool_user_access(uuid,uuid),
 app.tool_begin(uuid,text,integer,jsonb,jsonb),app.tool_finish(uuid,text,text,text,jsonb),app.tool_reap() from public;
grant execute on function app.tool_guard(uuid,text,integer),app.tool_user_access(uuid,uuid),
 app.tool_begin(uuid,text,integer,jsonb,jsonb),app.tool_finish(uuid,text,text,text,jsonb),app.tool_reap() to supplyagent_api;
commit;
