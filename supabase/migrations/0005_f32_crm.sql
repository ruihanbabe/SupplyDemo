-- F32：CRM 组织事实与不可执行 Proposal；审批/执行留给 F41。
begin;

create table if not exists app.crm_customer (
  id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id) on delete restrict,
  display_name text not null check (char_length(display_name) between 1 and 240), lifecycle text not null default 'lead' check (lifecycle in ('lead','active','inactive')),
  scope jsonb not null default '{}'::jsonb check (jsonb_typeof(scope) = 'object'), version integer not null default 1 check (version > 0),
  created_by uuid not null, created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table if not exists app.crm_contact (
  id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id) on delete restrict,
  customer_id uuid not null references app.crm_customer(id) on delete restrict, display_name text not null check (char_length(display_name) between 1 and 160),
  email text, scope jsonb not null default '{}'::jsonb check (jsonb_typeof(scope) = 'object'), version integer not null default 1 check (version > 0), created_by uuid not null, created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table if not exists app.crm_opportunity (
  id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id) on delete restrict,
  customer_id uuid not null references app.crm_customer(id) on delete restrict, title text not null check (char_length(title) between 1 and 240),
  stage text not null check (stage in ('lead','qualified','proposal','negotiation','won','lost')), kanban_position numeric(20,10) not null default 0,
  supply_case_id uuid references app.supply_case(id) on delete restrict, scope jsonb not null default '{}'::jsonb check (jsonb_typeof(scope) = 'object'), version integer not null default 1 check (version > 0), created_by uuid not null, created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table if not exists app.crm_conversation (
  id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id) on delete restrict,
  customer_id uuid references app.crm_customer(id) on delete restrict, opportunity_id uuid references app.crm_opportunity(id) on delete restrict,
  channel text not null check (channel in ('manual','whatsapp','feishu','email')), external_reference text, summary text not null check (char_length(summary) between 1 and 2000), scope jsonb not null default '{}'::jsonb check (jsonb_typeof(scope) = 'object'), version integer not null default 1 check (version > 0), created_by uuid not null, created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table if not exists app.crm_task (
  id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id) on delete restrict,
  opportunity_id uuid references app.crm_opportunity(id) on delete restrict, title text not null check (char_length(title) between 1 and 240), status text not null default 'open' check (status in ('open','done','cancelled')), scope jsonb not null default '{}'::jsonb check (jsonb_typeof(scope) = 'object'), version integer not null default 1 check (version > 0), created_by uuid not null, created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table if not exists app.crm_proposal (
  id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id) on delete restrict,
  target_type text not null check (target_type in ('customer','opportunity','task','reply_draft')), target_id uuid, target_version integer,
  proposal_type text not null check (proposal_type in ('customer_update','opportunity_update','task_update','reply_draft')), content jsonb not null check (jsonb_typeof(content) = 'object'), content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  evidence_refs jsonb not null default '[]'::jsonb check (jsonb_typeof(evidence_refs) = 'array'), risk_level text not null check (risk_level in ('r0','r1','r2')), status text not null default 'draft' check (status in ('draft','submitted','accepted','rejected','stale','expired')),
  requested_by uuid not null, created_at timestamptz not null default now()
);

create index if not exists crm_opportunity_kanban_idx on app.crm_opportunity (organization_id, stage, kanban_position, id);
create index if not exists crm_proposal_target_idx on app.crm_proposal (organization_id, target_type, target_id, created_at desc);

-- 事实和 Proposal 都由受控服务函数写入；产品 DB 角色只有读与函数执行权限。
create or replace function app.crm_can_propose() returns boolean language sql stable security definer set search_path = app, pg_temp as $$
  select exists (select 1 from app.organization_member where organization_id = app.current_organization_id() and user_id = app.current_actor_id() and role in ('organization_admin','sales_manager'))
$$;
create or replace function app.create_crm_proposal(input_target_type text, input_target_id uuid, input_target_version integer, input_proposal_type text, input_content jsonb, input_content_hash text, input_evidence_refs jsonb, input_risk_level text)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare proposal_id uuid;
begin
  if not app.crm_can_propose() then raise exception 'crm proposal capability required' using errcode = '42501'; end if;
  insert into app.crm_proposal (organization_id,target_type,target_id,target_version,proposal_type,content,content_hash,evidence_refs,risk_level,requested_by)
  values (app.current_organization_id(),input_target_type,input_target_id,input_target_version,input_proposal_type,input_content,input_content_hash,coalesce(input_evidence_refs,'[]'::jsonb),input_risk_level,app.current_actor_id()) returning id into proposal_id;
  return proposal_id;
end;
$$;

alter table app.crm_customer enable row level security; alter table app.crm_customer force row level security;
alter table app.crm_contact enable row level security; alter table app.crm_contact force row level security;
alter table app.crm_opportunity enable row level security; alter table app.crm_opportunity force row level security;
alter table app.crm_conversation enable row level security; alter table app.crm_conversation force row level security;
alter table app.crm_task enable row level security; alter table app.crm_task force row level security;
alter table app.crm_proposal enable row level security; alter table app.crm_proposal force row level security;
do $$ declare tbl text; begin foreach tbl in array array['crm_customer','crm_contact','crm_opportunity','crm_conversation','crm_task','crm_proposal'] loop
 execute format('drop policy if exists %I on app.%I', tbl || '_isolation', tbl);
 execute format('create policy %I on app.%I for select to supplyagent_api using (organization_id = app.current_organization_id() and app.is_member(organization_id, app.current_actor_id()))', tbl || '_isolation', tbl);
end loop; end $$;
drop policy if exists crm_proposal_service_insert on app.crm_proposal;
create policy crm_proposal_service_insert on app.crm_proposal for insert to public with check (organization_id = app.current_organization_id() and app.crm_can_propose());
revoke all on app.crm_customer,app.crm_contact,app.crm_opportunity,app.crm_conversation,app.crm_task,app.crm_proposal from public;
grant select on app.crm_customer,app.crm_contact,app.crm_opportunity,app.crm_conversation,app.crm_task,app.crm_proposal to supplyagent_api;
grant execute on function app.crm_can_propose() to supplyagent_api;
grant execute on function app.create_crm_proposal(text,uuid,integer,text,jsonb,text,jsonb,text) to supplyagent_api;
commit;
