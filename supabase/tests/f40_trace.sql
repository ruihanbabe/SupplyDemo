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
  'impact_summary', 'PART-A shortage summary', '{"part":"PART-A"}'::jsonb,
  'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
) as trace_artifact_id \gset
select app.append_trace_event(
  :'trace_run_id'::uuid, null, 'supply.impact_calculated', 'impact calculated',
  '{"source":"deterministic"}'::jsonb, :'trace_artifact_id'::uuid
);
select app.append_trace_event(
  :'trace_run_id'::uuid, null, 'supply.case_observed', 'case observed', '{}'::jsonb, null
);
select case when (select count(*) from app.trace_event where run_id = :'trace_run_id'::uuid) = 2
                 and (select count(*) from app.trace_artifact where run_id = :'trace_run_id'::uuid) = 1
                 and app.verify_trace_chain(:'trace_run_id'::uuid)
            then 'ok: trace hash chain and artifacts recorded' else 'fail: trace chain' end as result;
select set_config('app.organization_id', '44444444-4444-4444-4444-444444444444', true);
select case when (select count(*) from app.trace_run) = 0
                 and (select count(*) from app.trace_event) = 0
                 and (select count(*) from app.trace_artifact) = 0
            then 'ok: trace cross-organization isolation' else 'fail: trace leakage' end as result;

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

rollback;
