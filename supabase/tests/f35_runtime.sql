\set ON_ERROR_STOP on
begin;
insert into app.organization(id,slug,display_name) values ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','runtime-alpha','Runtime Alpha') on conflict do nothing;
insert into app.organization_member(organization_id,user_id,role) values ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','cccccccc-cccc-4ccc-8ccc-cccccccccccc','organization_admin') on conflict do nothing;
insert into app.runtime_mission(organization_id,kind,created_by) values ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb','test','cccccccc-cccc-4ccc-8ccc-cccccccccccc') returning id as mission_id \gset
insert into app.runtime_run(organization_id,mission_id) values ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',:'mission_id'::uuid) returning id as run_id \gset
set local role supplyagent_api; select app.set_request_context('cccccccc-cccc-4ccc-8ccc-cccccccccccc','bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb');
select app.enqueue_runtime_job(:'run_id'::uuid,'idempotent-1','agent_turn',1) as job_id \gset
select app.enqueue_runtime_job(:'run_id'::uuid,'idempotent-1','agent_turn',1);
select case when (select count(*) from app.runtime_job)=1 then 'ok: enqueue idempotent' else 'fail: duplicate job' end as result;
select id as claimed_id from app.claim_runtime_job('worker-a',1) \gset
select case when (select count(*) from app.claim_runtime_job('worker-b',1))=0 then 'ok: claim mutually exclusive' else 'fail: double claim' end as result;
select pg_sleep(1.1); select case when app.reap_runtime_jobs()=1 then 'ok: expired lease reaped' else 'fail: reaper' end as result;
select case when app.cancel_runtime_job(:'claimed_id'::uuid) then 'ok: cancel queued job' else 'fail: cancel' end as result;
rollback;
