-- F39：供应链事实按组织 RLS 隔离。所有 fixture 在事务末尾回滚。
\set ON_ERROR_STOP on
begin;

insert into app.organization (id, slug, display_name) values
  ('11111111-1111-1111-1111-111111111111', 'alpha-org', 'Alpha'),
  ('22222222-2222-2222-2222-222222222222', 'bravo-org', 'Bravo')
on conflict (id) do nothing;
insert into app.organization_member (organization_id, user_id, role) values
  ('11111111-1111-1111-1111-111111111111', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'buyer'),
  ('22222222-2222-2222-2222-222222222222', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'buyer')
on conflict do nothing;

insert into app.supply_case (organization_id, title, status, severity) values
  ('11111111-1111-1111-1111-111111111111', 'Alpha shortage', 'open', 'high'),
  ('22222222-2222-2222-2222-222222222222', 'Bravo shortage', 'open', 'high');
insert into app.inventory_position (organization_id, part_number, location_code, on_hand_qty, reserved_qty, in_transit_qty) values
  ('11111111-1111-1111-1111-111111111111', 'PART-A', 'MAIN', 10, 2, null),
  ('22222222-2222-2222-2222-222222222222', 'PART-A', 'MAIN', 99, 0, 0);
insert into app.supply_demand (organization_id, source_type, source_reference, line_key, part_number, required_qty) values
  ('11111111-1111-1111-1111-111111111111', 'manual', 'alpha-input', '1', 'PART-A', 12),
  ('22222222-2222-2222-2222-222222222222', 'manual', 'bravo-input', '1', 'PART-A', 1);
insert into app.supply_receipt (organization_id, receipt_reference, part_number, expected_on, quantity, status) values
  ('11111111-1111-1111-1111-111111111111', 'alpha-shipment', 'PART-A', current_date + 1, 4, 'in_transit'),
  ('22222222-2222-2222-2222-222222222222', 'bravo-shipment', 'PART-A', current_date + 1, 2, 'in_transit');

set local role supplyagent_api;
select app.set_request_context('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111');
select case when (select count(*) from app.supply_case) = 1
                 and (select count(*) from app.inventory_position) = 1
                 and (select count(*) from app.supply_demand) = 1
                 and (select count(*) from app.supply_receipt) = 1
            then 'ok: alpha supply facts isolated' else 'fail: alpha leakage' end as result;
select set_config('app.organization_id', '22222222-2222-2222-2222-222222222222', true);
select case when (select count(*) from app.supply_case) = 0
                 and (select count(*) from app.inventory_position) = 0
                 and (select count(*) from app.supply_demand) = 0
                 and (select count(*) from app.supply_receipt) = 0
            then 'ok: forged organization denied' else 'fail: forged organization leaked' end as result;

reset role;
rollback;
