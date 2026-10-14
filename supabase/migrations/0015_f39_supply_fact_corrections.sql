-- F39 纠正：到货单按行建模、限制函数执行者，并防止旧库存观测覆盖新快照。
begin;

-- 历史单行记录统一补为行号 1；新事实必须由 API 明确提供 line_key。
alter table app.supply_receipt
  add column if not exists line_key text not null default '1'
  check (char_length(line_key) between 1 and 160);
-- 默认值只用于补齐历史单行记录；后续事实必须明确给出 line_key。
alter table app.supply_receipt
  alter column line_key drop default;
alter table app.supply_receipt
  drop constraint if exists supply_receipt_organization_id_receipt_reference_key;
alter table app.supply_receipt
  drop constraint if exists supply_receipt_organization_receipt_line_key;
alter table app.supply_receipt
  add constraint supply_receipt_organization_receipt_line_key unique (organization_id, receipt_reference, line_key);

-- 时间线只会查询尚在运输且有日期的到货；历史到货仍留在表中，不从业务事实删除。
drop index if exists app.supply_receipt_organization_part_expected_idx;
create index if not exists supply_receipt_in_transit_timeline_idx
  on app.supply_receipt (organization_id, part_number, expected_on)
  where status = 'in_transit' and expected_on is not null;

-- 旧版本曾以 SECURITY DEFINER command function 写入普通事实。普通录入现由
-- Python 参数化 DML + RLS 完成，故旧环境也必须移除这些旁路入口。
drop function if exists app.create_supply_case(text, text, text, jsonb);
drop function if exists app.upsert_inventory_position(text, text, numeric, numeric, numeric, timestamptz);
drop function if exists app.upsert_supply_demand(text, text, text, text, numeric, date);
drop function if exists app.upsert_supply_receipt(text, text, date, numeric, text);
drop function if exists app.upsert_supply_receipt(text, text, text, date, numeric, text);
drop function if exists app.supply_can_propose();

-- 只授予产品 API 角色必要的 DML；RLS 再以请求上下文的成员关系判定行权限。
grant insert, update on app.supply_case, app.inventory_position, app.supply_demand, app.supply_receipt
  to supplyagent_api;

drop policy if exists supply_case_member_insert on app.supply_case;
create policy supply_case_member_insert on app.supply_case for insert to supplyagent_api
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists supply_case_member_update on app.supply_case;
create policy supply_case_member_update on app.supply_case for update to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()))
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));

drop policy if exists inventory_position_member_insert on app.inventory_position;
create policy inventory_position_member_insert on app.inventory_position for insert to supplyagent_api
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists inventory_position_member_update on app.inventory_position;
create policy inventory_position_member_update on app.inventory_position for update to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()))
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));

drop policy if exists supply_demand_member_insert on app.supply_demand;
create policy supply_demand_member_insert on app.supply_demand for insert to supplyagent_api
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists supply_demand_member_update on app.supply_demand;
create policy supply_demand_member_update on app.supply_demand for update to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()))
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));

drop policy if exists supply_receipt_member_insert on app.supply_receipt;
create policy supply_receipt_member_insert on app.supply_receipt for insert to supplyagent_api
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));
drop policy if exists supply_receipt_member_update on app.supply_receipt;
create policy supply_receipt_member_update on app.supply_receipt for update to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()))
  with check (organization_id = app.current_organization_id()
              and app.is_member(organization_id, app.current_actor_id()));
commit;
