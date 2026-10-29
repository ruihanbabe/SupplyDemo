-- F46: additive fields for the accepted synthetic business story. No F41 dependency.
begin;
alter table app.crm_opportunity add column if not exists owner_id uuid;
alter table app.crm_task add column if not exists assignee_id uuid;
alter table app.crm_task add column if not exists due_at timestamptz;
alter table app.intelligence_request add column if not exists as_of timestamptz;
do $$ begin
 if not exists(select 1 from pg_constraint where conname='crm_opportunity_owner_member' and conrelid='app.crm_opportunity'::regclass) then
  alter table app.crm_opportunity add constraint crm_opportunity_owner_member
   foreign key(organization_id,owner_id) references app.organization_member(organization_id,user_id) on delete restrict;
 end if;
 if not exists(select 1 from pg_constraint where conname='crm_task_assignee_member' and conrelid='app.crm_task'::regclass) then
  alter table app.crm_task add constraint crm_task_assignee_member
   foreign key(organization_id,assignee_id) references app.organization_member(organization_id,user_id) on delete restrict;
 end if;
end $$;
create index if not exists crm_task_followup_idx on app.crm_task(organization_id,opportunity_id,due_at) where status='open';
create or replace function app.begin_business_intelligence_run(
  input_opportunity_id uuid, input_question text, input_key text, input_model_policy_id uuid, input_as_of timestamptz
) returns table(mission_id uuid, run_id uuid, job_id uuid)
language plpgsql security definer set search_path = app, pg_temp as $$
declare m app.runtime_mission%rowtype; declare r app.runtime_run%rowtype;
declare trace_id uuid; declare created_job_id uuid;
declare linked_case_id uuid; declare linked_part text; declare linked_parts jsonb; declare desired_context jsonb;
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
    select jsonb_agg(p.value order by p.value) into linked_parts
      from (select distinct value from app.supply_case c,
        jsonb_array_elements(c.scope->'partNumbers') as part(value) where c.id=linked_case_id) p;
  end if;
  if input_model_policy_id is not null and not exists (
    select 1 from app.model_policy p where p.id = input_model_policy_id
      and p.organization_id = app.current_organization_id() and p.active
  ) then raise exception 'model policy outside organization' using errcode = '42501'; end if;
  desired_context := jsonb_build_object(
    'opportunityId', input_opportunity_id, 'caseId', linked_case_id,
    'partNumber', linked_part, 'modelPolicyId', input_model_policy_id, 'businessNodes', true, 'partNumbers', linked_parts
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
    if not exists(select 1 from app.intelligence_request q where q.mission_id=m.id and q.requested_by=app.current_actor_id() and q.question=input_question and q.as_of is not distinct from input_as_of) then
      raise exception 'idempotency input mismatch' using errcode='22023'; end if;
    select id into created_job_id from app.runtime_job where app.runtime_job.run_id=r.id;
    return query select m.id,r.id,created_job_id; return;
  end if;
  insert into app.intelligence_request(mission_id,organization_id,requested_by,opportunity_id,question,as_of)
    values(m.id,m.organization_id,app.current_actor_id(),input_opportunity_id,input_question,input_as_of);
  trace_id := app.begin_trace_run('worker',linked_case_id);
  insert into app.runtime_run(organization_id,mission_id,trace_run_id)
    values(m.organization_id,m.id,trace_id) returning * into r;
  insert into app.runtime_job(organization_id,run_id,idempotency_key,kind)
    values(m.organization_id,r.id,input_key,'opportunity_intelligence') returning id into created_job_id;
  return query select m.id,r.id,created_job_id;
end;
$$;

revoke all on function app.begin_business_intelligence_run(uuid,text,text,uuid,timestamptz) from public;
grant execute on function app.begin_business_intelligence_run(uuid,text,text,uuid,timestamptz) to supplyagent_api;
commit;
