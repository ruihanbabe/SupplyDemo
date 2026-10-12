-- F39：组织范围的供应链事实。旧 Python / simenv 表不在本 migration 中。
begin;

create table if not exists app.supply_case (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  title text not null check (char_length(title) between 1 and 240),
  status text not null check (status in ('open', 'investigating', 'waiting', 'resolved', 'escalated')),
  severity text not null check (severity in ('low', 'medium', 'high', 'critical')),
  scope jsonb not null default '{}'::jsonb check (jsonb_typeof(scope) = 'object'),
  opened_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists app.inventory_position (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  part_number text not null check (char_length(part_number) between 1 and 160),
  location_code text not null check (char_length(location_code) between 1 and 80),
  on_hand_qty numeric(18, 4) check (on_hand_qty is null or on_hand_qty >= 0),
  reserved_qty numeric(18, 4) check (reserved_qty is null or reserved_qty >= 0),
  in_transit_qty numeric(18, 4) check (in_transit_qty is null or in_transit_qty >= 0),
  verified_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organization_id, part_number, location_code)
);

create table if not exists app.supply_demand (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  source_type text not null check (source_type in ('manual', 'customer_order', 'work_order', 'forecast')),
  source_reference text not null check (char_length(source_reference) between 1 and 160),
  line_key text not null check (char_length(line_key) between 1 and 160),
  part_number text not null check (char_length(part_number) between 1 and 160),
  required_qty numeric(18, 4) check (required_qty is null or required_qty >= 0),
  required_on date,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organization_id, source_type, source_reference, line_key)
);

create index if not exists supply_case_organization_updated_idx
  on app.supply_case (organization_id, updated_at desc);
create index if not exists inventory_position_organization_part_idx
  on app.inventory_position (organization_id, part_number);
create index if not exists supply_demand_organization_part_required_idx
  on app.supply_demand (organization_id, part_number, required_on);

alter table app.supply_case enable row level security;
alter table app.supply_case force row level security;
alter table app.inventory_position enable row level security;
alter table app.inventory_position force row level security;
alter table app.supply_demand enable row level security;
alter table app.supply_demand force row level security;

drop policy if exists supply_case_isolation on app.supply_case;
create policy supply_case_isolation on app.supply_case for select to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()));

drop policy if exists inventory_position_isolation on app.inventory_position;
create policy inventory_position_isolation on app.inventory_position for select to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()));

drop policy if exists supply_demand_isolation on app.supply_demand;
create policy supply_demand_isolation on app.supply_demand for select to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()));

revoke all on app.supply_case, app.inventory_position, app.supply_demand from public;
grant select on app.supply_case, app.inventory_position, app.supply_demand to supplyagent_api;

commit;
