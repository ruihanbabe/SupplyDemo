-- F31：新产品独立 schema。旧 Alembic / Python Harness 表不属于此 migration，也不授予产品访问。
begin;

create extension if not exists pgcrypto;
create schema if not exists app;

-- 产品服务使用的低权限数据库角色；生产环境的连接凭据由部署系统管理，浏览器没有该角色。
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'supplyagent_api') then
    create role supplyagent_api nologin noinherit nosuperuser nocreatedb nocreaterole noreplication;
  end if;
  -- migration executor 可在本地与 CI 切换到受限角色执行 RLS 验收；不创建可登录凭据。
  execute format('grant supplyagent_api to %I', current_user);
end $$;

create table if not exists app.organization (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique check (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  display_name text not null check (char_length(display_name) between 1 and 120),
  created_at timestamptz not null default now()
);

create table if not exists app.organization_member (
  organization_id uuid not null references app.organization(id) on delete cascade,
  user_id uuid not null,
  role text not null check (role in (
    'organization_admin', 'sales_manager', 'buyer', 'approver',
    'hardware_engineer', 'planner', 'auditor', 'maintainer'
  )),
  scope jsonb not null default '{}'::jsonb check (jsonb_typeof(scope) = 'object'),
  created_at timestamptz not null default now(),
  primary key (organization_id, user_id)
);

create table if not exists app.audit_event (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  actor_id uuid not null,
  event_type text not null check (char_length(event_type) between 1 and 96),
  entity_type text not null check (char_length(entity_type) between 1 and 96),
  entity_id text not null check (char_length(entity_id) between 1 and 160),
  metadata jsonb not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object'),
  occurred_at timestamptz not null default now()
);

create index if not exists audit_event_organization_occurred_idx
  on app.audit_event (organization_id, occurred_at desc);

-- 这两个函数只读取事务局部上下文；上下文只能由 app.set_request_context 设置。
create or replace function app.current_actor_id()
returns uuid language sql stable as $$
  select nullif(current_setting('app.actor_id', true), '')::uuid
$$;

create or replace function app.current_organization_id()
returns uuid language sql stable as $$
  select nullif(current_setting('app.organization_id', true), '')::uuid
$$;

-- security definer 避免 RLS policy 读取成员表时递归；函数本身只回答布尔值。
create or replace function app.is_member(target_organization_id uuid, target_actor_id uuid)
returns boolean language sql stable security definer set search_path = app, pg_temp as $$
  select target_actor_id is not null and exists (
    select 1 from app.organization_member
    where organization_id = target_organization_id and user_id = target_actor_id
  )
$$;

-- 仅受控服务在验证 Supabase Auth 会话后调用。true 令 GUC 在 commit/rollback 后自动清除。
create or replace function app.set_request_context(actor uuid, organization uuid)
returns void language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if actor is null or organization is null then
    raise exception 'actor and organization are required' using errcode = '22023';
  end if;
  if not app.is_member(organization, actor) then
    raise exception 'actor is not a member of organization' using errcode = '42501';
  end if;
  perform set_config('app.actor_id', actor::text, true);
  perform set_config('app.organization_id', organization::text, true);
end;
$$;

-- 组织选择页只用它列出已验证用户的成员关系；调用者是已验证 Supabase 会话后的服务端。
create or replace function app.list_organizations(actor uuid)
returns table (organization_id uuid, slug text, display_name text, role text, scope jsonb)
language sql stable security definer set search_path = app, pg_temp as $$
  select o.id, o.slug, o.display_name, m.role, m.scope
  from app.organization_member m
  join app.organization o on o.id = m.organization_id
  where m.user_id = actor
  order by o.created_at, o.id
$$;

create or replace function app.append_audit_event(
  input_event_type text,
  input_entity_type text,
  input_entity_id text,
  input_metadata jsonb default '{}'::jsonb
)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare event_id uuid;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null then
    raise exception 'missing request context' using errcode = '42501';
  end if;
  if not app.is_member(app.current_organization_id(), app.current_actor_id()) then
    raise exception 'invalid request context' using errcode = '42501';
  end if;
  insert into app.audit_event (organization_id, actor_id, event_type, entity_type, entity_id, metadata)
  values (app.current_organization_id(), app.current_actor_id(), input_event_type, input_entity_type,
          input_entity_id, coalesce(input_metadata, '{}'::jsonb))
  returning id into event_id;
  return event_id;
end;
$$;

alter table app.organization enable row level security;
alter table app.organization force row level security;
alter table app.organization_member enable row level security;
alter table app.organization_member force row level security;
alter table app.audit_event enable row level security;
alter table app.audit_event force row level security;

drop policy if exists organization_isolation on app.organization;
create policy organization_isolation on app.organization
  for select to supplyagent_api
  using (id = app.current_organization_id() and app.is_member(id, app.current_actor_id()));

drop policy if exists membership_isolation on app.organization_member;
create policy membership_isolation on app.organization_member
  for select to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()));

drop policy if exists audit_isolation on app.audit_event;
create policy audit_isolation on app.audit_event
  for select to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()));

revoke all on schema app from public;
revoke all on all tables in schema app from public;
revoke all on all functions in schema app from public;
grant usage on schema app to supplyagent_api;
grant select on app.organization, app.organization_member, app.audit_event to supplyagent_api;
grant execute on function app.current_actor_id() to supplyagent_api;
grant execute on function app.current_organization_id() to supplyagent_api;
grant execute on function app.is_member(uuid, uuid) to supplyagent_api;
grant execute on function app.set_request_context(uuid, uuid) to supplyagent_api;
grant execute on function app.list_organizations(uuid) to supplyagent_api;
grant execute on function app.append_audit_event(text, text, text, jsonb) to supplyagent_api;

commit;
