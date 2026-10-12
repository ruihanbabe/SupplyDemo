\set ON_ERROR_STOP on
begin;
insert into app.organization (id,slug,display_name) values ('66666666-6666-6666-6666-666666666666','crm-alpha','CRM Alpha'),('77777777-7777-7777-7777-777777777777','crm-bravo','CRM Bravo') on conflict do nothing;
insert into app.organization_member (organization_id,user_id,role) values ('66666666-6666-6666-6666-666666666666','eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee','sales_manager'),('77777777-7777-7777-7777-777777777777','ffffffff-ffff-ffff-ffff-ffffffffffff','sales_manager') on conflict do nothing;
insert into app.organization_member (organization_id,user_id,role) values ('66666666-6666-6666-6666-666666666666','99999999-9999-9999-9999-999999999999','approver') on conflict do nothing;
insert into app.crm_customer (organization_id,display_name,created_by) values ('66666666-6666-6666-6666-666666666666','Alpha Customer','eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'),('77777777-7777-7777-7777-777777777777','Bravo Customer','ffffffff-ffff-ffff-ffff-ffffffffffff');
insert into app.crm_customer (organization_id,display_name,scope,created_by) values ('66666666-6666-6666-6666-666666666666','Approver Only','{"member_ids":["99999999-9999-9999-9999-999999999999"]}'::jsonb,'99999999-9999-9999-9999-999999999999');
set local role supplyagent_api;
select app.set_request_context('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee','66666666-6666-6666-6666-666666666666');
select id as customer_id from app.crm_customer \gset
select app.create_crm_opportunity(:'customer_id'::uuid, 'Alpha opportunity') as opportunity_id \gset
select app.create_crm_contact(:'customer_id'::uuid, 'Alpha Contact', 'alpha@example.test');
select app.create_crm_conversation(:'customer_id'::uuid, :'opportunity_id'::uuid, 'manual', 'Manual note');
select app.create_crm_task(:'opportunity_id'::uuid, 'Follow up');
select app.reorder_crm_opportunity(:'opportunity_id'::uuid, 1, 'qualified', 10);
select app.create_crm_proposal('customer', :'customer_id'::uuid, 1, 'customer_update', '{"display_name":"New name"}'::jsonb, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '[]'::jsonb, 'r1') as proposal_id \gset
select app.create_crm_proposal('customer', :'customer_id'::uuid, 2, 'customer_update', '{"display_name":"Stale name"}'::jsonb, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', '[]'::jsonb, 'r1');
select case when (select count(*) from app.crm_customer)=1 and (select count(*) from app.audit_event where event_type like 'crm.%') >= 7 then 'ok: member scope and CRM audit' else 'fail: scope or audit leakage' end as result;
select app.set_request_context('99999999-9999-9999-9999-999999999999','66666666-6666-6666-6666-666666666666');
select app.review_crm_proposal(:'proposal_id'::uuid, 'accepted');
select case when (select count(*) from app.crm_customer)=2 and (select count(*) from app.crm_opportunity where customer_id=:'customer_id'::uuid and stage='qualified' and version=2)=1 and (select count(*) from app.crm_contact)=1 and (select count(*) from app.crm_conversation)=1 and (select count(*) from app.crm_task)=1 and (select count(*) from app.crm_proposal where status='accepted')=1 and (select count(*) from app.crm_proposal where status='stale')=1 then 'ok: crm facts, review and stale proposal isolated' else 'fail: crm facts' end as result;
select set_config('app.organization_id','77777777-7777-7777-7777-777777777777',true);
select case when (select count(*) from app.crm_customer)=0 and (select count(*) from app.crm_opportunity)=0 and (select count(*) from app.crm_proposal)=0 then 'ok: crm cross-organization denied' else 'fail: crm leakage' end as result;
rollback;
