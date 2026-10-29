-- F48 M1: Memory-specific synthetic authorization, usage and bounded reservations.
begin;
create table if not exists app.memory_operation(
 id uuid primary key default gen_random_uuid(),organization_id uuid not null references app.organization(id),
 actor_id uuid not null,namespace text not null,purpose text not null check(purpose in('summarize','embedding_publish','embedding_search')),
 model text not null,input_hash text not null,work_id uuid references app.memory_work_item(id),generation integer,
 source_epoch bigint,deletion_id uuid references app.memory_delete_request(id),policy_version text not null default 'synthetic-local-v1',reserved_tokens integer not null check(reserved_tokens between 1 and 65536),
 state text not null default 'reserved' check(state in('reserved','completed','unknown')),
 input_tokens integer,output_tokens integer,cost_micros bigint,created_at timestamptz not null default clock_timestamp(),settled_at timestamptz,
 check((state='completed' and input_tokens is not null and output_tokens is not null and cost_micros is not null and input_tokens>=0 and output_tokens>=0 and cost_micros=0) or (state<>'completed' and input_tokens is null and output_tokens is null and cost_micros is null))
);
create index if not exists memory_operation_budget on app.memory_operation(organization_id,created_at);
alter table app.memory_operation enable row level security;
alter table app.memory_operation force row level security;
revoke all on app.memory_operation from public;
grant select on app.memory_operation to supplyagent_api;
drop policy if exists memory_operation_private on app.memory_operation;
create policy memory_operation_private on app.memory_operation for select to supplyagent_api using(organization_id=app.current_organization_id() and (actor_id=app.current_actor_id() or app.is_runtime_worker()));
create or replace function app.memory_reserve(input_namespace text,input_purpose text,input_model text,input_hash text,input_bound integer,input_work uuid,input_owner text,input_generation integer,input_delete uuid,input_epoch bigint) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare result uuid;declare used bigint;
begin
 if input_namespace !~ '^[a-f0-9]{64}$' or input_hash !~ '^[a-f0-9]{64}$' or input_bound not between 1 and 65536
 or input_purpose not in('summarize','embedding_publish','embedding_search')
 or input_model not in('fixture-summary-v1','fixture-embedding-v1') then raise exception 'memory_denied' using errcode='42501';end if;
 if input_work is null then
  if input_delete is not null then
   if input_purpose<>'embedding_publish' or not exists(select 1 from app.memory_delete_request d where d.id=input_delete and d.organization_id=app.current_organization_id() and (d.owner_id=app.current_actor_id() or app.is_runtime_worker()) and d.state='requested') then raise exception 'memory_denied' using errcode='42501';end if;
  elsif input_purpose<>'embedding_search' or not app.is_member(app.current_organization_id(),app.current_actor_id()) then raise exception 'memory_denied' using errcode='42501';end if;
 elsif not app.is_runtime_worker() or not exists(select 1 from app.memory_work_item w where w.id=input_work and w.organization_id=app.current_organization_id()
  and w.owner is not distinct from input_owner and w.generation=input_generation and w.state='leased' and w.lease_until>clock_timestamp() and app.memory_source_readable(w.source_id)) then
  raise exception 'memory_lease_lost' using errcode='42501';
 end if;
 if input_work is not null and exists(select 1 from app.memory_work_item w join app.memory_source s on s.id=w.source_id join app.context_conversation c on c.id=s.conversation_id where w.id=input_work and c.source_epoch is distinct from input_epoch) then raise exception 'memory_source_changed' using errcode='42501';end if;
 if input_delete is not null and (select coalesce(max(source_epoch),0) from app.context_conversation where organization_id=app.current_organization_id()) is distinct from input_epoch then raise exception 'memory_source_changed' using errcode='42501';end if;
 perform pg_advisory_xact_lock(hashtextextended(app.current_organization_id()::text||':memory-budget',0));
 select coalesce(sum(case when state='completed' then input_tokens+output_tokens else reserved_tokens end),0) into used from app.memory_operation
 where organization_id=app.current_organization_id() and created_at>clock_timestamp()-interval '1 day';
 if used+input_bound>1000000 then raise exception 'memory_budget_exceeded' using errcode='42501';end if;
 insert into app.memory_operation(organization_id,actor_id,namespace,purpose,model,input_hash,reserved_tokens,work_id,generation,deletion_id,source_epoch)
 values(app.current_organization_id(),app.current_actor_id(),input_namespace,input_purpose,input_model,input_hash,input_bound,input_work,input_generation,input_delete,input_epoch) returning id into result;
 return result;
end $$;
create or replace function app.memory_settle(input_id uuid,input_usage jsonb,input_state text) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
declare op app.memory_operation%rowtype;
begin
 select * into op from app.memory_operation where id=input_id and organization_id=app.current_organization_id() and actor_id=app.current_actor_id() for update;
 if op.id is null or input_state not in('completed','unknown') then raise exception 'memory_denied' using errcode='42501';end if;
 if op.state<>'reserved' then return;end if;
 if input_state='completed' and (jsonb_typeof(input_usage->'inputTokens') is distinct from 'number' or jsonb_typeof(input_usage->'outputTokens') is distinct from 'number'
 or (input_usage->>'inputTokens')::integer<0 or (input_usage->>'outputTokens')::integer<0
 or (input_usage->>'inputTokens')::integer+(input_usage->>'outputTokens')::integer>op.reserved_tokens) then raise exception 'memory_invalid' using errcode='22023';end if;
 update app.memory_operation set state=input_state,input_tokens=case when input_state='completed' then (input_usage->>'inputTokens')::integer end,
 output_tokens=case when input_state='completed' then (input_usage->>'outputTokens')::integer end,cost_micros=case when input_state='completed' then 0 end,settled_at=clock_timestamp() where id=op.id;
end $$;
revoke all on function app.memory_reserve(text,text,text,text,integer,uuid,text,integer,uuid,bigint),app.memory_settle(uuid,jsonb,text) from public;
grant execute on function app.memory_reserve(text,text,text,text,integer,uuid,text,integer,uuid,bigint),app.memory_settle(uuid,jsonb,text) to supplyagent_api;
-- Independent usage reservations hold a FK key-share lock; lease mutation never changes the key.
create or replace function app.memory_guard(input_id uuid,input_owner text,input_generation integer) returns app.memory_work_item
language plpgsql security definer set search_path=app,pg_temp as $$
declare w app.memory_work_item%rowtype;
begin
 select * into w from app.memory_work_item where id=input_id and organization_id=app.current_organization_id() for no key update;
 if input_owner is null or input_generation is null or not app.is_runtime_worker() or w.id is null or w.state<>'leased' or w.owner is distinct from input_owner or w.generation<>input_generation
 or w.lease_until<=clock_timestamp() then raise exception 'memory_lease_lost' using errcode='42501'; end if;
 if not app.memory_source_readable(w.source_id) then raise exception 'memory_source_denied' using errcode='42501'; end if;
 -- A delayed prefix must never replace an already indexed, more complete source range.
 if exists(select 1 from app.memory_source s join app.memory_source newer on newer.conversation_id=s.conversation_id and newer.watermark>s.watermark and newer.start_ordinal<=s.start_ordinal
 join app.memory_evidence e on e.source_id=newer.id join app.memory_projection p on p.version_id=e.version_id and p.state='indexed'
 where s.id=w.source_id) then raise exception 'memory_source_changed' using errcode='42501'; end if;
 return w;
end $$;
commit;
