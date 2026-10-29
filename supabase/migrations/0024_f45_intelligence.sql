-- F45 private question/context/observation records. No F41 dependency.
begin;
create table if not exists app.intelligence_request (
  mission_id uuid primary key references app.runtime_mission(id),
  organization_id uuid not null references app.organization(id),
  requested_by uuid not null,
  opportunity_id uuid not null references app.crm_opportunity(id),
  question text not null check (char_length(question) between 1 and 600),
  created_at timestamptz not null default clock_timestamp()
);
create table if not exists app.intelligence_attempt (
  id uuid primary key,
  organization_id uuid not null references app.organization(id),
  mission_id uuid not null references app.intelligence_request(mission_id),
  job_id uuid not null references app.runtime_job(id),
  generation integer not null check(generation>0),
  role text not null check(role in ('customer_facts','business_policy','supply_risk','opportunity_diagnosis')),
  input_context jsonb not null,
  output jsonb,
  status text not null check(status in ('running','completed','failed')),
  error_code text,
  created_at timestamptz not null default clock_timestamp(),
  finished_at timestamptz,
  unique(job_id,generation,role)
);
create table if not exists app.intelligence_observation (
  id uuid primary key,
  attempt_id uuid not null references app.intelligence_attempt(id),
  organization_id uuid not null references app.organization(id),
  mission_id uuid not null references app.intelligence_request(mission_id),
  tool_name text not null,
  arguments jsonb not null,
  payload jsonb not null,
  content_hash text not null,
  observed_at timestamptz not null default clock_timestamp()
);
create index if not exists intelligence_attempt_mission on app.intelligence_attempt(mission_id,generation);
create index if not exists intelligence_observation_attempt on app.intelligence_observation(attempt_id);

create or replace function app.can_read_intelligence(input_mission uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select exists (
  select 1 from app.intelligence_request q join app.crm_opportunity o on o.id=q.opportunity_id
  join app.crm_customer c on c.id=o.customer_id
  where q.mission_id=input_mission and q.organization_id=app.current_organization_id()
    and app.is_member(q.organization_id,app.current_actor_id())
    and (app.is_runtime_worker() or (q.requested_by=app.current_actor_id()
      and app.can_read_crm_scope(o.scope) and app.can_read_crm_scope(c.scope)))
 );
$$;
-- Like F31/F35, these scoped SECURITY DEFINER commands are installed by the
-- PostgreSQL migration owner; supplyagent_api has no table mutation privileges.
do $$ declare t text; begin
 foreach t in array array['intelligence_request','intelligence_attempt','intelligence_observation'] loop
  execute format('alter table app.%I enable row level security',t);
  execute format('alter table app.%I force row level security',t);
  execute format('drop policy if exists intelligence_read on app.%I',t);
  execute format('create policy intelligence_read on app.%I for select to supplyagent_api using(app.can_read_intelligence(mission_id))',t);
  execute format('revoke all on app.%I from public',t);
  execute format('grant select on app.%I to supplyagent_api',t);
 end loop;
end $$;

create or replace function app.intelligence_attempt_write(
 input_id uuid,input_job uuid,input_owner text,input_generation integer,input_role text,
 input_context jsonb,input_status text,input_output jsonb default null,input_error text default null
) returns void language plpgsql security definer set search_path=app,pg_temp as $$
declare j app.runtime_job%rowtype; declare m uuid;
begin
 select * into j from app.runtime_job where id=input_job and organization_id=app.current_organization_id() for update;
 if not app.is_runtime_worker() or j.id is null or j.status<>'leased'
  or j.lease_owner is distinct from input_owner or j.attempts<>input_generation
  or j.lease_expires_at<=clock_timestamp() then
  raise exception 'intelligence lease lost' using errcode='42501'; end if;
 select mission_id into m from app.runtime_run where id=j.run_id;
 if input_status='running' then
  update app.intelligence_attempt set status='failed',error_code='lease_superseded',finished_at=clock_timestamp()
    where job_id=j.id and generation<input_generation and status='running';
  insert into app.intelligence_attempt(id,organization_id,mission_id,job_id,generation,role,input_context,status)
   values(input_id,j.organization_id,m,j.id,input_generation,input_role,input_context,'running');
 else
  update app.intelligence_attempt set status=input_status,output=input_output,error_code=input_error,finished_at=clock_timestamp()
   where id=input_id and mission_id=m and job_id=j.id and generation=input_generation and status='running';
  if not found then raise exception 'attempt is not active' using errcode='22023'; end if;
 end if;
end $$;
create or replace function app.intelligence_observe(
 input_id uuid,input_attempt uuid,input_owner text,input_tool text,input_arguments jsonb,input_payload jsonb
) returns text language plpgsql security definer set search_path=app,pg_temp as $$
declare a app.intelligence_attempt%rowtype; declare j app.runtime_job%rowtype; declare h text;
begin
 select * into a from app.intelligence_attempt where id=input_attempt and organization_id=app.current_organization_id();
 select * into j from app.runtime_job where id=a.job_id for update;
 if not app.is_runtime_worker() or a.id is null or a.status<>'running' or j.status<>'leased'
  or j.lease_owner is distinct from input_owner or j.attempts<>a.generation
  or j.lease_expires_at<=clock_timestamp() then raise exception 'intelligence lease lost' using errcode='42501'; end if;
 h:=encode(public.digest(convert_to(input_payload::text,'UTF8'),'sha256'),'hex');
 insert into app.intelligence_observation(id,attempt_id,organization_id,mission_id,tool_name,arguments,payload,content_hash)
 values(input_id,a.id,a.organization_id,a.mission_id,input_tool,input_arguments,input_payload,h);
 return h;
end $$;

create or replace function app.begin_intelligence_run(
  input_opportunity_id uuid, input_question text, input_key text, input_model_policy_id uuid default null
) returns table(mission_id uuid, run_id uuid, job_id uuid)
language plpgsql security definer set search_path = app, pg_temp as $$
declare m app.runtime_mission%rowtype; declare r app.runtime_run%rowtype;
declare trace_id uuid; declare created_job_id uuid;
declare linked_case_id uuid; declare linked_part text; declare desired_context jsonb;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null
     or not app.is_member(app.current_organization_id(), app.current_actor_id()) then
    raise exception 'missing organization membership' using errcode = '42501';
  end if;
  if input_question is null or char_length(btrim(input_question)) not between 1 and 600 then
    raise exception 'invalid question' using errcode='22023'; end if;
  if input_key is null or char_length(input_key) not between 1 and 120 then
    raise exception 'invalid run input' using errcode = '22023';
  end if;
  select o.supply_case_id into linked_case_id from app.crm_opportunity o
  where o.id = input_opportunity_id and o.organization_id = app.current_organization_id();
  if not found then raise exception 'opportunity outside scope' using errcode = '42501'; end if;
  if not exists(select 1 from app.crm_opportunity o join app.crm_customer c on c.id=o.customer_id
    where o.id=input_opportunity_id and app.can_read_crm_scope(o.scope) and app.can_read_crm_scope(c.scope)) then
    raise exception 'opportunity outside scope' using errcode='42501'; end if;
  if linked_case_id is not null then
    select c.scope->'partNumbers'->>0 into linked_part from app.supply_case c
    where c.id = linked_case_id and c.organization_id = app.current_organization_id();
    if not found or linked_part is null then
      raise exception 'linked supply case has no part scope' using errcode = '42501';
    end if;
  end if;
  if input_model_policy_id is not null and not exists (
    select 1 from app.model_policy p where p.id = input_model_policy_id
      and p.organization_id = app.current_organization_id() and p.active
  ) then raise exception 'model policy outside organization' using errcode = '42501'; end if;
  desired_context := jsonb_build_object(
    'opportunityId', input_opportunity_id, 'caseId', linked_case_id,
    'partNumber', linked_part, 'modelPolicyId', input_model_policy_id
  );
  insert into app.runtime_mission(organization_id,kind,created_by,idempotency_key,context_ref)
  values(app.current_organization_id(),'opportunity_intelligence',app.current_actor_id(),input_key,desired_context)
  on conflict (organization_id,idempotency_key) where idempotency_key is not null
    do update set idempotency_key=excluded.idempotency_key returning * into m;
  if m.kind <> 'opportunity_intelligence' or m.context_ref <> desired_context then
    raise exception 'idempotency key reused with different input' using errcode = '22023';
  end if;
  select * into r from app.runtime_run where app.runtime_run.mission_id=m.id;
  if found then
    if not exists(select 1 from app.intelligence_request q where q.mission_id=m.id and q.requested_by=app.current_actor_id() and q.question=input_question) then
      raise exception 'idempotency input mismatch' using errcode='22023'; end if;
    select id into created_job_id from app.runtime_job where app.runtime_job.run_id=r.id;
    return query select m.id,r.id,created_job_id; return;
  end if;
  insert into app.intelligence_request(mission_id,organization_id,requested_by,opportunity_id,question)
    values(m.id,m.organization_id,app.current_actor_id(),input_opportunity_id,input_question);
  trace_id := app.begin_trace_run('worker',linked_case_id);
  insert into app.runtime_run(organization_id,mission_id,trace_run_id)
    values(m.organization_id,m.id,trace_id) returning * into r;
  insert into app.runtime_job(organization_id,run_id,idempotency_key,kind)
    values(m.organization_id,r.id,input_key,'opportunity_intelligence') returning id into created_job_id;
  return query select m.id,r.id,created_job_id;
end;
$$;


revoke all on function app.can_read_intelligence(uuid), app.begin_intelligence_run(uuid,text,text,uuid), app.intelligence_attempt_write(uuid,uuid,text,integer,text,jsonb,text,jsonb,text), app.intelligence_observe(uuid,uuid,text,text,jsonb,jsonb) from public;
grant execute on function app.can_read_intelligence(uuid), app.begin_intelligence_run(uuid,text,text,uuid), app.intelligence_attempt_write(uuid,uuid,text,integer,text,jsonb,text,jsonb,text), app.intelligence_observe(uuid,uuid,text,text,jsonb,jsonb) to supplyagent_api;
commit;
