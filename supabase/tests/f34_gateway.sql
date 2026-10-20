\set ON_ERROR_STOP on
begin;
insert into app.organization(id,slug,display_name) values ('88888888-8888-8888-8888-888888888888','gateway-alpha','Gateway Alpha'),('aaaaaaaa-1111-4111-8111-111111111111','gateway-bravo','Gateway Bravo') on conflict do nothing;
insert into app.organization_member(organization_id,user_id,role) values ('88888888-8888-8888-8888-888888888888','12121212-1212-4121-8121-121212121212','organization_admin'),('aaaaaaaa-1111-4111-8111-111111111111','34343434-3434-4343-8343-343434343434','organization_admin') on conflict do nothing;
insert into app.model_policy(organization_id,provider,model_id,daily_budget_micros,max_call_cost_micros,real_calls_enabled) values ('88888888-8888-8888-8888-888888888888','replay','fixed-v1',1000,600,false),('aaaaaaaa-1111-4111-8111-111111111111','replay','fixed-v1',2000,600,false);
set local role supplyagent_api; select app.set_request_context('12121212-1212-4121-8121-121212121212','88888888-8888-8888-8888-888888888888');
select case when (select count(*) from app.model_policy)=1 and (select bool_and(real_calls_enabled=false) from app.model_policy) then 'ok: gateway policy isolated and real calls disabled' else 'fail: gateway policy' end as result;
select app.begin_trace_run('worker',null) as run_id \gset
select id as policy_id from app.model_policy \gset
select app.reserve_model_budget(:'policy_id'::uuid,:'run_id'::uuid,'call-1',repeat('a',64)) as reservation_id \gset
do $$ begin
  if (select count(*) from app.model_reservation) <> 1 then
    raise exception 'reservation not visible to organization';
  end if;
end $$;
select app.reserve_model_budget(:'policy_id'::uuid,:'run_id'::uuid,'call-2',repeat('b',64)) is null as budget_denied;
do $$ begin
  if (select count(*) from app.model_usage where outcome='budget_denied') <> 1 then
    raise exception 'budget denial not audited';
  end if;
end $$;
select app.settle_model_budget(:'reservation_id'::uuid,repeat('c',64),10,5,400,'success');
select app.reserve_model_budget(:'policy_id'::uuid,:'run_id'::uuid,'call-3',repeat('d',64)) as second_reservation_id \gset
do $$ begin
  if (select count(*) from app.model_reservation where not settled) <> 1
     or (select sum(cost_micros) from app.model_usage) <> 400 then
    raise exception 'settlement did not release unused reservation';
  end if;
  begin
    perform app.reserve_model_budget(
      (select id from app.model_policy where organization_id=app.current_organization_id()),
      (select id from app.trace_run where organization_id=app.current_organization_id() limit 1),
      'call-1', repeat('a',64));
    raise exception 'duplicate call key accepted';
  exception when unique_violation then null;
  end;
  begin
    perform app.settle_model_budget(
      (select id from app.model_reservation where call_key='call-3'),
      repeat('e',64), 10, 5, 601, 'success');
    raise exception 'over-reservation charge accepted';
  exception when invalid_parameter_value then null;
  end;
end $$;
select set_config('app.organization_id','aaaaaaaa-1111-4111-8111-111111111111',true);
select case when (select count(*) from app.model_policy)=0 then 'ok: gateway cross-organization denied' else 'fail: gateway leakage' end as result;
do $$ begin
  if (select count(*) from app.model_reservation) <> 0
     or (select count(*) from app.model_usage) <> 0 then
    raise exception 'gateway reservation or usage leaked across organization';
  end if;
end $$;
rollback;
