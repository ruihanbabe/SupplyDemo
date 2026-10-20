-- F35: durable supply-investigation job inputs and lease-generation fencing.
begin;

alter table app.runtime_mission add column if not exists idempotency_key text;
alter table app.runtime_mission add column if not exists context_ref jsonb not null default '{}'::jsonb;
create unique index if not exists runtime_mission_org_key_idx
  on app.runtime_mission(organization_id, idempotency_key)
  where idempotency_key is not null;
alter table app.runtime_run add column if not exists trace_run_id uuid
  references app.trace_run(id) on delete restrict;
create unique index if not exists runtime_run_trace_idx on app.runtime_run(trace_run_id);

create or replace function app.is_runtime_worker() returns boolean
language sql stable security definer set search_path = app, pg_temp as $$
  select exists (
    select 1 from app.organization_member m
    where m.organization_id = app.current_organization_id()
      and m.user_id = app.current_actor_id()
      and m.role = 'maintainer'
      and m.scope @> '{"runtimeWorker":true}'::jsonb
  );
$$;

create or replace function app.begin_supply_runtime_run(
  input_case_id uuid, input_part_number text, input_key text, input_model_policy_id uuid default null
) returns table(mission_id uuid, run_id uuid, job_id uuid)
language plpgsql security definer set search_path = app, pg_temp as $$
declare m app.runtime_mission%rowtype;
declare existing_run app.runtime_run%rowtype;
declare created_trace_id uuid;
declare created_job_id uuid;
declare desired_context jsonb;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null
     or not app.is_member(app.current_organization_id(), app.current_actor_id()) then
    raise exception 'missing organization membership' using errcode = '42501';
  end if;
  if input_part_number is null or char_length(input_part_number) not between 1 and 160
     or input_key is null or char_length(input_key) not between 1 and 120 then
    raise exception 'invalid run input' using errcode = '22023';
  end if;
  if not exists (
    select 1 from app.supply_case c where c.id = input_case_id
      and c.organization_id = app.current_organization_id()
      and c.scope->'partNumbers' ? input_part_number
  ) then
    raise exception 'case or part outside organization scope' using errcode = '42501';
  end if;
  if input_model_policy_id is not null and not exists (
    select 1 from app.model_policy p where p.id = input_model_policy_id
      and p.organization_id = app.current_organization_id() and p.active
  ) then
    raise exception 'model policy outside organization' using errcode = '42501';
  end if;
  desired_context := jsonb_build_object(
    'caseId', input_case_id, 'partNumber', input_part_number,
    'modelPolicyId', input_model_policy_id
  );
  insert into app.runtime_mission(
    organization_id, kind, created_by, idempotency_key, context_ref
  ) values (
    app.current_organization_id(), 'supply_impact', app.current_actor_id(),
    input_key, desired_context
  ) on conflict (organization_id, idempotency_key) where idempotency_key is not null
    do update set idempotency_key = excluded.idempotency_key
  returning * into m;
  if m.context_ref <> desired_context then
    raise exception 'idempotency key reused with different input' using errcode = '22023';
  end if;
  select r.* into existing_run from app.runtime_run r where r.mission_id = m.id;
  if found then
    select j.id into created_job_id from app.runtime_job j where j.run_id = existing_run.id;
    return query select m.id, existing_run.id, created_job_id;
    return;
  end if;
  created_trace_id := app.begin_trace_run('worker', input_case_id);
  insert into app.runtime_run(organization_id, mission_id, trace_run_id)
    values (m.organization_id, m.id, created_trace_id) returning * into existing_run;
  insert into app.runtime_job(organization_id, run_id, idempotency_key, kind)
    values (m.organization_id, existing_run.id, input_key, 'agent_turn')
    returning id into created_job_id;
  return query select m.id, existing_run.id, created_job_id;
end;
$$;

create or replace function app.claim_runtime_job(input_owner text, input_lease_seconds integer default 30)
returns table(id uuid, run_id uuid, kind text, attempts integer)
language plpgsql security definer set search_path = app, pg_temp as $$
declare claimed app.runtime_job%rowtype;
begin
  if not app.is_runtime_worker()
     or input_owner is null or char_length(input_owner) not between 1 and 120
     or input_lease_seconds not between 1 and 3600 then
    raise exception 'invalid worker claim' using errcode = '42501';
  end if;
  select * into claimed from app.runtime_job j
    where j.organization_id = app.current_organization_id()
      and j.status = 'queued' and j.visible_at <= clock_timestamp()
    order by j.priority desc, j.created_at, j.id
    for update skip locked limit 1;
  if not found then return; end if;
  update app.runtime_job j set status = 'leased', lease_owner = input_owner,
    lease_expires_at = clock_timestamp() + make_interval(secs => input_lease_seconds),
    attempts = j.attempts + 1, updated_at = clock_timestamp()
    where j.id = claimed.id returning * into claimed;
  update app.runtime_run set status = 'running', updated_at = clock_timestamp()
    where app.runtime_run.id = claimed.run_id and status in ('queued', 'running');
  return query select claimed.id, claimed.run_id, claimed.kind, claimed.attempts;
end;
$$;

drop function if exists app.heartbeat_runtime_job(uuid, text, integer);
create or replace function app.heartbeat_runtime_job(
  input_id uuid, input_owner text, input_generation integer, input_lease_seconds integer
) returns boolean language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if not app.is_runtime_worker() then
    raise exception 'runtime worker identity required' using errcode = '42501';
  end if;
  if input_lease_seconds not between 1 and 3600 then
    raise exception 'invalid lease duration' using errcode = '22023';
  end if;
  update app.runtime_job set lease_expires_at = clock_timestamp() + make_interval(secs => input_lease_seconds),
    updated_at = clock_timestamp()
    where id = input_id and organization_id = app.current_organization_id()
      and status = 'leased' and lease_owner = input_owner
      and attempts = input_generation and lease_expires_at > clock_timestamp();
  return found;
end;
$$;

create or replace function app.complete_runtime_job(
  input_id uuid, input_owner text, input_generation integer
) returns boolean language plpgsql security definer set search_path = app, pg_temp as $$
declare finished_run_id uuid;
begin
  if not app.is_runtime_worker() then
    raise exception 'runtime worker identity required' using errcode = '42501';
  end if;
  update app.runtime_job set status = 'completed', lease_owner = null, lease_expires_at = null,
    updated_at = clock_timestamp()
    where id = input_id and organization_id = app.current_organization_id()
      and status = 'leased' and lease_owner = input_owner
      and attempts = input_generation and lease_expires_at > clock_timestamp()
    returning run_id into finished_run_id;
  if not found then return false; end if;
  update app.runtime_run set status = 'completed', updated_at = clock_timestamp()
    where app.runtime_run.id = finished_run_id;
  return true;
end;
$$;

create or replace function app.fail_runtime_job(
  input_id uuid, input_owner text, input_generation integer,
  input_code text, input_max_attempts integer, input_retry_seconds integer
) returns boolean language plpgsql security definer set search_path = app, pg_temp as $$
declare failed_run_id uuid;
declare next_status text;
begin
  if not app.is_runtime_worker() then
    raise exception 'runtime worker identity required' using errcode = '42501';
  end if;
  if input_code not in ('model_failed','tool_failed','sidecar_exited','timeout','protocol_failed','cancelled')
     or input_max_attempts not between 1 and 20
     or input_retry_seconds not between 0 and 86400 then
    raise exception 'invalid worker failure policy' using errcode = '22023';
  end if;
  update app.runtime_job j set
    status = case when input_code = 'cancelled' then 'cancelled'
      when j.attempts >= input_max_attempts then 'failed' else 'queued' end,
    lease_owner = null, lease_expires_at = null,
    visible_at = case when input_code = 'cancelled' or j.attempts >= input_max_attempts
      then j.visible_at else clock_timestamp() + make_interval(secs => input_retry_seconds) end,
    last_error = input_code, updated_at = clock_timestamp()
    where j.id = input_id and j.organization_id = app.current_organization_id()
      and j.status = 'leased' and j.lease_owner = input_owner
      and j.attempts = input_generation and j.lease_expires_at > clock_timestamp()
    returning j.run_id, j.status into failed_run_id, next_status;
  if not found then return false; end if;
  update app.runtime_run set status = case when next_status = 'queued' then 'queued'
    else next_status end, updated_at = clock_timestamp()
    where app.runtime_run.id = failed_run_id;
  return true;
end;
$$;

create or replace function app.reap_runtime_jobs() returns integer
language plpgsql security definer set search_path = app, pg_temp as $$
declare recovered_runs uuid[];
begin
  if not app.is_runtime_worker() then
    raise exception 'runtime worker identity required' using errcode = '42501';
  end if;
  with expired as (
    update app.runtime_job j set status = 'queued', lease_owner = null,
      lease_expires_at = null, visible_at = clock_timestamp(), updated_at = clock_timestamp()
      where j.organization_id = app.current_organization_id()
        and j.status = 'leased' and j.lease_expires_at <= clock_timestamp()
      returning j.run_id
  ) select array_agg(run_id) into recovered_runs from expired;
  if recovered_runs is null then return 0; end if;
  update app.runtime_run set status = 'queued', updated_at = clock_timestamp()
    where app.runtime_run.id = any(recovered_runs) and status = 'running';
  return cardinality(recovered_runs);
end;
$$;

create or replace function app.cancel_runtime_job(input_id uuid) returns boolean
language plpgsql security definer set search_path = app, pg_temp as $$
declare cancelled_run_id uuid;
begin
  update app.runtime_job set status = 'cancelled', lease_owner = null,
    lease_expires_at = null, updated_at = clock_timestamp()
    where id = input_id and organization_id = app.current_organization_id()
      and status in ('queued', 'leased') returning run_id into cancelled_run_id;
  if not found then return false; end if;
  update app.runtime_run set status = 'cancelled', updated_at = clock_timestamp()
    where app.runtime_run.id = cancelled_run_id;
  return true;
end;
$$;

revoke all on function app.begin_supply_runtime_run(uuid, text, text, uuid) from public;
revoke all on function app.is_runtime_worker() from public;
revoke all on function app.claim_runtime_job(text, integer) from public;
revoke all on function app.heartbeat_runtime_job(uuid, text, integer, integer) from public;
revoke all on function app.complete_runtime_job(uuid, text, integer) from public;
revoke all on function app.fail_runtime_job(uuid, text, integer, text, integer, integer) from public;
revoke all on function app.reap_runtime_jobs() from public;
revoke all on function app.cancel_runtime_job(uuid) from public;
grant execute on function app.begin_supply_runtime_run(uuid, text, text, uuid) to supplyagent_api;
grant execute on function app.is_runtime_worker() to supplyagent_api;
grant execute on function app.claim_runtime_job(text, integer) to supplyagent_api;
grant execute on function app.heartbeat_runtime_job(uuid, text, integer, integer) to supplyagent_api;
grant execute on function app.complete_runtime_job(uuid, text, integer) to supplyagent_api;
grant execute on function app.fail_runtime_job(uuid, text, integer, text, integer, integer) to supplyagent_api;
grant execute on function app.reap_runtime_jobs() to supplyagent_api;
grant execute on function app.cancel_runtime_job(uuid) to supplyagent_api;
commit;
