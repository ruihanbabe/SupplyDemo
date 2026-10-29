-- F48 M1: private canonical versions and durable work; synthetic opt-in only.
begin;
alter table app.context_conversation add column if not exists memory_enabled boolean not null default false;
alter table app.context_conversation add column if not exists memory_denied boolean not null default false;
create table if not exists app.memory_source(
 id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id),
 owner_id uuid not null,conversation_id uuid references app.context_conversation(id),watermark bigint,
 object_scope text not null,source_hash text not null,explicit_text text,assertion_id uuid,
 state text not null default 'ready' check(state in('ready','denied','deleted')),synthetic boolean not null default true check(synthetic),
 created_at timestamptz not null default clock_timestamp(),unique(conversation_id,watermark)
);
create table if not exists app.memory_version(
 id uuid primary key default gen_random_uuid(),organization_id uuid not null references app.organization(id),owner_id uuid not null,
 assertion_id uuid not null,version integer not null,previous_id uuid references app.memory_version(id),object_scope text not null,
 content_kind text not null check(content_kind in('process_summary','stable_memory')),
 trust text not null check(trust in('generated_reference','unconfirmed','confirmed')),
 status text not null check(status in('ready','candidate','active','superseded','revoked','deleted','quarantined')),
 body text,content_hash text not null,confirmed_by uuid,created_at timestamptz not null default clock_timestamp(),
 expires_at timestamptz not null default clock_timestamp()+interval '7 days',unique(assertion_id,version)
);
create index if not exists memory_version_scope on app.memory_version(organization_id,owner_id,object_scope,status,expires_at);
create table if not exists app.memory_evidence(
 organization_id uuid not null references app.organization(id),version_id uuid not null references app.memory_version(id),
 source_id uuid not null references app.memory_source(id),primary key(version_id,source_id)
);
create index if not exists memory_source_dependents on app.memory_evidence(source_id);
create table if not exists app.memory_work_item(
 id uuid primary key default gen_random_uuid(),organization_id uuid not null references app.organization(id),
 source_id uuid not null references app.memory_source(id),version_id uuid references app.memory_version(id),
 state text not null default 'pending' check(state in('pending','leased','indexed','review_pending','quarantined','cancelled')),
 owner text,generation integer not null default 0,lease_until timestamptz,due_at timestamptz not null default clock_timestamp(),
 error_code text,created_at timestamptz not null default clock_timestamp(),unique(source_id)
);
create index if not exists memory_work_due on app.memory_work_item(organization_id,state,due_at);
create table if not exists app.memory_projection(
 version_id uuid primary key references app.memory_version(id),organization_id uuid not null references app.organization(id),
 external_ids jsonb not null,namespace text not null,backend_version text not null,content_hash text not null,
 state text not null check(state in('indexed','retired','deleted')),updated_at timestamptz not null default clock_timestamp()
);
create table if not exists app.memory_delete_request(
 id uuid primary key default gen_random_uuid(),organization_id uuid not null references app.organization(id),owner_id uuid not null,
 version_id uuid not null references app.memory_version(id),mode text not null check(mode in('soft','hard')),
 include_source boolean not null,state text not null check(state in('requested','backup_pending','completed')),
 inventory jsonb not null default '{}',created_at timestamptz not null default clock_timestamp()
);
create or replace function app.memory_source_readable(input_id uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select exists(select 1 from app.memory_source s where s.id=input_id and s.organization_id=app.current_organization_id()
 and s.state='ready' and (s.owner_id=app.current_actor_id() or app.is_runtime_worker())
 and app.is_member(s.organization_id,s.owner_id) and (s.conversation_id is null or app.context_can_read(s.conversation_id)))
$$;
create or replace function app.memory_version_readable(input_id uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select exists(select 1 from app.memory_version v where v.id=input_id and v.organization_id=app.current_organization_id()
 and (v.owner_id=app.current_actor_id() or app.is_runtime_worker()) and app.is_member(v.organization_id,v.owner_id)
 and v.status in('ready','active') and v.expires_at>clock_timestamp()
 and exists(select 1 from app.memory_evidence e where e.version_id=v.id)
 and not exists(select 1 from app.memory_evidence e where e.version_id=v.id and not app.memory_source_readable(e.source_id)))
$$;
create or replace function app.memory_enable(input_id uuid,input_synthetic boolean) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
declare c app.context_conversation%rowtype;
begin
 c:=app.context_guard(input_id);
 if input_synthetic is distinct from true or c.memory_denied then raise exception 'memory_denied' using errcode='42501'; end if;
 update app.context_conversation set memory_enabled=true where id=c.id;
end $$;
create or replace function app.memory_enable_worker(input_id uuid,input_job uuid,input_owner text,input_generation integer) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
declare c app.context_conversation%rowtype;
begin
 c:=app.context_guard(input_id,input_job,input_owner,input_generation);
 if c.memory_denied then return; end if;
 update app.context_conversation set memory_enabled=true where id=c.id;
end $$;
revoke all on function app.memory_enable_worker(uuid,uuid,text,integer) from public;
grant execute on function app.memory_enable_worker(uuid,uuid,text,integer) to supplyagent_api;
create or replace function app.memory_capture() returns trigger
language plpgsql security definer set search_path=app,pg_temp as $$
declare sid uuid; declare h text; declare scope text;
begin
 if not new.memory_enabled or new.memory_denied or new.committed_watermark=0 then return null; end if;
 if new.committed_watermark>old.committed_watermark then
  select encode(public.digest(convert_to(jsonb_agg(jsonb_build_object('ordinal',ordinal,'hash',content_hash) order by ordinal)::text,'UTF8'),'sha256'),'hex') into h
   from app.context_message where conversation_id=new.id and ordinal<=new.committed_watermark;
  scope:=jsonb_build_object('scope',new.scope,'role',regexp_replace(new.role,'_g[0-9]+$',''))::text;
  insert into app.memory_source(organization_id,owner_id,conversation_id,watermark,object_scope,source_hash)
   values(new.organization_id,new.owner_id,new.id,new.committed_watermark,scope,h) on conflict(conversation_id,watermark) do nothing returning id into sid;
  if sid is not null then insert into app.memory_work_item(organization_id,source_id,due_at) values(new.organization_id,sid,clock_timestamp()+interval '15 seconds'); end if;
 end if;
 if new.state='closed' then
  update app.memory_work_item w set due_at=clock_timestamp() from app.memory_source s where w.source_id=s.id and s.conversation_id=new.id and w.state='pending';
 end if;
 return null;
end $$;
drop trigger if exists memory_capture on app.context_conversation;
create trigger memory_capture after update of committed_watermark,state on app.context_conversation for each row execute function app.memory_capture();
create or replace function app.memory_remember(input_scope text,input_text text) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare sid uuid;
begin
 if not app.is_member(app.current_organization_id(),app.current_actor_id()) or app.is_runtime_worker()
 or length(input_scope) not between 1 and 1024 or length(input_text) not between 1 and 8000 then raise exception 'memory_denied' using errcode='42501'; end if;
 insert into app.memory_source(organization_id,owner_id,object_scope,explicit_text,source_hash)
 values(app.current_organization_id(),app.current_actor_id(),input_scope,input_text,encode(public.digest(convert_to(input_text,'UTF8'),'sha256'),'hex')) returning id into sid;
 insert into app.memory_work_item(organization_id,source_id) values(app.current_organization_id(),sid);
 return sid;
end $$;
create or replace function app.memory_claim(input_owner text) returns setof app.memory_work_item
language plpgsql security definer set search_path=app,pg_temp as $$
declare wid uuid;
begin
 if not app.is_runtime_worker() or input_owner is null or length(input_owner) not between 1 and 100 then raise exception 'memory_denied' using errcode='42501'; end if;
 select id into wid from app.memory_work_item where organization_id=app.current_organization_id()
 and ((state='pending' and due_at<=clock_timestamp()) or (state='leased' and lease_until<=clock_timestamp()))
 order by created_at for update skip locked limit 1;
 return query update app.memory_work_item set state='leased',owner=input_owner,generation=generation+1,lease_until=clock_timestamp()+interval '60 seconds' where id=wid returning *;
end $$;
create or replace function app.memory_guard(input_id uuid,input_owner text,input_generation integer) returns app.memory_work_item
language plpgsql security definer set search_path=app,pg_temp as $$
declare w app.memory_work_item%rowtype;
begin
 select * into w from app.memory_work_item where id=input_id and organization_id=app.current_organization_id() for update;
 if input_owner is null or input_generation is null or not app.is_runtime_worker() or w.id is null or w.state<>'leased' or w.owner is distinct from input_owner or w.generation<>input_generation
 or w.lease_until<=clock_timestamp() then raise exception 'memory_lease_lost' using errcode='42501'; end if;
 if not app.memory_source_readable(w.source_id) then raise exception 'memory_source_denied' using errcode='42501'; end if;
 -- A delayed prefix must never replace an already indexed, more complete source range.
 if exists(select 1 from app.memory_source s join app.memory_source newer on newer.conversation_id=s.conversation_id and newer.watermark>s.watermark
 join app.memory_evidence e on e.source_id=newer.id join app.memory_projection p on p.version_id=e.version_id and p.state='indexed'
 where s.id=w.source_id) then raise exception 'memory_source_changed' using errcode='42501'; end if;
 return w;
end $$;
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
 insert into app.memory_version(organization_id,owner_id,assertion_id,version,previous_id,object_scope,content_kind,trust,status,body,content_hash)
 values(s.organization_id,s.owner_id,aid,v,(select id from app.memory_version where assertion_id=aid order by version desc limit 1),s.object_scope,
 case when s.conversation_id is null then 'stable_memory' else 'process_summary' end,
 case when s.conversation_id is null then 'unconfirmed' else 'generated_reference' end,
 case when s.conversation_id is null then 'candidate' else 'ready' end,input_body,encode(public.digest(convert_to(input_body,'UTF8'),'sha256'),'hex')) returning id into result;
 insert into app.memory_evidence values(s.organization_id,result,s.id);
 update app.memory_work_item set version_id=result where id=w.id;
 return result;
end $$;
create or replace function app.memory_confirm(input_id uuid) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if not exists(select 1 from app.memory_version v where v.id=input_id and v.organization_id=app.current_organization_id()
 and v.owner_id=app.current_actor_id() and v.status='candidate' and not exists(select 1 from app.memory_evidence e where e.version_id=v.id and not app.memory_source_readable(e.source_id))) then raise exception 'memory_denied' using errcode='42501'; end if;
 update app.memory_version set status='active',trust='confirmed',confirmed_by=app.current_actor_id() where id=input_id;
 update app.memory_work_item set state='pending',due_at=clock_timestamp() where version_id=input_id;
end $$;
create or replace function app.memory_indexed(input_work uuid,input_owner text,input_generation integer,input_epoch bigint,input_ids jsonb,input_namespace text,input_backend text,input_hash text) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
declare w app.memory_work_item%rowtype; declare s app.memory_source%rowtype; declare v app.memory_version%rowtype;
begin
 w:=app.memory_guard(input_work,input_owner,input_generation);
 select * into s from app.memory_source where id=w.source_id;
 if s.conversation_id is not null and (select source_epoch from app.context_conversation where id=s.conversation_id)<>input_epoch then raise exception 'memory_source_changed' using errcode='42501'; end if;
 select * into v from app.memory_version where id=w.version_id;
 if v.id is null or v.status not in('ready','active') or v.content_hash is distinct from input_hash or jsonb_typeof(input_ids)<>'array' or jsonb_array_length(input_ids) not between 1 and 30 then raise exception 'memory_invalid' using errcode='22023'; end if;
 insert into app.memory_projection values(v.id,v.organization_id,input_ids,input_namespace,input_backend,input_hash,'indexed',clock_timestamp())
 on conflict(version_id) do update set external_ids=excluded.external_ids,state='indexed',updated_at=clock_timestamp();
 update app.memory_version set status='superseded' where assertion_id=v.assertion_id and version<v.version and status in('ready','active');
 update app.memory_projection set state='retired' where version_id in(select id from app.memory_version where assertion_id=v.assertion_id and version<v.version);
 update app.memory_work_item set state='indexed',lease_until=null where id=w.id;
end $$;
create or replace function app.memory_quarantine(input_work uuid,input_owner text,input_generation integer,input_code text) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if not app.is_runtime_worker() or not exists(select 1 from app.memory_work_item where id=input_work and organization_id=app.current_organization_id() and state='leased' and owner=input_owner and generation=input_generation and lease_until>clock_timestamp()) then raise exception 'memory_lease_lost' using errcode='42501'; end if;
 update app.memory_work_item set state='quarantined',error_code=input_code,lease_until=null where id=input_work;
end $$;
create or replace function app.memory_wait_review(input_work uuid,input_owner text,input_generation integer) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 perform app.memory_guard(input_work,input_owner,input_generation);
 update app.memory_work_item set state='review_pending',lease_until=null where id=input_work;
end $$;
revoke all on function app.memory_wait_review(uuid,text,integer) from public;
grant execute on function app.memory_wait_review(uuid,text,integer) to supplyagent_api;
create or replace function app.memory_correct(input_id uuid,input_text text) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare v app.memory_version%rowtype; declare sid uuid;
begin
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
create or replace function app.memory_forget(input_id uuid,input_mode text,input_source boolean) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare v app.memory_version%rowtype; declare result uuid; declare cid uuid;
begin
 select * into v from app.memory_version where id=input_id and organization_id=app.current_organization_id() and owner_id=app.current_actor_id() for update;
 if v.id is null or input_mode not in('soft','hard') or input_mode is null or input_source is null then raise exception 'memory_denied' using errcode='42501'; end if;
 update app.memory_version set status='revoked' where assertion_id=v.assertion_id;
 update app.memory_source set state='denied' where id in(select e.source_id from app.memory_evidence e join app.memory_version m on m.id=e.version_id where m.assertion_id=v.assertion_id);
 for cid in select distinct s.conversation_id from app.memory_evidence e join app.memory_version m on m.id=e.version_id join app.memory_source s on s.id=e.source_id where m.assertion_id=v.assertion_id and s.conversation_id is not null loop
  update app.context_conversation set memory_enabled=false,memory_denied=true where id=cid;
  update app.memory_source set state='denied' where conversation_id=cid;
 end loop;
 update app.memory_work_item set state='cancelled' where source_id in(select s.id from app.memory_source s where s.state<>'ready' and s.organization_id=v.organization_id and s.owner_id=v.owner_id);
 insert into app.memory_delete_request(organization_id,owner_id,version_id,mode,include_source,state)
 values(v.organization_id,v.owner_id,v.id,input_mode,input_source,case when input_mode='soft' then 'completed' else 'requested' end) returning id into result;
 return result;
end $$;
create or replace function app.memory_erased(input_id uuid,input_inventory jsonb,input_rebuilt jsonb) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
declare d app.memory_delete_request%rowtype; declare aid uuid; declare cid uuid; declare pair record; declare target record;
begin
 select * into d from app.memory_delete_request where id=input_id and organization_id=app.current_organization_id() and (owner_id=app.current_actor_id() or app.is_runtime_worker()) for update;
 if d.id is null or d.mode<>'hard' or input_inventory->>'state' is distinct from 'local_stores_erased' then raise exception 'memory_denied' using errcode='42501'; end if;
 select assertion_id into aid from app.memory_version where id=d.version_id;
 for target in select id from app.memory_version where assertion_id=aid loop
  update app.context_snapshot set projection='{"erased":true}',state='invalidated' where organization_id=d.organization_id and projection->'memoryItems' @> jsonb_build_array(jsonb_build_object('versionId',target.id::text));
  delete from app.model_exchange where organization_id=d.organization_id and position(target.id::text in request::text)>0;
 end loop;
 if d.include_source then
  for cid in select distinct s.conversation_id from app.memory_evidence e join app.memory_version v on v.id=e.version_id join app.memory_source s on s.id=e.source_id where v.assertion_id=aid and s.conversation_id is not null loop
   update app.context_message set payload='{"erased":true}',content_hash=encode(public.digest(convert_to('{"erased":true}','UTF8'),'sha256'),'hex') where conversation_id=cid;
   update app.context_working_state set payload='{"goal":"Erased source"}',compaction=null where conversation_id=cid;
   update app.context_snapshot set projection='{"erased":true}',state='invalidated' where conversation_id=cid;
   delete from app.model_exchange where context_snapshot_id in(select id from app.context_snapshot where conversation_id=cid);
  end loop;
 end if;
 update app.memory_source set explicit_text=null,state='deleted' where id in(select e.source_id from app.memory_evidence e join app.memory_version v on v.id=e.version_id where v.assertion_id=aid);
 update app.memory_version set body=null,status='deleted' where assertion_id=aid;
 update app.memory_projection set external_ids='[]',state='deleted' where version_id in(select id from app.memory_version where assertion_id=aid);
 for pair in select key,value from jsonb_each(input_rebuilt) loop
  update app.memory_projection set external_ids=pair.value->'ids',updated_at=clock_timestamp() where version_id=pair.key::uuid and organization_id=d.organization_id and version_id in(select id from app.memory_version where owner_id=d.owner_id and object_scope=(select object_scope from app.memory_version where id=d.version_id));
 end loop;
 update app.memory_delete_request set state='backup_pending',inventory=input_inventory||jsonb_build_object('sql','erased','context','erased','source',case when include_source then 'erased' else 'retained_by_scope' end) where id=d.id;
end $$;
revoke all on function app.memory_correct(uuid,text),app.memory_forget(uuid,text,boolean),app.memory_erased(uuid,jsonb,jsonb) from public;
grant execute on function app.memory_correct(uuid,text),app.memory_forget(uuid,text,boolean),app.memory_erased(uuid,jsonb,jsonb) to supplyagent_api;
-- Deny invalidates existing ready Context, regardless of the backend's eventual cleanup.
do $$ declare t text; begin
 foreach t in array array['memory_source','memory_version'] loop
 execute format('drop trigger if exists memory_epoch_change on app.%I',t);
 execute format('create trigger memory_epoch_change after update or delete on app.%I for each row execute function app.context_bump_epoch()',t);
 end loop;
end $$;
-- Functions are the only write seam. Source/version bodies remain private under RLS.
do $$ declare t text; begin
 foreach t in array array['memory_source','memory_version','memory_evidence','memory_work_item','memory_projection','memory_delete_request'] loop
 execute format('alter table app.%I enable row level security',t);
 execute format('alter table app.%I force row level security',t);
 execute format('revoke all on app.%I from public',t);
 execute format('grant select on app.%I to supplyagent_api',t);
 execute format('drop policy if exists memory_private on app.%I',t);
 end loop;
end $$;
create policy memory_private on app.memory_source for select to supplyagent_api using(organization_id=app.current_organization_id() and (owner_id=app.current_actor_id() or app.is_runtime_worker()));
create policy memory_private on app.memory_version for select to supplyagent_api using(organization_id=app.current_organization_id() and (owner_id=app.current_actor_id() or app.is_runtime_worker()));
create policy memory_private on app.memory_evidence for select to supplyagent_api using(organization_id=app.current_organization_id() and exists(select 1 from app.memory_version v where v.id=version_id));
create policy memory_private on app.memory_work_item for select to supplyagent_api using(organization_id=app.current_organization_id() and (app.is_runtime_worker() or exists(select 1 from app.memory_source s where s.id=source_id and s.owner_id=app.current_actor_id())));
create policy memory_private on app.memory_projection for select to supplyagent_api using(organization_id=app.current_organization_id() and exists(select 1 from app.memory_version v where v.id=version_id));
create policy memory_private on app.memory_delete_request for select to supplyagent_api using(organization_id=app.current_organization_id() and (owner_id=app.current_actor_id() or app.is_runtime_worker()));
revoke all on function app.memory_source_readable(uuid),app.memory_version_readable(uuid),app.memory_enable(uuid,boolean),app.memory_capture(),app.memory_remember(text,text),app.memory_claim(text),app.memory_guard(uuid,text,integer),app.memory_accept(uuid,text,integer,bigint,text),app.memory_confirm(uuid),app.memory_indexed(uuid,text,integer,bigint,jsonb,text,text,text),app.memory_quarantine(uuid,text,integer,text) from public;
grant execute on function app.memory_source_readable(uuid),app.memory_version_readable(uuid),app.memory_enable(uuid,boolean),app.memory_remember(text,text),app.memory_claim(text),app.memory_guard(uuid,text,integer),app.memory_accept(uuid,text,integer,bigint,text),app.memory_confirm(uuid),app.memory_indexed(uuid,text,integer,bigint,jsonb,text,text,text),app.memory_quarantine(uuid,text,integer,text) to supplyagent_api;
commit;
