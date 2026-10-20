-- F40：组织级 SQL Trace。旧 Python SessionStore 只作历史/Eval 参考，不迁入产品路径。
begin;

create table if not exists app.trace_run (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  case_id uuid references app.supply_case(id) on delete restrict,
  run_kind text not null check (run_kind in ('manual', 'api', 'worker', 'replay')),
  actor_id uuid not null,
  created_at timestamptz not null default now(),
  -- 业务需要可变更
  next_event_sequence bigint not null default 1 check (next_event_sequence > 0),
  last_event_hash text check (last_event_hash is null or last_event_hash ~ '^[0-9a-f]{64}$')
);

create table if not exists app.trace_artifact (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  run_id uuid not null references app.trace_run(id) on delete restrict,
  case_id uuid references app.supply_case(id) on delete restrict,
  artifact_key uuid not null,
  version integer not null check (version > 0),
  artifact_type text not null check (char_length(artifact_type) between 1 and 96),
  -- 脱敏摘要, 不携带客户信息、密钥
  redacted_summary text not null check (char_length(redacted_summary) between 1 and 2000),
  redacted_payload jsonb not null default '{}'::jsonb check (jsonb_typeof(redacted_payload) = 'object'),
  content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  actor_id uuid not null,
  created_at timestamptz not null default now(),
  unique (organization_id, artifact_key, version)
);
-- 事件流水, 必要时刻hash全部流水做验证
create table if not exists app.trace_event (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  run_id uuid not null references app.trace_run(id) on delete restrict,
  case_id uuid references app.supply_case(id) on delete restrict,
  actor_id uuid not null,
  sequence_no bigint not null check (sequence_no > 0),
  event_type text not null check (char_length(event_type) between 1 and 96),
  redacted_summary text not null check (char_length(redacted_summary) between 1 and 2000),
  metadata jsonb not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object'),
  artifact_id uuid references app.trace_artifact(id) on delete restrict,
  prev_hash text check (prev_hash is null or prev_hash ~ '^[0-9a-f]{64}$'),
  event_hash text not null check (event_hash ~ '^[0-9a-f]{64}$'),
  occurred_at timestamptz not null default now(),
  unique (organization_id, run_id, sequence_no)
);

create index if not exists trace_run_organization_created_idx
  on app.trace_run (organization_id, created_at desc);
create index if not exists trace_artifact_organization_run_idx
  on app.trace_artifact (organization_id, run_id, created_at);
create index if not exists trace_event_organization_run_sequence_idx
  on app.trace_event (organization_id, run_id, sequence_no);

create or replace function app.trace_prevent_mutation()
returns trigger language plpgsql as $$
begin
  raise exception '% is append-only', tg_table_name using errcode = '55000';
end;
$$;

drop trigger if exists trace_artifact_append_only on app.trace_artifact;
create trigger trace_artifact_append_only before update or delete on app.trace_artifact
  for each row execute function app.trace_prevent_mutation();
drop trigger if exists trace_event_append_only on app.trace_event;
create trigger trace_event_append_only before update or delete on app.trace_event
  for each row execute function app.trace_prevent_mutation();

-- Trace Run 的可变游标只由这两个受控函数推进；事实事件和 Artifact 没有写权限。
create or replace function app.begin_trace_run(
  input_kind text,
  input_case_id uuid default null
)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare trace_id uuid;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null then
    raise exception 'missing request context' using errcode = '42501';
  end if;
  if input_kind not in ('manual', 'api', 'worker', 'replay') then
    raise exception 'invalid trace run kind' using errcode = '22023';
  end if;
  if input_case_id is not null and not exists (
    select 1 from app.supply_case
    where id = input_case_id and organization_id = app.current_organization_id()
  ) then
    raise exception 'case is outside organization context' using errcode = '42501';
  end if;
  insert into app.trace_run (organization_id, case_id, run_kind, actor_id)
  values (app.current_organization_id(), input_case_id, input_kind, app.current_actor_id())
  returning id into trace_id;
  return trace_id;
end;
$$;

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
begin
  if app.current_actor_id() is null or app.current_organization_id() is null then
    raise exception 'missing request context' using errcode = '42501';
  end if;

  if not exists (
    select 1 from app.trace_run where id = input_run_id
      and organization_id = app.current_organization_id()
  ) then
    raise exception 'run is outside organization context' using errcode = '42501';
  end if;

  if input_case_id is not null and not exists (
    select 1 from app.supply_case where id = input_case_id
      and organization_id = app.current_organization_id()
  ) then
    raise exception 'case is outside organization context' using errcode = '42501';
  end if;

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
  if input_case_id is not null and not exists (
    select 1 from app.supply_case where id = input_case_id
      and organization_id = app.current_organization_id()
  ) then
    raise exception 'case is outside organization context' using errcode = '42501';
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
declare expected_previous text := null;
declare canonical_payload jsonb;
declare expected_hash text;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null then
    raise exception 'missing request context' using errcode = '42501';
  end if;
  if not exists (select 1 from app.trace_run where id = input_run_id
                 and organization_id = app.current_organization_id()) then
    raise exception 'run is outside organization context' using errcode = '42501';
  end if;
  for current_event in select * from app.trace_event where run_id = input_run_id
    and organization_id = app.current_organization_id() order by sequence_no
  loop
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
  end loop;
  return true;
end;
$$;

alter table app.trace_run enable row level security;
alter table app.trace_run force row level security;
alter table app.trace_artifact enable row level security;
alter table app.trace_artifact force row level security;
alter table app.trace_event enable row level security;
alter table app.trace_event force row level security;

drop policy if exists trace_run_isolation on app.trace_run;
create policy trace_run_isolation on app.trace_run for select to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists trace_run_service_insert on app.trace_run;
create policy trace_run_service_insert on app.trace_run for insert to public
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists trace_run_service_update on app.trace_run;
create policy trace_run_service_update on app.trace_run for update to public
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()))
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists trace_artifact_isolation on app.trace_artifact;
create policy trace_artifact_isolation on app.trace_artifact for select to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists trace_artifact_service_insert on app.trace_artifact;
create policy trace_artifact_service_insert on app.trace_artifact for insert to public
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists trace_event_isolation on app.trace_event;
create policy trace_event_isolation on app.trace_event for select to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists trace_event_service_insert on app.trace_event;
create policy trace_event_service_insert on app.trace_event for insert to public
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));

revoke all on app.trace_run, app.trace_artifact, app.trace_event from public;
grant select on app.trace_run, app.trace_artifact, app.trace_event to supplyagent_api;
grant execute on function app.begin_trace_run(text, uuid) to supplyagent_api;
grant execute on function app.append_trace_artifact(uuid, uuid, uuid, text, text, jsonb, text) to supplyagent_api;
grant execute on function app.append_trace_event(uuid, uuid, text, text, jsonb, uuid) to supplyagent_api;
grant execute on function app.verify_trace_chain(uuid) to supplyagent_api;

commit;
