-- F36: durable opportunity Mission with CRM/supply scope resolved only by PostgreSQL.
begin;

create or replace function app.begin_opportunity_runtime_run(
  input_opportunity_id uuid, input_key text, input_model_policy_id uuid default null
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
  if input_key is null or char_length(input_key) not between 1 and 120 then
    raise exception 'invalid run input' using errcode = '22023';
  end if;
  select o.supply_case_id into linked_case_id from app.crm_opportunity o
  where o.id = input_opportunity_id and o.organization_id = app.current_organization_id();
  if not found then raise exception 'opportunity outside scope' using errcode = '42501'; end if;
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
  values(app.current_organization_id(),'opportunity_next_action',app.current_actor_id(),input_key,desired_context)
  on conflict (organization_id,idempotency_key) where idempotency_key is not null
    do update set idempotency_key=excluded.idempotency_key returning * into m;
  if m.kind <> 'opportunity_next_action' or m.context_ref <> desired_context then
    raise exception 'idempotency key reused with different input' using errcode = '22023';
  end if;
  select * into r from app.runtime_run where app.runtime_run.mission_id=m.id;
  if found then
    select id into created_job_id from app.runtime_job where app.runtime_job.run_id=r.id;
    return query select m.id,r.id,created_job_id; return;
  end if;
  trace_id := app.begin_trace_run('worker',linked_case_id);
  insert into app.runtime_run(organization_id,mission_id,trace_run_id)
    values(m.organization_id,m.id,trace_id) returning * into r;
  insert into app.runtime_job(organization_id,run_id,idempotency_key,kind)
    values(m.organization_id,r.id,input_key,'opportunity_next_action') returning id into created_job_id;
  return query select m.id,r.id,created_job_id;
end;
$$;

revoke all on function app.begin_opportunity_runtime_run(uuid,text,uuid) from public;
grant execute on function app.begin_opportunity_runtime_run(uuid,text,uuid) to supplyagent_api;
commit;
