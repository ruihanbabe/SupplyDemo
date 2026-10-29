-- F48 M1: consumed-reference dependencies, deny closure, retention and restore replay.
begin;
create or replace function app.memory_domain_lock(input_org uuid,input_user uuid,input_scope text) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if input_org is distinct from app.current_organization_id() or (input_user is distinct from app.current_actor_id() and not app.is_runtime_worker()) then raise exception 'memory_denied' using errcode='42501';end if;
 perform pg_advisory_xact_lock(hashtextextended(input_org::text||':'||input_user::text||':'||input_scope,0));
end $$;
revoke all on function app.memory_domain_lock(uuid,uuid,text) from public;
grant execute on function app.memory_domain_lock(uuid,uuid,text) to supplyagent_api;
create table if not exists app.memory_recovery_state(organization_id uuid primary key references app.organization(id),blocked boolean not null default false,ledger_applied boolean not null default false);
alter table app.memory_recovery_state enable row level security;
alter table app.memory_recovery_state force row level security;
revoke all on app.memory_recovery_state from public;
create or replace function app.memory_recovery_ready() returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select not exists(select 1 from app.memory_recovery_state where organization_id=app.current_organization_id() and blocked)
$$;
create or replace function app.memory_begin_recovery() returns void
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if not app.is_runtime_worker() then raise exception 'memory_denied' using errcode='42501';end if;
 insert into app.memory_recovery_state values(app.current_organization_id(),true,false) on conflict(organization_id) do update set blocked=true,ledger_applied=false;
end $$;
create or replace function app.memory_finish_recovery() returns void
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if not app.is_runtime_worker() or not exists(select 1 from app.memory_recovery_state where organization_id=app.current_organization_id() and ledger_applied) or exists(select 1 from app.memory_delete_request where organization_id=app.current_organization_id() and mode='hard' and state='requested') then raise exception 'memory_denied' using errcode='42501';end if;
 update app.memory_recovery_state set blocked=false where organization_id=app.current_organization_id();
end $$;
revoke all on function app.memory_recovery_ready(),app.memory_begin_recovery(),app.memory_finish_recovery() from public;
grant execute on function app.memory_recovery_ready(),app.memory_begin_recovery(),app.memory_finish_recovery() to supplyagent_api;
create or replace function app.memory_current_epoch() returns bigint
language sql stable security definer set search_path=app,pg_temp as $$
 select coalesce(max(source_epoch),0) from app.context_conversation where organization_id=app.current_organization_id()
$$;
revoke all on function app.memory_current_epoch() from public;
grant execute on function app.memory_current_epoch() to supplyagent_api;
create table if not exists app.memory_dependency(
 organization_id uuid not null references app.organization(id),version_id uuid not null references app.memory_version(id),
 conversation_id uuid not null references app.context_conversation(id),created_at timestamptz not null default clock_timestamp(),primary key(version_id,conversation_id)
);
create index if not exists memory_dependency_reverse on app.memory_dependency(conversation_id);
alter table app.memory_dependency enable row level security;
alter table app.memory_dependency force row level security;
revoke all on app.memory_dependency from public;
grant select on app.memory_dependency to supplyagent_api;
drop policy if exists memory_dependency_private on app.memory_dependency;
create policy memory_dependency_private on app.memory_dependency for select to supplyagent_api using(organization_id=app.current_organization_id() and app.context_can_read(conversation_id));
create table if not exists app.memory_replay_log(
 organization_id uuid not null references app.organization(id),request_id uuid not null,replayed_at timestamptz not null default clock_timestamp(),primary key(organization_id,request_id)
);
alter table app.memory_replay_log enable row level security;
alter table app.memory_replay_log force row level security;
revoke all on app.memory_replay_log from public;
create or replace function app.memory_source_base_readable(input_id uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select exists(select 1 from app.memory_source s where s.id=input_id and s.organization_id=app.current_organization_id()
 and s.state='ready' and s.created_at>clock_timestamp()-interval '7 days' and (s.owner_id=app.current_actor_id() or app.is_runtime_worker())
 and app.is_member(s.organization_id,s.owner_id) and (s.conversation_id is null or app.context_can_read(s.conversation_id)))
$$;
create or replace function app.memory_source_ancestors(input_id uuid) returns table(id uuid)
language sql stable security definer set search_path=app,pg_temp as $$
 with recursive sources(id) as (
 select s.id from app.memory_source s where s.id=input_id and s.organization_id=app.current_organization_id() and (s.owner_id=app.current_actor_id() or app.is_runtime_worker())
 union
 select e.source_id from sources a join app.memory_source s on s.id=a.id join app.memory_dependency d on d.conversation_id=s.conversation_id
 join app.memory_evidence e on e.version_id=d.version_id where d.organization_id=app.current_organization_id()
 ) select id from sources
$$;
create or replace function app.memory_source_readable(input_id uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select app.memory_source_base_readable(input_id) and (app.memory_recovery_ready() or app.is_runtime_worker())
 and not exists(select 1 from app.memory_source_ancestors(input_id) a where not app.memory_source_base_readable(a.id))
 and not exists(select 1 from app.memory_source_ancestors(input_id) a join app.memory_source s on s.id=a.id
 join app.memory_dependency d on d.conversation_id=s.conversation_id join app.memory_version v on v.id=d.version_id
 where v.status not in('ready','active') or v.expires_at<=clock_timestamp())
$$;
create or replace function app.memory_snapshot_dependency() returns trigger
language plpgsql security definer set search_path=app,pg_temp as $$
declare item jsonb;declare c app.context_conversation%rowtype;declare v app.memory_version%rowtype;
begin
 select * into c from app.context_conversation where id=new.conversation_id;
 for item in select value from jsonb_array_elements(coalesce(new.projection->'memoryItems','[]')) loop
  select * into v from app.memory_version where id=(item->>'versionId')::uuid;
  perform app.memory_domain_lock(c.organization_id,c.owner_id,v.object_scope);
  select * into v from app.memory_version where id=(item->>'versionId')::uuid;
  if not app.memory_recovery_ready() or v.id is null or v.organization_id<>c.organization_id or v.owner_id<>c.owner_id or not app.memory_version_readable(v.id)
   or v.object_scope is distinct from jsonb_build_object('scope',c.scope,'role',regexp_replace(c.role,'_g[0-9]+$',''))::text
   or v.body is distinct from item->>'body' or v.content_hash is distinct from item->>'contentHash' then raise exception 'memory_source_denied' using errcode='42501';end if;
  insert into app.memory_dependency values(c.organization_id,v.id,c.id,clock_timestamp()) on conflict do nothing;
 end loop;
 return null;
end $$;
drop trigger if exists memory_snapshot_dependency on app.context_snapshot;
create trigger memory_snapshot_dependency after insert on app.context_snapshot for each row execute function app.memory_snapshot_dependency();
create or replace function app.memory_affected_assertions(input_id uuid) returns table(id uuid)
language sql stable security definer set search_path=app,pg_temp as $$
 with recursive assertions(id) as (
 select v.assertion_id from app.memory_version v where v.id=input_id and v.organization_id=app.current_organization_id() and (v.owner_id=app.current_actor_id() or app.is_runtime_worker())
 union
 select child.assertion_id from assertions a join app.memory_version parent on parent.assertion_id=a.id
 join app.memory_dependency d on d.version_id=parent.id join app.memory_source s on s.conversation_id=d.conversation_id
 join app.memory_evidence e on e.source_id=s.id join app.memory_version child on child.id=e.version_id where child.organization_id=app.current_organization_id()
 ) select id from assertions
$$;
create or replace function app.memory_affected_conversations(input_id uuid) returns table(id uuid)
language sql stable security definer set search_path=app,pg_temp as $$
 select distinct s.conversation_id from app.memory_affected_assertions(input_id) a join app.memory_version v on v.assertion_id=a.id join app.memory_evidence e on e.version_id=v.id join app.memory_source s on s.id=e.source_id where s.conversation_id is not null
 union
 select distinct d.conversation_id from app.memory_affected_assertions(input_id) a join app.memory_version v on v.assertion_id=a.id join app.memory_dependency d on d.version_id=v.id
$$;
create or replace function app.memory_forget(input_id uuid,input_mode text,input_source boolean) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare v app.memory_version%rowtype;declare result uuid;declare aids uuid[];declare cids uuid[];
begin
 select * into v from app.memory_version where id=input_id and organization_id=app.current_organization_id() and (owner_id=app.current_actor_id() or app.is_runtime_worker());
 if v.id is null then raise exception 'memory_denied' using errcode='42501';end if;
 perform app.memory_domain_lock(v.organization_id,v.owner_id,v.object_scope);
 select * into v from app.memory_version where id=input_id and organization_id=app.current_organization_id() and (owner_id=app.current_actor_id() or app.is_runtime_worker()) for update;
 if v.id is null or input_mode not in('soft','hard') or input_mode is null or input_source is null then raise exception 'memory_denied' using errcode='42501';end if;
 select array_agg(id) into aids from app.memory_affected_assertions(input_id);
 select array_agg(id) into cids from app.memory_affected_conversations(input_id);
 update app.memory_version set status='revoked' where assertion_id=any(aids) and status<>'deleted';
 update app.memory_source set state='denied' where state<>'deleted' and (conversation_id=any(cids) or id in(select e.source_id from app.memory_evidence e join app.memory_version m on m.id=e.version_id where m.assertion_id=any(aids)));
 update app.context_conversation set memory_enabled=false,memory_denied=true,state='closed',closed_at=coalesce(closed_at,clock_timestamp()) where id=any(cids);
 update app.memory_work_item set state='cancelled' where source_id in(select s.id from app.memory_source s where s.state<>'ready' and s.organization_id=v.organization_id and s.owner_id=v.owner_id);
 insert into app.memory_delete_request(organization_id,owner_id,version_id,mode,include_source,state,inventory)
 values(v.organization_id,v.owner_id,v.id,input_mode,input_source,case when input_mode='soft' then 'completed' else 'requested' end,
 jsonb_build_object('assertions',to_jsonb(aids),'conversations',coalesce(to_jsonb(cids),'[]'::jsonb))) returning id into result;
 return result;
end $$;
create or replace function app.memory_erased(input_id uuid,input_inventory jsonb,input_rebuilt jsonb) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
declare d app.memory_delete_request%rowtype;declare aids uuid[];declare cids uuid[];declare cid uuid;declare pair record;
begin
 select * into d from app.memory_delete_request where id=input_id and organization_id=app.current_organization_id() and (owner_id=app.current_actor_id() or app.is_runtime_worker()) for update;
 if d.id is null or d.mode<>'hard' or input_inventory->>'state' is distinct from 'local_stores_erased' then raise exception 'memory_denied' using errcode='42501';end if;
 select array_agg(value::uuid) into aids from jsonb_array_elements_text(d.inventory->'assertions');
 select array_agg(value::uuid) into cids from jsonb_array_elements_text(d.inventory->'conversations');
 -- Backward-compatible delete requests created before the dependency ledger.
 if aids is null then select array_agg(id) into aids from app.memory_affected_assertions(d.version_id);end if;
 if cids is null then select array_agg(id) into cids from app.memory_affected_conversations(d.version_id);end if;
 foreach cid in array coalesce(cids,'{}'::uuid[]) loop
  if d.include_source or exists(select 1 from app.memory_dependency dep join app.memory_version v on v.id=dep.version_id where dep.conversation_id=cid and v.assertion_id=any(aids)) then
   update app.context_message set payload='{"erased":true}',content_hash=encode(public.digest(convert_to('{"erased":true}','UTF8'),'sha256'),'hex') where conversation_id=cid;
   update app.context_working_state set payload='{"goal":"Erased source"}',compaction=null where conversation_id=cid;
   update app.context_snapshot set projection='{"erased":true}',state='invalidated' where conversation_id=cid;
   delete from app.model_exchange where context_snapshot_id in(select id from app.context_snapshot where conversation_id=cid);
  end if;
 end loop;
 update app.context_snapshot set projection='{"erased":true}',state='invalidated' where id in(
 select s.id from app.context_snapshot s,jsonb_array_elements(coalesce(s.projection->'memoryItems','[]')) item
 join app.memory_version v on v.id=(item->>'versionId')::uuid where v.assertion_id=any(aids));
 delete from app.model_exchange where context_snapshot_id in(select id from app.context_snapshot where organization_id=d.organization_id and projection='{"erased":true}');
 update app.memory_source set explicit_text=null,state='deleted' where conversation_id=any(cids) or id in(select e.source_id from app.memory_evidence e join app.memory_version v on v.id=e.version_id where v.assertion_id=any(aids));
 update app.memory_version set body=null,status='deleted' where assertion_id=any(aids);
 update app.memory_projection set external_ids='[]',state='deleted' where version_id in(select id from app.memory_version where assertion_id=any(aids));
 for pair in select key,value from jsonb_each(input_rebuilt) loop
  update app.memory_projection set external_ids=pair.value->'ids',updated_at=clock_timestamp() where version_id=pair.key::uuid and organization_id=d.organization_id and version_id in(select id from app.memory_version where owner_id=d.owner_id and object_scope=(select object_scope from app.memory_version where id=d.version_id));
 end loop;
 update app.memory_delete_request set state='backup_pending',inventory=d.inventory||input_inventory||jsonb_build_object('sql','erased','context','erased','source',case when include_source then 'erased' else 'retained_by_scope' end) where id=d.id;
end $$;
create or replace function app.memory_cancel(input_work uuid,input_owner text,input_generation integer) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if not app.is_runtime_worker() then raise exception 'memory_denied' using errcode='42501';end if;
 perform app.memory_forget(w.version_id,'hard',false) from app.memory_work_item w where w.id=input_work and w.organization_id=app.current_organization_id() and w.owner=input_owner and w.generation=input_generation and w.state='leased' and w.version_id is not null;
 update app.memory_work_item set state='cancelled',lease_until=null,error_code='cancelled' where id=input_work and organization_id=app.current_organization_id() and owner=input_owner and generation=input_generation and state='leased';
end $$;
create or replace function app.memory_expire() returns integer
language plpgsql security definer set search_path=app,pg_temp as $$
declare target record;declare source record;declare n integer:=0;
begin
 if not app.is_runtime_worker() then raise exception 'memory_denied' using errcode='42501';end if;
 for target in select distinct on(v.assertion_id) v.id from app.memory_version v where v.organization_id=app.current_organization_id() and v.status<>'deleted'
 and (v.expires_at<=clock_timestamp() or exists(select 1 from app.memory_evidence e join app.memory_source s on s.id=e.source_id where e.version_id=v.id and s.created_at<clock_timestamp()-interval '7 days'))
 and not exists(select 1 from app.memory_delete_request d join app.memory_version old on old.id=d.version_id where old.assertion_id=v.assertion_id and d.mode='hard') loop
  perform app.memory_forget(target.id,'hard',true);n:=n+1;
 end loop;
 for source in select * from app.memory_source where organization_id=app.current_organization_id() and state='ready' and created_at<clock_timestamp()-interval '7 days'
 and not exists(select 1 from app.memory_evidence e where e.source_id=memory_source.id) loop
  update app.memory_source set state='deleted',explicit_text=null where id=source.id;
  update app.memory_work_item set state='cancelled',error_code='retention_expired' where source_id=source.id;
  if source.conversation_id is not null then
   update app.context_conversation set memory_enabled=false,memory_denied=true,state='closed',closed_at=coalesce(closed_at,clock_timestamp()) where id=source.conversation_id;
   update app.context_message set payload='{"erased":true}',content_hash=encode(public.digest(convert_to('{"erased":true}','UTF8'),'sha256'),'hex') where conversation_id=source.conversation_id;
   update app.context_working_state set payload='{"goal":"Erased source"}',compaction=null where conversation_id=source.conversation_id;
   update app.context_snapshot set projection='{"erased":true}',state='invalidated' where conversation_id=source.conversation_id;
   delete from app.model_exchange where context_snapshot_id in(select id from app.context_snapshot where conversation_id=source.conversation_id);
  end if;
  n:=n+1;
 end loop;
 return n;
end $$;
create or replace function app.memory_replay_deletions(input_ledger jsonb) returns integer
language plpgsql security definer set search_path=app,pg_temp as $$
declare entry jsonb;declare target record;declare n integer:=0;
begin
 if not app.is_runtime_worker() or jsonb_typeof(input_ledger)<>'array' or octet_length(input_ledger::text)>1048576 then raise exception 'memory_denied' using errcode='42501';end if;
 perform app.memory_begin_recovery();
 for entry in select value from jsonb_array_elements(input_ledger) loop
  if (entry->>'organization_id')::uuid<>app.current_organization_id() then raise exception 'memory_denied' using errcode='42501';end if;
  if not exists(select 1 from app.memory_replay_log where organization_id=app.current_organization_id() and request_id=(entry->>'id')::uuid) then
   for target in select distinct on(v.assertion_id) v.id from app.memory_version v where v.organization_id=app.current_organization_id() and v.owner_id=(entry->>'owner_id')::uuid
    and (v.id=(entry->>'version_id')::uuid or v.assertion_id in(select value::uuid from jsonb_array_elements_text(entry->'inventory'->'assertions'))) loop
    perform app.memory_forget(target.id,entry->>'mode',(entry->>'include_source')::boolean);n:=n+1;
   end loop;
   insert into app.memory_replay_log values(app.current_organization_id(),(entry->>'id')::uuid,clock_timestamp());
  end if;
 end loop;
 update app.memory_recovery_state set ledger_applied=true where organization_id=app.current_organization_id();
 return n;
end $$;
revoke all on function app.memory_source_base_readable(uuid),app.memory_source_ancestors(uuid),app.memory_snapshot_dependency(),app.memory_affected_assertions(uuid),app.memory_affected_conversations(uuid),app.memory_cancel(uuid,text,integer),app.memory_expire(),app.memory_replay_deletions(jsonb) from public;
grant execute on function app.memory_source_ancestors(uuid),app.memory_cancel(uuid,text,integer),app.memory_expire(),app.memory_replay_deletions(jsonb) to supplyagent_api;
create or replace function app.memory_accept(input_work uuid,input_owner text,input_generation integer,input_epoch bigint,input_body text) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare w app.memory_work_item%rowtype; declare s app.memory_source%rowtype; declare aid uuid; declare v integer; declare result uuid;
begin
 w:=app.memory_guard(input_work,input_owner,input_generation);
 if w.version_id is not null then return w.version_id; end if;
 select * into s from app.memory_source where id=w.source_id;
 if s.conversation_id is not null and (select source_epoch from app.context_conversation where id=s.conversation_id)<>input_epoch then
 raise exception 'memory_source_changed' using errcode='42501'; end if;
 if input_body is null or octet_length(input_body) not between 1 and 32768 then raise exception 'memory_invalid' using errcode='22023'; end if;
 aid:=coalesce(s.assertion_id,s.conversation_id,s.id);
 perform pg_advisory_xact_lock(hashtextextended(aid::text,0));
 select coalesce(max(version),0)+1 into v from app.memory_version where assertion_id=aid;
 insert into app.memory_version(organization_id,owner_id,assertion_id,version,previous_id,object_scope,content_kind,trust,status,body,content_hash,summary_manifest)
 values(s.organization_id,s.owner_id,aid,v,(select id from app.memory_version where assertion_id=aid order by version desc limit 1),s.object_scope,
 case when s.conversation_id is null then 'stable_memory' else 'process_summary' end,
 case when s.conversation_id is null then 'unconfirmed' else 'generated_reference' end,
 case when s.conversation_id is null then 'candidate' else 'ready' end,input_body,encode(public.digest(convert_to(input_body,'UTF8'),'sha256'),'hex'),app.memory_source_manifest(s.id)||coalesce((select jsonb_build_object('modelPolicy','grounded-process-v1','operationId',id) from app.memory_operation where work_id=w.id and purpose='summarize' and state='completed' order by created_at desc limit 1),'{}'::jsonb)) returning id into result;
 insert into app.memory_evidence values(s.organization_id,result,s.id);
 update app.memory_work_item set version_id=result where id=w.id;
 return result;
end $$;
create or replace function app.memory_correct(input_id uuid,input_text text) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare v app.memory_version%rowtype; declare sid uuid;
begin
 select * into v from app.memory_version where id=input_id and organization_id=app.current_organization_id() and owner_id=app.current_actor_id();
 if v.id is null then raise exception 'memory_denied' using errcode='42501';end if;
 perform app.memory_domain_lock(v.organization_id,v.owner_id,v.object_scope);
 select * into v from app.memory_version where id=input_id and organization_id=app.current_organization_id() and owner_id=app.current_actor_id() for update;
 if v.id is null or v.status not in('ready','candidate','active') or v.expires_at<=clock_timestamp()
 or exists(select 1 from app.memory_evidence e where e.version_id=v.id and not app.memory_source_readable(e.source_id))
 or input_text is null or octet_length(input_text) not between 1 and 32768 then raise exception 'memory_denied' using errcode='42501'; end if;
 update app.memory_version set status='revoked' where assertion_id=v.assertion_id;
 insert into app.memory_source(organization_id,owner_id,object_scope,explicit_text,source_hash,assertion_id)
 values(v.organization_id,v.owner_id,v.object_scope,input_text,encode(public.digest(convert_to(input_text,'UTF8'),'sha256'),'hex'),v.assertion_id) returning id into sid;
 insert into app.memory_work_item(organization_id,source_id) values(v.organization_id,sid);
 return sid;
end $$;
create or replace function app.memory_reserve(input_namespace text,input_purpose text,input_model text,input_hash text,input_bound integer,input_work uuid,input_owner text,input_generation integer,input_delete uuid,input_epoch bigint) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare result uuid;declare used bigint;
begin
 if input_delete is null and not app.memory_recovery_ready() then raise exception 'memory_denied' using errcode='42501';end if;
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
commit;
