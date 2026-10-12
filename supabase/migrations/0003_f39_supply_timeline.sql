-- F39：在途到货按日期建模，使影响分析可计算首次缺料日，而不是只做总量相减。
begin;

create table if not exists app.supply_receipt (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  receipt_reference text not null check (char_length(receipt_reference) between 1 and 160),
  part_number text not null check (char_length(part_number) between 1 and 160),
  expected_on date,
  quantity numeric(18, 4) check (quantity is null or quantity >= 0),
  status text not null check (status in ('in_transit', 'arrived', 'cancelled')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (organization_id, receipt_reference)
);

create index if not exists supply_receipt_organization_part_expected_idx
  on app.supply_receipt (organization_id, part_number, expected_on);

alter table app.supply_receipt enable row level security;
alter table app.supply_receipt force row level security;

drop policy if exists supply_receipt_isolation on app.supply_receipt;
create policy supply_receipt_isolation on app.supply_receipt for select to supplyagent_api
  using (organization_id = app.current_organization_id()
         and app.is_member(organization_id, app.current_actor_id()));

revoke all on app.supply_receipt from public;
grant select on app.supply_receipt to supplyagent_api;

commit;
