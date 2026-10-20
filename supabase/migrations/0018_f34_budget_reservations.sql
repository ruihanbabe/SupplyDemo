-- F34: reserve the configured maximum charge before a model call, then settle it once.
-- Unsettled reservations remain charged against the day until explicitly reconciled.
begin;

alter table app.model_policy
  add column if not exists max_call_cost_micros bigint not null default 0
  check (max_call_cost_micros >= 0);
alter table app.model_policy
  add column if not exists input_price_micros_per_million bigint not null default 0
  check (input_price_micros_per_million >= 0);
alter table app.model_policy
  add column if not exists output_price_micros_per_million bigint not null default 0
  check (output_price_micros_per_million >= 0);
alter table app.model_policy
  add column if not exists max_output_tokens integer not null default 128
  check (max_output_tokens between 1 and 32768);
alter table app.model_usage
  add column if not exists policy_id uuid references app.model_policy(id) on delete restrict;
create index if not exists model_usage_policy_day_idx
  on app.model_usage(policy_id, occurred_at desc);

create table if not exists app.model_reservation (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  policy_id uuid not null references app.model_policy(id) on delete restrict,
  run_id uuid not null references app.trace_run(id) on delete restrict,
  call_key text not null check (char_length(call_key) between 1 and 120),
  input_digest text not null check (input_digest ~ '^[0-9a-f]{64}$'),
  reserved_micros bigint not null check (reserved_micros >= 0),
  settled boolean not null default false,
  created_at timestamptz not null default now(),
  unique (organization_id, call_key)
);

alter table app.model_reservation enable row level security;
alter table app.model_reservation force row level security;
drop policy if exists model_reservation_isolation on app.model_reservation;
create policy model_reservation_isolation on app.model_reservation for select to supplyagent_api
  using (organization_id = app.current_organization_id()
    and app.is_member(organization_id, app.current_actor_id()));
revoke all on app.model_reservation from public;
grant select on app.model_reservation to supplyagent_api;

create or replace function app.reserve_model_budget(
  input_policy_id uuid, input_run_id uuid, input_call_key text, input_digest text
) returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare p app.model_policy%rowtype;
declare existing app.model_reservation%rowtype;
declare spent bigint;
declare result_id uuid;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null
     or input_call_key is null or char_length(input_call_key) not between 1 and 120
     or input_digest is null or input_digest !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid model reservation context' using errcode = '22023';
  end if;
  select * into p from app.model_policy
    where id = input_policy_id and organization_id = app.current_organization_id()
    for update;
  if not found or not app.is_member(p.organization_id, app.current_actor_id())
     or not p.active or (p.provider <> 'replay' and not p.real_calls_enabled) then
    raise exception 'model policy unavailable' using errcode = '42501';
  end if;
  if not exists (select 1 from app.trace_run
      where id = input_run_id and organization_id = p.organization_id) then
    raise exception 'trace run outside organization' using errcode = '42501';
  end if;
  select * into existing from app.model_reservation
    where organization_id = p.organization_id and call_key = input_call_key;
  if found then
    raise exception 'model call key already reserved' using errcode = '23505';
  end if;
  select coalesce(sum(cost), 0) into spent from (
    select cost_micros as cost from app.model_usage
      where policy_id = p.id
        and occurred_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc'
        and occurred_at < (date_trunc('day', now() at time zone 'utc') + interval '1 day') at time zone 'utc'
    union all
    select reserved_micros from app.model_reservation
      where policy_id = p.id and not settled
        and created_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc'
        and created_at < (date_trunc('day', now() at time zone 'utc') + interval '1 day') at time zone 'utc'
  ) charges;
  if p.max_call_cost_micros > p.daily_budget_micros - spent then
    insert into app.model_usage (
      organization_id, policy_id, run_id, provider, model_id, input_digest, output_digest,
      input_tokens, output_tokens, cost_micros, outcome
    ) values (
      p.organization_id, p.id, input_run_id, p.provider, p.model_id, input_digest,
      encode(public.digest('', 'sha256'), 'hex'), 0, 0, 0, 'budget_denied'
    );
    return null;
  end if;
  insert into app.model_reservation (
    organization_id, policy_id, run_id, call_key, input_digest, reserved_micros
  ) values (
    p.organization_id, p.id, input_run_id, input_call_key, input_digest, p.max_call_cost_micros
  ) returning id into result_id;
  return result_id;
end;
$$;

create or replace function app.settle_model_budget(
  input_reservation_id uuid, input_output_digest text, input_tokens integer,
  input_output_tokens integer, input_cost_micros bigint, input_outcome text
) returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare r app.model_reservation%rowtype;
declare p app.model_policy%rowtype;
declare result_id uuid;
begin
  select * into r from app.model_reservation
    where id = input_reservation_id and organization_id = app.current_organization_id()
    for update;
  if not found or not app.is_member(r.organization_id, app.current_actor_id()) then
    raise exception 'model reservation unavailable' using errcode = '42501';
  end if;
  if r.settled then
    raise exception 'model reservation already settled' using errcode = '22023';
  end if;
  if input_output_digest is null or input_output_digest !~ '^[0-9a-f]{64}$'
     or input_tokens is null or input_tokens < 0
     or input_output_tokens is null or input_output_tokens < 0
     or input_cost_micros is null or input_cost_micros < 0
     or input_cost_micros > r.reserved_micros
     or input_outcome is null
     or input_outcome not in ('success', 'retryable_error', 'permanent_error') then
    raise exception 'invalid model settlement' using errcode = '22023';
  end if;
  select * into p from app.model_policy where id = r.policy_id;
  insert into app.model_usage (
    organization_id, policy_id, run_id, provider, model_id, input_digest, output_digest,
    input_tokens, output_tokens, cost_micros, outcome, occurred_at
  ) values (
    r.organization_id, p.id, r.run_id, p.provider, p.model_id, r.input_digest,
    input_output_digest, input_tokens, input_output_tokens, input_cost_micros,
    input_outcome, r.created_at
  ) returning id into result_id;
  update app.model_reservation set settled = true where id = r.id;
  return result_id;
end;
$$;

revoke all on function app.reserve_model_budget(uuid, uuid, text, text) from public;
revoke all on function app.settle_model_budget(uuid, text, integer, integer, bigint, text) from public;
grant execute on function app.reserve_model_budget(uuid, uuid, text, text) to supplyagent_api;
grant execute on function app.settle_model_budget(uuid, text, integer, integer, bigint, text) to supplyagent_api;
commit;
