-- F34：组织级模型网关元数据。credential_ref 是部署凭证库的 opaque 引用，绝非原始 BYOK。
begin;
create table if not exists app.model_policy (
 id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id) on delete restrict,
 provider text not null check (provider in ('replay','openai','anthropic','custom')), model_id text not null check (char_length(model_id) between 1 and 160),
 credential_ref text check (credential_ref is null or char_length(credential_ref) between 1 and 240), real_calls_enabled boolean not null default false,
 daily_budget_micros bigint not null default 0 check (daily_budget_micros >= 0), active boolean not null default true, created_at timestamptz not null default now(),
 unique(organization_id,provider,model_id)
);
create table if not exists app.model_usage (
 id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id) on delete restrict,
 run_id uuid references app.trace_run(id) on delete restrict, provider text not null, model_id text not null, input_digest text not null check (input_digest ~ '^[0-9a-f]{64}$'), output_digest text not null check (output_digest ~ '^[0-9a-f]{64}$'),
 input_tokens integer not null check(input_tokens>=0), output_tokens integer not null check(output_tokens>=0), cost_micros bigint not null check(cost_micros>=0), outcome text not null check(outcome in ('success','retryable_error','permanent_error','budget_denied')), occurred_at timestamptz not null default now()
);
create index if not exists model_usage_org_day_idx on app.model_usage(organization_id,occurred_at desc);
alter table app.model_policy enable row level security; alter table app.model_policy force row level security;
alter table app.model_usage enable row level security; alter table app.model_usage force row level security;
drop policy if exists model_policy_isolation on app.model_policy;
drop policy if exists model_usage_isolation on app.model_usage;
create policy model_policy_isolation on app.model_policy for select to supplyagent_api using(organization_id=app.current_organization_id() and app.is_member(organization_id,app.current_actor_id()));
create policy model_usage_isolation on app.model_usage for select to supplyagent_api using(organization_id=app.current_organization_id() and app.is_member(organization_id,app.current_actor_id()));
revoke all on app.model_policy,app.model_usage from public;
grant select on app.model_policy,app.model_usage to supplyagent_api;
commit;
