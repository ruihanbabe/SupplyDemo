-- F52 C2: per-request private projection, one-use consumption and source epochs.
begin;
alter table app.context_conversation add column if not exists source_epoch bigint not null default 0;
create table if not exists app.context_snapshot(
 id uuid primary key, organization_id uuid not null references app.organization(id),
 conversation_id uuid not null references app.context_conversation(id), requested_by uuid not null, worker_actor_id uuid not null,
 job_id uuid not null references app.runtime_job(id), generation integer not null, worker_owner text not null,
 invocation_id uuid not null, model_call_key text not null, source_watermark bigint not null, source_epoch bigint not null,
 schema_version integer not null default 1, context_policy_version text not null, model_policy_hash text not null,
 projection jsonb not null check(jsonb_typeof(projection)='object' and octet_length(projection::text)<=1048576),
 projection_hash text not null, request_hash text, state text not null default 'prepared' check(state in ('prepared','accepted','invalidated','cancelled')),
 created_at timestamptz not null default clock_timestamp(), accepted_at timestamptz,
 unique(invocation_id,model_call_key)
);
create index if not exists context_snapshot_conversation on app.context_snapshot(conversation_id,created_at);
-- These columns are absent from databases that have not enabled F49 yet.
do $$ begin
 if to_regclass('app.model_exchange') is not null then
  alter table app.model_exchange add column if not exists context_snapshot_id uuid references app.context_snapshot(id);
  alter table app.model_exchange add column if not exists projection_hash text;
  alter table app.model_exchange add column if not exists request_hash text;
 end if;
end $$;

create or replace function app.context_bump_epoch() returns trigger
language plpgsql security definer set search_path=app,pg_temp as $$
declare org uuid;
begin
 org:=case when tg_op='DELETE' then old.organization_id else new.organization_id end;
 update app.context_conversation set source_epoch=source_epoch+1 where organization_id=org;
 update app.context_snapshot set state='invalidated' where organization_id=org and state='prepared';
 return null;
end $$;
do $$ declare t text; begin
 foreach t in array array['organization_member','crm_customer','crm_contact','crm_opportunity','crm_conversation','crm_task',
  'supply_case','knowledge_document','knowledge_document_version','knowledge_chunk','knowledge_document_acl','knowledge_version_revocation','knowledge_document_withdrawal',
  'bom_revision','opportunity_bom_binding','commercial_quote_revision','model_policy'] loop
  if to_regclass('app.'||t) is not null then
   execute format('drop trigger if exists context_epoch_change on app.%I',t);
   execute format('create trigger context_epoch_change after insert or update or delete on app.%I for each row execute function app.context_bump_epoch()',t);
  end if;
 end loop;
end $$;
create or replace function app.context_prepare(input_id uuid,input_conversation uuid,input_job uuid,input_owner text,input_generation integer,
 input_invocation uuid,input_call_key text,input_model_policy_hash text,input_projection jsonb) returns text
language plpgsql security definer set search_path=app,pg_temp as $$
declare c app.context_conversation%rowtype; declare h text;
begin
 c:=app.context_guard(input_conversation,input_job,input_owner,input_generation);
 if c.state<>'open' or input_call_key !~ ('^'||input_invocation::text||'-[1-9][0-9]{0,3}$') or input_projection->>'policyVersion' is null then
  raise exception 'context_invalid' using errcode='22023'; end if;
 h:=encode(public.digest(convert_to(input_projection::text,'UTF8'),'sha256'),'hex');
 insert into app.context_snapshot(id,organization_id,conversation_id,requested_by,worker_actor_id,job_id,generation,worker_owner,invocation_id,model_call_key,
 source_watermark,source_epoch,context_policy_version,model_policy_hash,projection,projection_hash)
 values(input_id,c.organization_id,c.id,c.owner_id,app.current_actor_id(),input_job,input_generation,input_owner,input_invocation,input_call_key,
 c.committed_watermark,c.source_epoch,input_projection->>'policyVersion',input_model_policy_hash,input_projection,h);
 return h;
end $$;
create or replace function app.context_accept(input_id uuid,input_trace_run uuid,input_call_key text,input_policy_hash text,input_projection_hash text,input_request_hash text)
returns void language plpgsql security definer set search_path=app,pg_temp as $$
declare s app.context_snapshot%rowtype; declare c app.context_conversation%rowtype; declare j app.runtime_job%rowtype;
begin
 select * into s from app.context_snapshot where id=input_id and organization_id=app.current_organization_id();
 if s.id is null or not app.is_runtime_worker() or s.worker_actor_id<>app.current_actor_id() then raise exception 'context_denied' using errcode='42501'; end if;
 select * into j from app.runtime_job where id=s.job_id;
 c:=app.context_guard(s.conversation_id,j.id,s.worker_owner,s.generation);
 select * into s from app.context_snapshot where id=input_id for update;
 if s.state<>'prepared' or c.state<>'open' or s.source_epoch<>c.source_epoch or s.source_watermark<>c.committed_watermark
  or s.model_call_key is distinct from input_call_key or s.model_policy_hash is distinct from input_policy_hash
  or s.projection_hash is distinct from input_projection_hash
  or not exists(select 1 from app.runtime_run r where r.id=j.run_id and r.trace_run_id=input_trace_run) then
  raise exception 'context_snapshot_invalid' using errcode='42501'; end if;
 update app.context_snapshot set state='accepted',request_hash=input_request_hash,accepted_at=clock_timestamp() where id=s.id;
end $$;
create or replace function app.context_finish(input_id uuid,input_job uuid,input_owner text,input_generation integer,input_watermark bigint)
returns void language plpgsql security definer set search_path=app,pg_temp as $$
declare c app.context_conversation%rowtype;
begin
 c:=app.context_guard(input_id,input_job,input_owner,input_generation);
 if c.committed_watermark<>input_watermark then raise exception 'context_watermark_conflict' using errcode='40001'; end if;
 update app.context_conversation set state='closed',closed_at=coalesce(closed_at,clock_timestamp()) where id=c.id;
end $$;
-- RLS hides revoked conversations; hidden history must not reopen the legacy bypass.
create or replace function app.context_required(input_trace_run uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select exists(select 1 from app.context_conversation c
  join app.runtime_job j on j.id=c.origin_job_id join app.runtime_run r on r.id=j.run_id
  where c.organization_id=app.current_organization_id() and r.trace_run_id=input_trace_run)
$$;
revoke all on function app.context_required(uuid) from public;
grant execute on function app.context_required(uuid) to supplyagent_api;
-- Cleanup must remain possible after lease loss, but can only deny this Worker's own ready.
create or replace function app.context_abandon(input_invocation uuid) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if not app.is_runtime_worker() then raise exception 'context_denied' using errcode='42501'; end if;
 update app.context_snapshot set state='cancelled' where organization_id=app.current_organization_id()
  and worker_actor_id=app.current_actor_id() and invocation_id=input_invocation and state='prepared';
end $$;
revoke all on function app.context_abandon(uuid) from public;
grant execute on function app.context_abandon(uuid) to supplyagent_api;
alter table app.context_snapshot enable row level security;
alter table app.context_snapshot force row level security;
revoke all on app.context_snapshot from public;
grant select on app.context_snapshot to supplyagent_api;
drop policy if exists context_snapshot_private on app.context_snapshot;
create policy context_snapshot_private on app.context_snapshot for select to supplyagent_api using(organization_id=app.current_organization_id() and app.context_can_read(conversation_id));
revoke all on function app.context_bump_epoch(),app.context_prepare(uuid,uuid,uuid,text,integer,uuid,text,text,jsonb),
 app.context_accept(uuid,uuid,text,text,text,text),app.context_finish(uuid,uuid,text,integer,bigint) from public;
grant execute on function app.context_prepare(uuid,uuid,uuid,text,integer,uuid,text,text,jsonb),
 app.context_accept(uuid,uuid,text,text,text,text),app.context_finish(uuid,uuid,text,integer,bigint) to supplyagent_api;
commit;
