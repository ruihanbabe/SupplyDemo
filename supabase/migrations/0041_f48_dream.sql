-- M3: lossless synthetic consolidation using the existing durable work/lease ledger.
begin;
alter table app.memory_version add column if not exists temperature text not null default 'hot' check(temperature in('hot','cold'));
create table if not exists app.memory_derivation(
 organization_id uuid not null references app.organization(id),source_id uuid not null references app.memory_source(id),
 version_id uuid not null references app.memory_version(id),primary key(source_id,version_id)
);
create table if not exists app.memory_dream_batch(
 source_id uuid primary key references app.memory_source(id),organization_id uuid not null references app.organization(id),
 owner_id uuid not null,object_scope text not null,input_hash text not null,policy text not null default 'lossless-dream-v1',
 evaluation jsonb not null,created_at timestamptz not null default clock_timestamp(),unique(organization_id,owner_id,object_scope,input_hash)
);
do $$ declare t text;begin
 foreach t in array array['memory_derivation','memory_dream_batch'] loop
 execute format('alter table app.%I enable row level security',t);
 execute format('alter table app.%I force row level security',t);
 execute format('drop policy if exists memory_private on app.%I',t);
 execute format('create policy memory_private on app.%I for select to supplyagent_api using(organization_id=app.current_organization_id() and exists(select 1 from app.memory_source s where s.id=source_id and (s.owner_id=app.current_actor_id() or app.is_runtime_worker())))',t);
 execute format('grant select on app.%I to supplyagent_api',t);
 end loop;
end $$;
create or replace function app.memory_source_ancestors(input_id uuid) returns table(id uuid)
language sql stable security definer set search_path=app,pg_temp as $$
 with recursive edges(child,parent) as (
 select s.id,e.source_id from app.memory_source s join app.memory_dependency d on d.conversation_id=s.conversation_id join app.memory_evidence e on e.version_id=d.version_id where s.organization_id=app.current_organization_id()
 union
 select d.source_id,e.source_id from app.memory_derivation d join app.memory_evidence e on e.version_id=d.version_id where d.organization_id=app.current_organization_id()
 ), sources(id) as (
 select s.id from app.memory_source s where s.id=input_id and s.organization_id=app.current_organization_id() and (s.owner_id=app.current_actor_id() or app.is_runtime_worker())
 union select e.parent from sources s join edges e on e.child=s.id
 ) select id from sources
$$;
create or replace function app.memory_source_readable(input_id uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select app.memory_source_base_readable(input_id) and (app.memory_recovery_ready() or app.is_runtime_worker())
 and not exists(select 1 from app.memory_source_ancestors(input_id) a where not app.memory_source_base_readable(a.id))
 and not exists(select 1 from app.memory_source_ancestors(input_id) a join app.memory_source s on s.id=a.id
 join app.memory_dependency d on d.conversation_id=s.conversation_id join app.memory_version v on v.id=d.version_id
 where v.status not in('ready','active') or v.expires_at<=clock_timestamp())
 and not exists(select 1 from app.memory_source_ancestors(input_id) a join app.memory_derivation d on d.source_id=a.id join app.memory_version v on v.id=d.version_id
 where v.status not in('ready','active') or v.expires_at<=clock_timestamp())
$$;
create or replace function app.memory_roots(input_id uuid) returns table(id uuid)
language sql stable security definer set search_path=app,pg_temp as $$
 select distinct a.id from app.memory_evidence e cross join lateral app.memory_source_ancestors(e.source_id) a
 join app.memory_source s on s.id=a.id where e.version_id=input_id and app.memory_version_readable(input_id)
 and not exists(select 1 from app.memory_derivation d where d.source_id=s.id)
 and not exists(select 1 from app.memory_dependency d where d.conversation_id=s.conversation_id)
$$;
create or replace function app.memory_schedule_dream(input_user uuid,input_scope text,input_enabled boolean) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare ids uuid[];declare roots uuid[];declare fingerprint text;declare sid uuid;declare body text;declare before_count integer;declare distinct_count integer;declare baseline integer;declare last_batch timestamptz;declare additions integer;
begin
 if input_enabled is distinct from true then return null;end if;
 if not app.is_runtime_worker() or not app.is_member(app.current_organization_id(),input_user) or not app.memory_recovery_ready() then raise exception 'memory_denied' using errcode='42501';end if;
 perform app.memory_domain_lock(app.current_organization_id(),input_user,input_scope);
 select array_agg(id order by id) into ids from (select v.id from app.memory_version v join app.memory_projection p on p.version_id=v.id
 where v.organization_id=app.current_organization_id() and v.owner_id=input_user and v.object_scope=input_scope and p.state='indexed' and app.memory_version_readable(v.id)
 and not exists(select 1 from app.memory_evidence e join app.memory_derivation d on d.source_id=e.source_id where e.version_id=v.id)
 order by v.created_at desc limit 30) inputs;
 if coalesce(cardinality(ids),0)<2 then return null;end if;
 select array_agg(distinct r.id order by r.id) into roots from unnest(ids) v cross join lateral app.memory_roots(v) r;
 if coalesce(cardinality(roots),0)<2 then return null;end if;
 fingerprint:=encode(public.digest(convert_to(roots::text||':lossless-dream-v1','UTF8'),'sha256'),'hex');
 select source_id into sid from app.memory_dream_batch where organization_id=app.current_organization_id() and owner_id=input_user and object_scope=input_scope and input_hash=fingerprint;
 if sid is not null then return sid;end if;
 select max(created_at) into last_batch from app.memory_dream_batch where organization_id=app.current_organization_id() and owner_id=input_user and object_scope=input_scope;
 if last_batch::date=clock_timestamp()::date then
  select count(*) into additions from app.memory_version v join app.memory_projection p on p.version_id=v.id where v.organization_id=app.current_organization_id()
  and v.owner_id=input_user and v.object_scope=input_scope and v.created_at>last_batch and p.state='indexed' and app.memory_version_readable(v.id)
  and not exists(select 1 from app.memory_evidence e join app.memory_derivation d on d.source_id=e.source_id where e.version_id=v.id);
  if additions<50 then return null;end if;
 end if;
 -- Render exact blocks with every qualifier, never a stable rule or a business update.
 select string_agg(block,E'\n\n' order by block),count(*) into body,distinct_count from (
 select distinct '[historical reference; not current business fact] '||jsonb_build_object('eventAt',event_at,'validFrom',valid_from,'validTo',valid_to,'conditions',conditions)::text||E'\n'||v.body as block from app.memory_version v where v.id=any(ids)) blocks;
 before_count:=cardinality(ids);baseline:=least(before_count,5);
 -- A lossless fixture gate: either deduplicate exact blocks or cover more than five slots.
 if octet_length(body)>8000 or (distinct_count=before_count and before_count<=5) then return null;end if;
 insert into app.memory_source(organization_id,owner_id,object_scope,explicit_text,source_hash)
 values(app.current_organization_id(),input_user,input_scope,body,encode(public.digest(convert_to(body,'UTF8'),'sha256'),'hex')) returning id into sid;
 insert into app.memory_dream_batch(source_id,organization_id,owner_id,object_scope,input_hash,evaluation)
 values(sid,app.current_organization_id(),input_user,input_scope,fingerprint,jsonb_build_object('policy','lossless-dream-v1','inputs',before_count,'distinctBlocks',distinct_count,'baselineSlots',baseline,'consolidatedSlots',1,'lossless',true,'syntheticOnly',true));
 insert into app.memory_derivation select app.current_organization_id(),sid,unnest(ids);
 insert into app.memory_work_item(organization_id,source_id) values(app.current_organization_id(),sid);
 return sid;
end $$;
create or replace function app.memory_derived_version() returns trigger
language plpgsql security definer set search_path=app,pg_temp as $$
declare sid uuid;
begin
 select s.id into sid from app.memory_source s where s.id=new.assertion_id and s.organization_id=new.organization_id and s.owner_id=new.owner_id;
 if sid is null then return new;end if;
 if exists(select 1 from app.memory_derivation where source_id=sid) then
  if not app.memory_source_readable(sid) then raise exception 'memory_source_denied' using errcode='42501';end if;
  if exists(select 1 from app.memory_dream_batch where source_id=sid) then
   new.content_kind:='process_summary';new.trust:='generated_reference';new.status:='ready';new.confirmed_by:=null;
  end if;
  new.summary_manifest:=new.summary_manifest||jsonb_build_object('derivedFrom',(select jsonb_agg(version_id order by version_id) from app.memory_derivation where source_id=sid),'independentSources',(select count(distinct a.id) from app.memory_derivation d cross join lateral app.memory_roots(d.version_id) a where d.source_id=sid));
 end if;
 return new;
end $$;
create or replace function app.memory_derived_edges() returns trigger
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 insert into app.memory_evidence select new.organization_id,new.id,e.source_id from app.memory_derivation d join app.memory_evidence e on e.version_id=d.version_id where d.source_id=new.assertion_id on conflict do nothing;
 insert into app.memory_relation select new.organization_id,new.id,d.version_id,'derived_from',app.current_actor_id(),clock_timestamp() from app.memory_derivation d where d.source_id=new.assertion_id on conflict do nothing;
 return null;
end $$;
drop trigger if exists memory_derived_version on app.memory_version;
create trigger memory_derived_version before insert on app.memory_version for each row execute function app.memory_derived_version();
drop trigger if exists memory_derived_edges on app.memory_version;
create trigger memory_derived_edges after insert on app.memory_version for each row execute function app.memory_derived_edges();
create or replace function app.memory_lesson_candidate(input_id uuid,input_text text) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare v app.memory_version%rowtype;declare sid uuid;
begin
 select * into v from app.memory_version where id=input_id and organization_id=app.current_organization_id() and owner_id=app.current_actor_id();
 if app.is_runtime_worker() or v.id is null or not app.memory_version_readable(v.id) or input_text is null or length(input_text)<1 or position(input_text in v.body)=0 then raise exception 'memory_source_denied' using errcode='42501';end if;
 perform app.memory_domain_lock(v.organization_id,v.owner_id,v.object_scope);
 if not app.memory_version_readable(v.id) then raise exception 'memory_source_denied' using errcode='42501';end if;
 select s.id into sid from app.memory_source s join app.memory_derivation d on d.source_id=s.id where d.version_id=v.id and s.explicit_text=input_text and s.state='ready' limit 1;
 if sid is not null then return sid;end if;
 sid:=app.memory_remember(v.object_scope,input_text);
 insert into app.memory_derivation values(v.organization_id,sid,v.id);
 return sid;
end $$;
create or replace function app.memory_cool(input_id uuid,input_cold boolean) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if input_cold is null or not app.memory_version_readable(input_id) then raise exception 'memory_source_denied' using errcode='42501';end if;
 update app.memory_version set temperature=case when input_cold then 'cold' else 'hot' end where id=input_id and organization_id=app.current_organization_id() and (owner_id=app.current_actor_id() or app.is_runtime_worker());
end $$;
create or replace function app.memory_task_retrospective() returns trigger
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if new.status in('completed','failed','cancelled') and new.status is distinct from old.status then
  update app.context_conversation set state='closed',closed_at=coalesce(closed_at,clock_timestamp()) where origin_job_id=new.id and memory_enabled;
 end if;
 return null;
end $$;
drop trigger if exists memory_task_retrospective on app.runtime_job;
create trigger memory_task_retrospective after update of status on app.runtime_job for each row execute function app.memory_task_retrospective();
revoke all on function app.memory_roots(uuid),app.memory_schedule_dream(uuid,text,boolean),app.memory_derived_version(),app.memory_derived_edges(),app.memory_lesson_candidate(uuid,text),app.memory_cool(uuid,boolean),app.memory_task_retrospective() from public;
grant execute on function app.memory_roots(uuid),app.memory_schedule_dream(uuid,text,boolean),app.memory_lesson_candidate(uuid,text),app.memory_cool(uuid,boolean) to supplyagent_api;
create or replace function app.memory_forget(input_id uuid,input_mode text,input_source boolean) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare v app.memory_version%rowtype;declare result uuid;declare aids uuid[];declare cids uuid[];declare sids uuid[];
begin
 select * into v from app.memory_version where id=input_id and organization_id=app.current_organization_id() and (owner_id=app.current_actor_id() or app.is_runtime_worker());
 if v.id is null then raise exception 'memory_denied' using errcode='42501';end if;
 perform app.memory_domain_lock(v.organization_id,v.owner_id,v.object_scope);
 select * into v from app.memory_version where id=input_id and organization_id=app.current_organization_id() and (owner_id=app.current_actor_id() or app.is_runtime_worker()) for update;
 if v.id is null or input_mode not in('soft','hard') or input_mode is null or input_source is null then raise exception 'memory_denied' using errcode='42501';end if;
 select array_agg(id) into aids from app.memory_affected_assertions(input_id);
 select array_agg(id) into cids from app.memory_affected_conversations(input_id);
 select array_agg(distinct source_id) into sids from (
 select e.source_id from app.memory_evidence e join app.memory_version m on m.id=e.version_id where m.assertion_id=any(aids)
 union select d.source_id from app.memory_derivation d join app.memory_version m on m.id=d.version_id where m.assertion_id=any(aids)) pending_and_accepted;
 update app.memory_source set state='denied' where id=any(sids) and state<>'deleted';
 update app.memory_version set status='revoked' where assertion_id=any(aids) and status<>'deleted';
 update app.memory_source set state='denied' where state<>'deleted' and (conversation_id=any(cids) or id in(select e.source_id from app.memory_evidence e join app.memory_version m on m.id=e.version_id where m.assertion_id=any(aids)));
 update app.context_conversation set memory_enabled=false,memory_denied=true,state='closed',closed_at=coalesce(closed_at,clock_timestamp()) where id=any(cids);
 update app.memory_work_item set state='cancelled' where source_id in(select s.id from app.memory_source s where s.state<>'ready' and s.organization_id=v.organization_id and s.owner_id=v.owner_id);
 insert into app.memory_delete_request(organization_id,owner_id,version_id,mode,include_source,state,inventory)
 values(v.organization_id,v.owner_id,v.id,input_mode,input_source,case when input_mode='soft' then 'completed' else 'requested' end,
 jsonb_build_object('sources',coalesce(to_jsonb(sids),'[]'::jsonb),'assertions',to_jsonb(aids),'conversations',coalesce(to_jsonb(cids),'[]'::jsonb))) returning id into result;
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
 update app.memory_source set explicit_text=null,state='deleted' where id in(select value::uuid from jsonb_array_elements_text(coalesce(d.inventory->'sources','[]'))) or conversation_id=any(cids) or id in(select e.source_id from app.memory_evidence e join app.memory_version v on v.id=e.version_id where v.assertion_id=any(aids));
 update app.memory_version set body=null,status='deleted' where assertion_id=any(aids);
 update app.memory_projection set external_ids='[]',state='deleted' where version_id in(select id from app.memory_version where assertion_id=any(aids));
 for pair in select key,value from jsonb_each(input_rebuilt) loop
  update app.memory_projection set external_ids=pair.value->'ids',updated_at=clock_timestamp() where version_id=pair.key::uuid and organization_id=d.organization_id and version_id in(select id from app.memory_version where owner_id=d.owner_id and object_scope=(select object_scope from app.memory_version where id=d.version_id));
 end loop;
 update app.memory_delete_request set state='backup_pending',inventory=d.inventory||input_inventory||jsonb_build_object('sql','erased','context','erased','source',case when include_source then 'erased' else 'retained_by_scope' end) where id=d.id;
end $$;
commit;
