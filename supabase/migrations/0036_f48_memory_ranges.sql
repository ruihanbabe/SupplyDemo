-- F48 M1: incremental closed-unit ranges and content-free summary coverage.
-- Only apply after 0035; source database deployment remains a separate decision.
begin;
alter table app.memory_source add column if not exists start_ordinal bigint not null default 1 check(start_ordinal>0);
alter table app.memory_version add column if not exists summary_manifest jsonb not null default '{}';
create or replace function app.memory_source_manifest(input_id uuid) returns jsonb
language sql stable security definer set search_path=app,pg_temp as $$
 select jsonb_build_object('strategy',case when s.conversation_id is null then 'explicit-command-v1' else 'verbatim-closed-units-v1' end,
 'sourceId',s.id,'sourceHash',s.source_hash,'start',s.start_ordinal,'end',s.watermark,
 'units',coalesce((select jsonb_agg(u.manifest order by u.first_ordinal) from (
 select min(m.ordinal) first_ordinal,jsonb_build_object('id',m.message_unit_id,'start',min(m.ordinal),'end',max(m.ordinal),
 'hash',encode(public.digest(convert_to(jsonb_agg(jsonb_build_object('ordinal',m.ordinal,'hash',m.content_hash) order by m.ordinal)::text,'UTF8'),'sha256'),'hex')) manifest
 from app.context_message m where m.conversation_id=s.conversation_id and m.ordinal between s.start_ordinal and s.watermark
 group by m.message_unit_id) u),'[]'::jsonb))
 from app.memory_source s where s.id=input_id and s.organization_id=app.current_organization_id() and app.memory_source_readable(s.id)
$$;
create or replace function app.memory_range_covered(input_version uuid,input_source uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select exists(select 1 from app.memory_evidence e join app.memory_source old on old.id=e.source_id
 join app.memory_source newer on newer.id=input_source and newer.organization_id=old.organization_id
 where e.version_id=input_version and old.organization_id=app.current_organization_id()
 and (old.id=newer.id or (old.conversation_id=newer.conversation_id and newer.start_ordinal<=old.start_ordinal and newer.watermark>=old.watermark)))
$$;
revoke all on function app.memory_source_manifest(uuid),app.memory_range_covered(uuid,uuid) from public;
create or replace function app.memory_capture() returns trigger
language plpgsql security definer set search_path=app,pg_temp as $$
declare sid uuid; declare h text; declare scope text; declare u record; declare r jsonb;
declare range_start bigint; declare range_end bigint; declare range_size bigint:=0; declare ranges jsonb:='[]';
begin
 if not new.memory_enabled or new.memory_denied or new.committed_watermark=0 then return null; end if;
 if new.committed_watermark>old.committed_watermark then
  -- Never cut an assistant/tool-result unit; an oversized unit is isolated by the Worker.
  for u in select message_unit_id,min(ordinal) first_ordinal,max(ordinal) last_ordinal,
   sum(octet_length(to_jsonb(m)::text)) bytes from app.context_message m
   where conversation_id=new.id and ordinal>old.committed_watermark and ordinal<=new.committed_watermark
   group by message_unit_id order by min(ordinal) loop
   if range_start is not null and range_size+u.bytes>6144 then
    ranges:=ranges||jsonb_build_array(jsonb_build_object('start',range_start,'end',range_end));
    range_start:=null;range_size:=0;
   end if;
   range_start:=coalesce(range_start,u.first_ordinal);range_end:=u.last_ordinal;range_size:=range_size+u.bytes;
  end loop;
  if range_start is not null then ranges:=ranges||jsonb_build_array(jsonb_build_object('start',range_start,'end',range_end));end if;
  scope:=jsonb_build_object('scope',new.scope,'role',regexp_replace(new.role,'_g[0-9]+$',''))::text;
  for r in select value from jsonb_array_elements(ranges) loop
   select encode(public.digest(convert_to(jsonb_agg(jsonb_build_object('ordinal',ordinal,'hash',content_hash) order by ordinal)::text,'UTF8'),'sha256'),'hex') into h
    from app.context_message where conversation_id=new.id and ordinal between (r->>'start')::bigint and (r->>'end')::bigint;
   insert into app.memory_source(organization_id,owner_id,conversation_id,start_ordinal,watermark,object_scope,source_hash)
    values(new.organization_id,new.owner_id,new.id,(r->>'start')::bigint,(r->>'end')::bigint,scope,h)
    on conflict(conversation_id,watermark) do nothing returning id into sid;
   if sid is not null then insert into app.memory_work_item(organization_id,source_id,due_at) values(new.organization_id,sid,clock_timestamp()+interval '15 seconds');end if;
  end loop;
 end if;
 if new.state='closed' then
  update app.memory_work_item w set due_at=clock_timestamp() from app.memory_source s where w.source_id=s.id and s.conversation_id=new.id and w.state='pending';
 end if;
 return null;
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
 if exists(select 1 from app.memory_source s join app.memory_source newer on newer.conversation_id=s.conversation_id and newer.watermark>s.watermark and newer.start_ordinal<=s.start_ordinal
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
 insert into app.memory_version(organization_id,owner_id,assertion_id,version,previous_id,object_scope,content_kind,trust,status,body,content_hash,summary_manifest)
 values(s.organization_id,s.owner_id,aid,v,(select id from app.memory_version where assertion_id=aid order by version desc limit 1),s.object_scope,
 case when s.conversation_id is null then 'stable_memory' else 'process_summary' end,
 case when s.conversation_id is null then 'unconfirmed' else 'generated_reference' end,
 case when s.conversation_id is null then 'candidate' else 'ready' end,input_body,encode(public.digest(convert_to(input_body,'UTF8'),'sha256'),'hex'),app.memory_source_manifest(s.id)) returning id into result;
 insert into app.memory_evidence values(s.organization_id,result,s.id);
 update app.memory_work_item set version_id=result where id=w.id;
 return result;
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
 update app.memory_version set status='superseded' where assertion_id=v.assertion_id and version<v.version and status in('ready','active') and app.memory_range_covered(id,s.id);
 update app.memory_projection set state='retired' where version_id in(select id from app.memory_version where assertion_id=v.assertion_id and version<v.version and app.memory_range_covered(id,s.id));
 update app.memory_work_item set state='indexed',lease_until=null where id=w.id;
end $$;
commit;
