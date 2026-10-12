\set ON_ERROR_STOP on
begin;
insert into app.organization(id,slug,display_name) values ('88888888-8888-8888-8888-888888888888','gateway-alpha','Gateway Alpha'),('aaaaaaaa-1111-4111-8111-111111111111','gateway-bravo','Gateway Bravo') on conflict do nothing;
insert into app.organization_member(organization_id,user_id,role) values ('88888888-8888-8888-8888-888888888888','12121212-1212-4121-8121-121212121212','organization_admin'),('aaaaaaaa-1111-4111-8111-111111111111','34343434-3434-4343-8343-343434343434','organization_admin') on conflict do nothing;
insert into app.model_policy(organization_id,provider,model_id,daily_budget_micros,real_calls_enabled) values ('88888888-8888-8888-8888-888888888888','replay','fixed-v1',1000,false),('aaaaaaaa-1111-4111-8111-111111111111','replay','fixed-v1',2000,false);
set local role supplyagent_api; select app.set_request_context('12121212-1212-4121-8121-121212121212','88888888-8888-8888-8888-888888888888');
select case when (select count(*) from app.model_policy)=1 and (select bool_and(real_calls_enabled=false) from app.model_policy) then 'ok: gateway policy isolated and real calls disabled' else 'fail: gateway policy' end as result;
select set_config('app.organization_id','aaaaaaaa-1111-4111-8111-111111111111',true);
select case when (select count(*) from app.model_policy)=0 then 'ok: gateway cross-organization denied' else 'fail: gateway leakage' end as result;
rollback;
