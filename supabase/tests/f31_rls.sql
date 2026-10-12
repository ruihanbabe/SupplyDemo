-- F31 集成验收：以受控产品数据库角色读取两组织相同形状的数据。
-- 此脚本由 make tenant-test 执行；整个事务回滚，不保留 fixture。
\set ON_ERROR_STOP on
begin;

insert into app.organization (id, slug, display_name) values
  ('11111111-1111-1111-1111-111111111111', 'alpha-org', 'Alpha'),
  ('22222222-2222-2222-2222-222222222222', 'bravo-org', 'Bravo')
on conflict (id) do nothing;

insert into app.organization_member (organization_id, user_id, role) values
  ('11111111-1111-1111-1111-111111111111', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'organization_admin'),
  ('22222222-2222-2222-2222-222222222222', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'organization_admin')
on conflict do nothing;

set local role supplyagent_api;

-- 缺组织上下文：RLS 不返回任何行。
select case when count(*) = 0 then 'ok: missing context denied' else 'fail: missing context leaked' end as result
  from app.organization;

select app.set_request_context('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111');
select case when count(*) = 1 and min(slug) = 'alpha-org' then 'ok: own organization visible'
            else 'fail: own organization result' end as result
  from app.organization;
select case when count(*) = 1 and bool_and(organization_id = '11111111-1111-1111-1111-111111111111'::uuid)
            then 'ok: membership isolated' else 'fail: membership isolation' end as result
  from app.organization_member;

-- 即使恶意修改组织 GUC，成员检查仍拒绝另一组织。
select set_config('app.organization_id', '22222222-2222-2222-2222-222222222222', true);
select case when count(*) = 0 then 'ok: cross organization denied' else 'fail: cross organization leaked' end as result
  from app.organization;

reset role;
rollback;
