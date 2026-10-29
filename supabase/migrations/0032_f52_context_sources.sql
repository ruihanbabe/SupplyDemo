-- F52 C1: private source commits and versioned working checkpoints; no Memory dependency.
begin;
create table if not exists app.context_conversation(
 id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id),
 owner_id uuid not null, origin_job_id uuid not null references app.runtime_job(id),
 role text not null check(char_length(role) between 1 and 64), scope jsonb not null,
 state text not null default 'open' check(state in ('open','closed')),
 committed_watermark bigint not null default 0 check(committed_watermark>=0),
 created_at timestamptz not null default clock_timestamp(), closed_at timestamptz,
 unique(origin_job_id,role)
);
create table if not exists app.context_message(
 id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id),
 conversation_id uuid not null references app.context_conversation(id), ordinal bigint not null check(ordinal>0),
 message_unit_id uuid not null, role text not null check(role in ('user','assistant','tool_result','control')),
 invocation_id uuid, model_call_key text, batch_id integer, call_id text, call_index integer,
 job_id uuid references app.runtime_job(id), generation integer,
 payload jsonb not null check(jsonb_typeof(payload)='object'), content_hash text not null,
 source_label text not null check(source_label in ('user_supplied','model_generated','tool_evidence','control')),
 unit_complete boolean not null, commit_key text not null check(char_length(commit_key) between 1 and 120),
 commit_hash text not null, created_at timestamptz not null default clock_timestamp(),
 unique(conversation_id,ordinal)
);
create index if not exists context_message_commit on app.context_message(conversation_id,commit_key);
create index if not exists context_message_unit on app.context_message(conversation_id,message_unit_id);
create table if not exists app.context_working_state(
 id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id),
 conversation_id uuid not null references app.context_conversation(id), job_id uuid not null references app.runtime_job(id),
 generation integer not null check(generation>0), version integer not null check(version>0),
 previous_version integer, source_watermark bigint not null check(source_watermark>=0),
 kind text not null check(kind in ('working','checkpoint','compaction')),
 schema_version integer not null default 1, payload jsonb not null check(jsonb_typeof(payload)='object'), content_hash text not null,
 created_at timestamptz not null default clock_timestamp(), unique(conversation_id,version)
);

create or replace function app.context_job_scope(input_job uuid) returns jsonb
language sql stable security definer set search_path=app,pg_temp as $$
 select jsonb_build_object('opportunityId',m.context_ref->'opportunityId','caseId',m.context_ref->'caseId',
 'partNumber',m.context_ref->'partNumber','partNumbers',m.context_ref->'partNumbers',
 'bomRevisionId',m.context_ref->'bomRevisionId','bomBindingVersion',m.context_ref->'bomBindingVersion')
 from app.runtime_job j join app.runtime_run r on r.id=j.run_id join app.runtime_mission m on m.id=r.mission_id
 where j.id=input_job and j.organization_id=app.current_organization_id()
 and r.organization_id=j.organization_id and m.organization_id=j.organization_id;
$$;
create or replace function app.context_can_read(input_id uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select exists(select 1 from app.context_conversation c where c.id=input_id and c.organization_id=app.current_organization_id()
 and (c.owner_id=app.current_actor_id() or app.is_runtime_worker()) and app.tool_user_access(c.origin_job_id,c.owner_id));
$$;
create or replace function app.context_guard(input_id uuid,input_job uuid default null,input_owner text default null,input_generation integer default null)
returns app.context_conversation language plpgsql security definer set search_path=app,pg_temp as $$
declare c app.context_conversation%rowtype;
begin
 select * into c from app.context_conversation where id=input_id and organization_id=app.current_organization_id() for update;
 if c.id is null or not app.context_can_read(c.id) then raise exception 'context_denied' using errcode='42501'; end if;
 if input_job is not null then
  if input_owner is null or input_generation is null then raise exception 'lease_lost' using errcode='42501'; end if;
  perform app.tool_guard(input_job,input_owner,input_generation);
  if not app.tool_user_access(input_job,c.owner_id) or app.context_job_scope(input_job) is distinct from c.scope then
   raise exception 'context_scope_changed' using errcode='42501'; end if;
 elsif c.owner_id<>app.current_actor_id() then raise exception 'context_denied' using errcode='42501'; end if;
 return c;
end $$;
create or replace function app.context_open(input_job uuid,input_role text) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare requested uuid; declare result uuid;
begin
 select m.created_by into requested from app.runtime_job j join app.runtime_run r on r.id=j.run_id
 join app.runtime_mission m on m.id=r.mission_id where j.id=input_job and j.organization_id=app.current_organization_id();
 if requested is null or not app.tool_user_access(input_job,requested) or
 (requested<>app.current_actor_id() and not app.is_runtime_worker()) then
  raise exception 'context_denied' using errcode='42501'; end if;
 if input_role !~ '^[a-z][a-z0-9_]{0,63}$' then raise exception 'invalid context role' using errcode='22023'; end if;
 insert into app.context_conversation(organization_id,owner_id,origin_job_id,role,scope)
 values(app.current_organization_id(),requested,input_job,input_role,app.context_job_scope(input_job))
 on conflict(origin_job_id,role) do nothing returning id into result;
 if result is null then select id into result from app.context_conversation where origin_job_id=input_job and role=input_role; end if;
 return result;
end $$;

create or replace function app.context_commit(input_id uuid,input_job uuid,input_owner text,input_generation integer,
 input_watermark bigint,input_key text,input_units jsonb) returns bigint
language plpgsql security definer set search_path=app,pg_temp as $$
declare c app.context_conversation%rowtype; declare u jsonb; declare msg jsonb; declare head jsonb; declare call jsonb;
declare e app.tool_execution%rowtype; declare h text; declare prior_hash text; declare prior_end bigint;
declare current_pos bigint; declare n integer; declare i integer; declare role_name text; declare ref jsonb;
begin
 c:=app.context_guard(input_id,input_job,input_owner,input_generation);
 if input_key is null or char_length(input_key) not between 1 and 120 or jsonb_typeof(input_units)<>'array'
 or jsonb_array_length(input_units) not between 1 and 128 or octet_length(input_units::text)>1048576 then
  raise exception 'context_invalid' using errcode='22023'; end if;
 h:=encode(public.digest(convert_to(input_units::text,'UTF8'),'sha256'),'hex');
 select max(ordinal),min(commit_hash) into prior_end,prior_hash from app.context_message where conversation_id=c.id and commit_key=input_key;
 if prior_end is not null then
  if h<>prior_hash then raise exception 'context_idempotency_conflict' using errcode='22023'; end if;
  return prior_end;
 end if;
 if c.state<>'open' then raise exception 'context_closed' using errcode='22023'; end if;
 if c.committed_watermark<>input_watermark then raise exception 'context_watermark_conflict' using errcode='40001'; end if;
 current_pos:=input_watermark;
 for u in select value from jsonb_array_elements(input_units) loop
  if (u->>'unitId') is null or jsonb_typeof(u->'messages')<>'array' or jsonb_typeof(u->'complete')<>'boolean' then
   raise exception 'context_invalid' using errcode='22023'; end if;
  if exists(select 1 from app.context_message where conversation_id=c.id and message_unit_id=(u->>'unitId')::uuid) then
   raise exception 'context_duplicate_unit' using errcode='22023'; end if;
  n:=jsonb_array_length(u->'messages');head:=u->'messages'->0;
  if n not between 1 and 17 then raise exception 'context_invalid' using errcode='22023'; end if;
  if input_job is null and (n<>1 or head->>'role'<>'user' or (u->>'complete')::boolean is not true) then
   raise exception 'context_denied' using errcode='42501'; end if;
  if jsonb_array_length(coalesce(head->'payload'->'toolCalls','[]'::jsonb))>0 then
   if head->>'role'<>'assistant' or (u->>'complete')::boolean is not true or n<>jsonb_array_length(head->'payload'->'toolCalls')+1 then
    raise exception 'context_unpaired' using errcode='22023'; end if;
   for i in 1..n-1 loop
    call:=head->'payload'->'toolCalls'->(i-1);msg:=u->'messages'->i;
    if not (msg->'payload' ?& array['status','code','resultRef']) or exists(
      select 1 from jsonb_object_keys(msg->'payload') k where k not in ('status','code','resultRef')) then
      raise exception 'context_invalid_evidence' using errcode='42501'; end if;
    select * into e from app.tool_execution where organization_id=c.organization_id and job_id=input_job
      and generation=input_generation and invocation_id=(head->>'invocationId')::uuid and call_id=call->>'id';
    if not (call ?& array['id','name','arguments']) or (e.state<>'rejected' and jsonb_typeof(call->'arguments')<>'object') or e.id is null or e.state not in ('succeeded','rejected','failed','cancelled') or e.call_index<>i-1
      or e.batch_id is distinct from (head->>'batchId')::integer or e.model_call_key is distinct from head->>'modelCallKey'
      or call->>'name' not in (e.requested_name,coalesce(e.canonical_name,e.requested_name))
      or encode(public.digest(convert_to((call->'arguments')::text,'UTF8'),'sha256'),'hex')<>e.arguments_hash
      or msg->>'role'<>'tool_result' or msg->>'callId' is distinct from e.call_id
      or msg->>'invocationId' is distinct from e.invocation_id::text
      or msg->>'modelCallKey' is distinct from e.model_call_key
      or (msg->>'batchId')::integer is distinct from e.batch_id or (msg->>'index')::integer is distinct from e.call_index then
       raise exception 'context_unpaired' using errcode='22023'; end if;
    ref:=msg->'payload'->'resultRef';
    if e.state='succeeded' then
     if msg->'payload'->>'status' is distinct from 'ok' or ref->>'sourceKind' is distinct from e.result_kind
      or ref->>'sourceId' is distinct from e.result_id::text or ref->>'version' is distinct from e.result_version
      or ref->>'fragmentHash' is distinct from e.result_hash
      or ref->>'locator' is distinct from (case when e.result_kind='trace_artifact' then 'redacted_payload' else 'payload' end) then
      raise exception 'context_invalid_evidence' using errcode='42501'; end if;
    elsif msg->'payload'->>'status' is distinct from 'error' or ref is distinct from 'null'::jsonb
      or msg->'payload'->>'code' is distinct from e.error_code then
      raise exception 'context_invalid_evidence' using errcode='42501'; end if;
   end loop;
  elsif n<>1 or head->>'role'='tool_result' or (u->>'complete')::boolean is not true and head->>'role'<>'control' then
   raise exception 'context_unpaired' using errcode='22023';
  end if;
  for msg in select value from jsonb_array_elements(u->'messages') loop
   role_name:=msg->>'role';
   if role_name not in ('user','assistant','tool_result','control') or jsonb_typeof(msg->'payload')<>'object' then
    raise exception 'context_invalid' using errcode='22023'; end if;
   current_pos:=current_pos+1;
   insert into app.context_message(organization_id,conversation_id,ordinal,message_unit_id,role,
    invocation_id,model_call_key,batch_id,call_id,call_index,job_id,generation,payload,content_hash,source_label,unit_complete,commit_key,commit_hash)
   values(c.organization_id,c.id,current_pos,(u->>'unitId')::uuid,role_name,(msg->>'invocationId')::uuid,
    msg->>'modelCallKey',(msg->>'batchId')::integer,msg->>'callId',(msg->>'index')::integer,input_job,input_generation,
    msg->'payload',encode(public.digest(convert_to((msg->'payload')::text,'UTF8'),'sha256'),'hex'),
    case role_name when 'user' then 'user_supplied' when 'assistant' then 'model_generated' when 'tool_result' then 'tool_evidence' else 'control' end,
    (u->>'complete')::boolean,input_key,h);
  end loop;
 end loop;
 update app.context_conversation set committed_watermark=current_pos where id=c.id;
 return current_pos;
end $$;

create or replace function app.context_close(input_id uuid,input_watermark bigint) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
declare c app.context_conversation%rowtype;
begin
 c:=app.context_guard(input_id);
 if c.committed_watermark<>input_watermark then raise exception 'context_watermark_conflict' using errcode='40001'; end if;
 update app.context_conversation set state='closed',closed_at=coalesce(closed_at,clock_timestamp()) where id=c.id;
end $$;
create or replace function app.context_checkpoint(input_id uuid,input_job uuid,input_owner text,input_generation integer,
 input_version integer,input_watermark bigint,input_payload jsonb) returns integer
language plpgsql security definer set search_path=app,pg_temp as $$
declare c app.context_conversation%rowtype; declare v integer; declare evidence jsonb;
begin
 c:=app.context_guard(input_id,input_job,input_owner,input_generation);
 if c.state<>'open' or c.committed_watermark<>input_watermark then raise exception 'context_watermark_conflict' using errcode='40001'; end if;
 select coalesce(max(version),0) into v from app.context_working_state where conversation_id=c.id;
 if v<>input_version then raise exception 'context_version_conflict' using errcode='40001'; end if;
 if jsonb_typeof(input_payload)<>'object' or octet_length(input_payload::text)>65536 then raise exception 'context_invalid' using errcode='22023'; end if;
 for evidence in select value from jsonb_array_elements(coalesce(input_payload->'acceptedEvidence','[]'::jsonb)) loop
  if not exists(select 1 from app.tool_execution e where e.organization_id=c.organization_id and e.job_id=input_job
    and e.generation=input_generation and e.state='succeeded' and e.result_id::text=evidence->>'sourceId'
    and e.result_kind=evidence->>'sourceKind' and e.result_version=evidence->>'version' and e.result_hash=evidence->>'fragmentHash'
    and evidence->>'locator'=(case when e.result_kind='trace_artifact' then 'redacted_payload' else 'payload' end)) then
   raise exception 'context_invalid_evidence' using errcode='42501'; end if;
 end loop;
 insert into app.context_working_state(organization_id,conversation_id,job_id,generation,version,previous_version,source_watermark,kind,payload,content_hash)
 values(c.organization_id,c.id,input_job,input_generation,v+1,nullif(v,0),input_watermark,'checkpoint',input_payload,
 encode(public.digest(convert_to(input_payload::text,'UTF8'),'sha256'),'hex'));
 return v+1;
end $$;

do $$ declare t text; begin
 foreach t in array array['context_conversation','context_message','context_working_state'] loop
  execute format('alter table app.%I enable row level security',t);
  execute format('alter table app.%I force row level security',t);
  execute format('revoke all on app.%I from public',t);
  execute format('grant select on app.%I to supplyagent_api',t);
  execute format('drop policy if exists context_private on app.%I',t);
  execute format('create policy context_private on app.%I for select to supplyagent_api using(organization_id=app.current_organization_id() and app.context_can_read(%s))',t,case when t='context_conversation' then 'id' else 'conversation_id' end);
 end loop;
end $$;
revoke all on function app.context_job_scope(uuid),app.context_can_read(uuid),app.context_guard(uuid,uuid,text,integer),
 app.context_open(uuid,text),app.context_commit(uuid,uuid,text,integer,bigint,text,jsonb),app.context_close(uuid,bigint),
 app.context_checkpoint(uuid,uuid,text,integer,integer,bigint,jsonb) from public;
grant execute on function app.context_job_scope(uuid),app.context_can_read(uuid),app.context_guard(uuid,uuid,text,integer),
 app.context_open(uuid,text),app.context_commit(uuid,uuid,text,integer,bigint,text,jsonb),app.context_close(uuid,bigint),
 app.context_checkpoint(uuid,uuid,text,integer,integer,bigint,jsonb) to supplyagent_api;
commit;
