-- F40 Trace 加固：函数 ACL、Run 游标完整性、Artifact 并发版本与 Case 一致性。
-- 0004 已可能被部署；本迁移只做向前兼容的安全修复。
begin;

-- SECURITY DEFINER 函数的可调用者必须显式白名单，不能依赖 schema ACL 或默认权限。
revoke all on function app.begin_trace_run(text, uuid) from public;
revoke all on function app.append_trace_artifact(uuid, uuid, uuid, text, text, jsonb, text) from public;
revoke all on function app.append_trace_event(uuid, uuid, text, text, jsonb, uuid) from public;
revoke all on function app.verify_trace_chain(uuid) from public;
grant execute on function app.begin_trace_run(text, uuid) to supplyagent_api;
grant execute on function app.append_trace_artifact(uuid, uuid, uuid, text, text, jsonb, text) to supplyagent_api;
grant execute on function app.append_trace_event(uuid, uuid, text, text, jsonb, uuid) to supplyagent_api;
grant execute on function app.verify_trace_chain(uuid) to supplyagent_api;

-- SECURITY DEFINER 函数以迁移/函数 owner（本地为 postgres）执行；普通 API 角色即使误获表写权限也不满足 RLS。
drop policy if exists trace_run_service_insert on app.trace_run;
create policy trace_run_service_insert on app.trace_run for insert to postgres
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists trace_run_service_update on app.trace_run;
create policy trace_run_service_update on app.trace_run for update to postgres
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()))
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists trace_artifact_service_insert on app.trace_artifact;
create policy trace_artifact_service_insert on app.trace_artifact for insert to postgres
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists trace_event_service_insert on app.trace_event;
create policy trace_event_service_insert on app.trace_event for insert to postgres
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));

-- Run 指针是 Event 链的缓存，必须与现有不可变事件精确对应；直接 UPDATE 不能改写历史或制造跳号。
create or replace function app.trace_guard_run_cursor()
returns trigger language plpgsql set search_path = app, pg_temp as $$
declare last_sequence bigint;
declare terminal_hash text;
begin
  if new.id is distinct from old.id
     or new.organization_id is distinct from old.organization_id
     or new.case_id is distinct from old.case_id
     or new.run_kind is distinct from old.run_kind
     or new.actor_id is distinct from old.actor_id
     or new.created_at is distinct from old.created_at then
    raise exception 'trace run identity is immutable' using errcode = '55000';
  end if;

  select coalesce(max(sequence_no), 0),
         (array_agg(event_hash order by sequence_no desc))[1]
    into last_sequence, terminal_hash
  from app.trace_event
  where run_id = old.id and organization_id = old.organization_id;

  if new.next_event_sequence <> last_sequence + 1
     or new.last_event_hash is distinct from terminal_hash then
    raise exception 'trace run cursor must match terminal event' using errcode = '55000';
  end if;
  return new;
end;
$$;

drop trigger if exists trace_run_cursor_consistency on app.trace_run;
create trigger trace_run_cursor_consistency before update on app.trace_run
  for each row execute function app.trace_guard_run_cursor();

create or replace function app.append_trace_artifact(
  input_run_id uuid,
  input_case_id uuid,
  input_artifact_key uuid,
  input_artifact_type text,
  input_redacted_summary text,
  input_redacted_payload jsonb,
  input_content_hash text
)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare artifact_id uuid;
declare next_version integer;
declare current_run app.trace_run%rowtype;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null then
    raise exception 'missing request context' using errcode = '42501';
  end if;
  select * into current_run from app.trace_run
  where id = input_run_id and organization_id = app.current_organization_id()
  for key share;
  if not found then
    raise exception 'run is outside organization context' using errcode = '42501';
  end if;
  if input_case_id is distinct from current_run.case_id then
    raise exception 'case must match trace run' using errcode = '42501';
  end if;

  -- 版本是每个组织/Artifact 逻辑键的线性历史；首次写入没有可 FOR UPDATE 的父行，因此锁该逻辑键。
  perform pg_advisory_xact_lock(hashtextextended(
    app.current_organization_id()::text || ':' || input_artifact_key::text, 0
  ));
  select coalesce(max(version), 0) + 1 into next_version
  from app.trace_artifact
  where organization_id = app.current_organization_id() and artifact_key = input_artifact_key;
  insert into app.trace_artifact (
    organization_id, run_id, case_id, artifact_key, version, artifact_type,
    redacted_summary, redacted_payload, content_hash, actor_id
  ) values (
    app.current_organization_id(), input_run_id, input_case_id, input_artifact_key, next_version,
    input_artifact_type, input_redacted_summary, coalesce(input_redacted_payload, '{}'::jsonb),
    input_content_hash, app.current_actor_id()
  ) returning id into artifact_id;
  return artifact_id;
end;
$$;

create or replace function app.append_trace_event(
  input_run_id uuid,
  input_case_id uuid,
  input_event_type text,
  input_redacted_summary text,
  input_metadata jsonb default '{}'::jsonb,
  input_artifact_id uuid default null
)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare event_id uuid;
declare current_run app.trace_run%rowtype;
declare canonical_payload jsonb;
declare calculated_hash text;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null then
    raise exception 'missing request context' using errcode = '42501';
  end if;
  select * into current_run from app.trace_run
  where id = input_run_id and organization_id = app.current_organization_id()
  for update;
  if not found then
    raise exception 'run is outside organization context' using errcode = '42501';
  end if;
  if input_case_id is distinct from current_run.case_id then
    raise exception 'case must match trace run' using errcode = '42501';
  end if;
  if input_artifact_id is not null and not exists (
    select 1 from app.trace_artifact where id = input_artifact_id and run_id = input_run_id
      and organization_id = app.current_organization_id()
  ) then
    raise exception 'artifact is outside run context' using errcode = '42501';
  end if;
  canonical_payload := jsonb_build_object(
    'actor_id', app.current_actor_id(),
    'artifact_id', input_artifact_id,
    'case_id', input_case_id,
    'event_type', input_event_type,
    'metadata', coalesce(input_metadata, '{}'::jsonb),
    'redacted_summary', input_redacted_summary,
    'run_id', input_run_id,
    'sequence_no', current_run.next_event_sequence
  );
  calculated_hash := encode(public.digest(coalesce(current_run.last_event_hash, '') || canonical_payload::text, 'sha256'), 'hex');
  insert into app.trace_event (
    organization_id, run_id, case_id, actor_id, sequence_no, event_type, redacted_summary,
    metadata, artifact_id, prev_hash, event_hash
  ) values (
    app.current_organization_id(), input_run_id, input_case_id, app.current_actor_id(),
    current_run.next_event_sequence, input_event_type, input_redacted_summary,
    coalesce(input_metadata, '{}'::jsonb), input_artifact_id, current_run.last_event_hash, calculated_hash
  ) returning id into event_id;
  update app.trace_run set next_event_sequence = current_run.next_event_sequence + 1,
    last_event_hash = calculated_hash where id = input_run_id;
  return event_id;
end;
$$;

create or replace function app.verify_trace_chain(input_run_id uuid)
returns boolean language plpgsql security definer set search_path = app, pg_temp as $$
declare current_event app.trace_event%rowtype;
declare current_run app.trace_run%rowtype;
declare expected_previous text := null;
declare expected_hash text;
declare expected_sequence bigint := 1;
declare canonical_payload jsonb;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null then
    raise exception 'missing request context' using errcode = '42501';
  end if;
  select * into current_run from app.trace_run where id = input_run_id
    and organization_id = app.current_organization_id();
  if not found then
    raise exception 'run is outside organization context' using errcode = '42501';
  end if;
  for current_event in select * from app.trace_event where run_id = input_run_id
    and organization_id = app.current_organization_id() order by sequence_no
  loop
    if current_event.sequence_no <> expected_sequence then
      return false;
    end if;
    canonical_payload := jsonb_build_object(
      'actor_id', current_event.actor_id,
      'artifact_id', current_event.artifact_id,
      'case_id', current_event.case_id,
      'event_type', current_event.event_type,
      'metadata', current_event.metadata,
      'redacted_summary', current_event.redacted_summary,
      'run_id', current_event.run_id,
      'sequence_no', current_event.sequence_no
    );
    expected_hash := encode(public.digest(coalesce(expected_previous, '') || canonical_payload::text, 'sha256'), 'hex');
    if current_event.prev_hash is distinct from expected_previous
       or current_event.event_hash is distinct from expected_hash then
      return false;
    end if;
    expected_previous := current_event.event_hash;
    expected_sequence := expected_sequence + 1;
  end loop;
  return current_run.next_event_sequence = expected_sequence
     and current_run.last_event_hash is not distinct from expected_previous;
end;
$$;

comment on trigger trace_run_cursor_consistency on app.trace_run is
  'Single-run Trace is intentionally serialized: cursor must equal the immutable event chain terminal state.';

commit;
