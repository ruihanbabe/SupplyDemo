-- F40：Trace 只追加、哈希链和组织隔离。fixture 一律在事务末尾回滚。
\set ON_ERROR_STOP on
begin;

insert into app.organization (id, slug, display_name) values
  ('33333333-3333-3333-3333-333333333333', 'trace-alpha', 'Trace Alpha'),
  ('44444444-4444-4444-4444-444444444444', 'trace-bravo', 'Trace Bravo')
on conflict (id) do nothing;
insert into app.organization_member (organization_id, user_id, role) values
  ('33333333-3333-3333-3333-333333333333', 'cccccccc-cccc-cccc-cccc-cccccccccccc', 'buyer'),
  ('44444444-4444-4444-4444-444444444444', 'dddddddd-dddd-dddd-dddd-dddddddddddd', 'auditor')
on conflict do nothing;

set local role supplyagent_api;
select app.set_request_context('cccccccc-cccc-cccc-cccc-cccccccccccc', '33333333-3333-3333-3333-333333333333');
select app.begin_trace_run('api') as trace_run_id \gset
select app.append_trace_artifact(
  :'trace_run_id'::uuid, null, '55555555-5555-5555-5555-555555555555'::uuid,
  'impact_summary', 'PART-A shortage summary', '{"part":"PART-A"}'::jsonb
) as trace_artifact_id \gset
select set_config('app.trace_test_artifact_id', :'trace_artifact_id', true);
select app.append_trace_event(
  :'trace_run_id'::uuid, null, 'supply.impact_calculated', 'impact calculated',
  '{"source":"deterministic"}'::jsonb, :'trace_artifact_id'::uuid
);
select app.append_trace_event(
  :'trace_run_id'::uuid, null, 'supply.case_observed', 'case observed', '{}'::jsonb, null
);
select set_config('app.trace_test_run_id', :'trace_run_id', true);
select case when (select count(*) from app.trace_event where run_id = :'trace_run_id'::uuid) = 2
                 and (select count(*) from app.trace_artifact where run_id = :'trace_run_id'::uuid) = 1
                 and app.verify_trace_chain(:'trace_run_id'::uuid)
            then 'ok: trace hash chain and artifacts recorded' else 'fail: trace chain' end as result;
do $$
declare trace_id uuid := current_setting('app.trace_test_run_id')::uuid;
begin
  if (select count(*) from app.trace_event where run_id = trace_id) <> 2
     or (select count(*) from app.trace_artifact where run_id = trace_id) <> 1
     or not app.verify_trace_chain(trace_id) then
    raise exception 'trace hash chain or artifact assertion failed';
  end if;
end;
$$;

-- Artifact 指纹必须由 SQL 覆盖 type、摘要和 payload；应用不能自行声明 hash。
do $$
declare trace_id uuid := current_setting('app.trace_test_run_id')::uuid;
declare changed_type_id uuid;
declare changed_summary_id uuid;
declare changed_payload_id uuid;
declare base_hash text;
declare changed_type_hash text;
declare changed_summary_hash text;
declare changed_payload_hash text;
begin
  select content_hash into base_hash from app.trace_artifact where id = current_setting('app.trace_test_artifact_id')::uuid;
  if base_hash <> encode(public.digest(convert_to(jsonb_build_object(
    'artifact_type', 'impact_summary',
    'redacted_summary', 'PART-A shortage summary',
    'redacted_payload', '{"part":"PART-A"}'::jsonb
  )::text, 'UTF8'), 'sha256'), 'hex') then
    raise exception 'artifact content hash does not use the documented SQL canonical form';
  end if;
  select app.append_trace_artifact(trace_id, null, gen_random_uuid(), 'changed_type', 'PART-A shortage summary', '{"part":"PART-A"}'::jsonb)
    into changed_type_id;
  select content_hash into changed_type_hash from app.trace_artifact where id = changed_type_id;
  select app.append_trace_artifact(trace_id, null, gen_random_uuid(), 'impact_summary', 'changed summary', '{"part":"PART-A"}'::jsonb)
    into changed_summary_id;
  select content_hash into changed_summary_hash from app.trace_artifact where id = changed_summary_id;
  select app.append_trace_artifact(trace_id, null, gen_random_uuid(), 'impact_summary', 'PART-A shortage summary', '{"part":"PART-B"}'::jsonb)
    into changed_payload_id;
  select content_hash into changed_payload_hash from app.trace_artifact where id = changed_payload_id;
  if base_hash = changed_type_hash or base_hash = changed_summary_hash or base_hash = changed_payload_hash then
    raise exception 'artifact content hash omitted a protected field';
  end if;
end;
$$;
select 'ok: artifact content hash covers type summary and payload' as result;

-- 即使未来误授 UPDATE，普通 API 角色也不能通过 RLS 或 trigger 改写 Run 指针。
reset role;
grant update on app.trace_run to supplyagent_api;
set local role supplyagent_api;
do $$
begin
  begin
    update app.trace_run set next_event_sequence = 999, last_event_hash = null
    where id = current_setting('app.trace_test_run_id')::uuid;
    if found then
      raise exception 'trace cursor mutation unexpectedly succeeded';
    end if;
  exception when insufficient_privilege or sqlstate '55000' then
    null;
  end;
end;
$$;
reset role;
do $$
begin
  begin
    update app.trace_run set next_event_sequence = 999, last_event_hash = null
    where id = current_setting('app.trace_test_run_id')::uuid;
    raise exception 'trace cursor trigger unexpectedly succeeded';
  exception when sqlstate '55000' then
    null;
  end;
end;
$$;
set local role supplyagent_api;
select case when app.verify_trace_chain(:'trace_run_id'::uuid)
            then 'ok: cursor RLS and trigger rejected mutation' else 'fail: valid cursor rejected' end as result;

-- 模拟历史库被拥有者越过 trigger 损坏：验证函数必须发现尾指针/序号不闭环。
reset role;
alter table app.trace_run disable trigger trace_run_cursor_consistency;
update app.trace_run set next_event_sequence = 999 where id = :'trace_run_id'::uuid;
alter table app.trace_run enable trigger trace_run_cursor_consistency;
set local role supplyagent_api;
select case when not app.verify_trace_chain(:'trace_run_id'::uuid)
            then 'ok: terminal cursor inconsistency detected' else 'fail: terminal cursor accepted' end as result;
reset role;
select set_config('app.organization_id', '44444444-4444-4444-4444-444444444444', true);
set local role supplyagent_api;
select case when (select count(*) from app.trace_run) = 0
                 and (select count(*) from app.trace_event) = 0
                 and (select count(*) from app.trace_artifact) = 0
            then 'ok: trace cross-organization isolation' else 'fail: trace leakage' end as result;
do $$
begin
  if exists (select 1 from app.trace_run)
     or exists (select 1 from app.trace_event)
     or exists (select 1 from app.trace_artifact) then
    raise exception 'trace cross-organization isolation failed';
  end if;
end;
$$;

reset role;
do $$
begin
  begin
    update app.trace_event set redacted_summary = 'tampered'
    where organization_id = '33333333-3333-3333-3333-333333333333'::uuid;
    raise exception 'trace event mutation unexpectedly succeeded';
  exception when sqlstate '55000' then
    null;
  end;
end;
$$;
select 'ok: trace event append-only trigger' as result;

do $$
begin
  if has_function_privilege('public', 'app.begin_trace_run(text,uuid)'::regprocedure, 'execute')
     or has_function_privilege('public', 'app.append_trace_artifact(uuid,uuid,uuid,text,text,jsonb)'::regprocedure, 'execute')
     or has_function_privilege('public', 'app.append_trace_event(uuid,uuid,text,text,jsonb,uuid)'::regprocedure, 'execute')
     or has_function_privilege('public', 'app.verify_trace_chain(uuid)'::regprocedure, 'execute') then
    raise exception 'trace security-definer function is executable by public';
  end if;
end;
$$;
select 'ok: trace functions are not executable by public' as result;

rollback;
