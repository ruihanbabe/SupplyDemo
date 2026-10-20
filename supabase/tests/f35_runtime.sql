\set ON_ERROR_STOP on
begin;
insert into app.organization(id,slug,display_name) values ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','runtime-alpha','Runtime Alpha') on conflict do nothing;
insert into app.organization_member(organization_id,user_id,role) values ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','cccccccc-cccc-4ccc-8ccc-cccccccccccc','organization_admin') on conflict do nothing;
insert into app.organization_member(organization_id,user_id,role,scope) values ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','dddddddd-dddd-4ddd-8ddd-dddddddddddd','maintainer','{"runtimeWorker":true}') on conflict do nothing;
insert into app.supply_case(organization_id,title,status,severity,scope)
values ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','F35 case','open','high','{"partNumbers":["PART-A"]}'::jsonb)
returning id as case_id \gset
insert into app.runtime_mission(organization_id,kind,created_by) values ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','test','cccccccc-cccc-4ccc-8ccc-cccccccccccc') returning id as mission_id \gset
insert into app.runtime_run(organization_id,mission_id) values ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',:'mission_id'::uuid) returning id as run_id \gset
set local role supplyagent_api; select app.set_request_context('cccccccc-cccc-4ccc-8ccc-cccccccccccc','bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb');
select app.enqueue_runtime_job(:'run_id'::uuid,'idempotent-1','agent_turn',1) as job_id \gset
select app.enqueue_runtime_job(:'run_id'::uuid,'idempotent-1','agent_turn',1);
select case when (select count(*) from app.runtime_job)=1 then 'ok: enqueue idempotent' else 'fail: duplicate job' end as result;
do $$ begin
  begin
    perform * from app.claim_runtime_job('unauthorized',1);
    raise exception 'ordinary member claimed worker job';
  exception when insufficient_privilege then null;
  end;
end $$;
select app.set_request_context('dddddddd-dddd-4ddd-8ddd-dddddddddddd','bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb');
select id as claimed_id from app.claim_runtime_job('worker-a',1) \gset
select case when (select count(*) from app.claim_runtime_job('worker-b',1))=0 then 'ok: claim mutually exclusive' else 'fail: double claim' end as result;
select pg_sleep(1.1); select case when app.reap_runtime_jobs()=1 then 'ok: expired lease reaped' else 'fail: reaper' end as result;
select case when app.cancel_runtime_job(:'claimed_id'::uuid) then 'ok: cancel queued job' else 'fail: cancel' end as result;
select app.set_request_context('cccccccc-cccc-4ccc-8ccc-cccccccccccc','bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb');
select * from app.begin_supply_runtime_run(:'case_id'::uuid,'PART-A','supply-run-1',null) \gset
select * from app.begin_supply_runtime_run(:'case_id'::uuid,'PART-A','supply-run-1',null);
do $$ begin
  if (select count(*) from app.runtime_mission where idempotency_key='supply-run-1') <> 1
     or (select count(*) from app.runtime_run where trace_run_id is not null) <> 1
     or (select count(*) from app.runtime_job where idempotency_key='supply-run-1') <> 1 then
    raise exception 'supply run was not idempotent or missed trace';
  end if;
end $$;
select app.set_request_context('dddddddd-dddd-4ddd-8ddd-dddddddddddd','bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb');
select id as supply_job_id, attempts as first_generation
from app.claim_runtime_job('worker-1',1) \gset
select pg_sleep(1.1);
do $$ begin
  if app.reap_runtime_jobs() <> 1 then raise exception 'expired supply lease not reaped'; end if;
end $$;
select id as reclaimed_job_id, attempts as second_generation
from app.claim_runtime_job('worker-2',30) \gset
do $$ begin
  if app.heartbeat_runtime_job(
       (select id from app.runtime_job where idempotency_key='supply-run-1'), 'worker-1', 1, 30)
     or app.complete_runtime_job(
       (select id from app.runtime_job where idempotency_key='supply-run-1'), 'worker-1', 1) then
    raise exception 'stale worker was allowed to modify reclaimed lease';
  end if;
  if not app.heartbeat_runtime_job(
       (select id from app.runtime_job where idempotency_key='supply-run-1'), 'worker-2', 2, 30)
     or not app.complete_runtime_job(
       (select id from app.runtime_job where idempotency_key='supply-run-1'), 'worker-2', 2) then
    raise exception 'current worker could not complete lease';
  end if;
  if (select status from app.runtime_run where trace_run_id is not null) <> 'completed' then
    raise exception 'completed job did not complete run';
  end if;
end $$;
rollback;
