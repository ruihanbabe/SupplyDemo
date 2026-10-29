-- M2: optional qualifiers and explicit private relations; no semantic guesses.
begin;
alter table app.memory_version add column if not exists event_at timestamptz;
alter table app.memory_version add column if not exists valid_from timestamptz;
alter table app.memory_version add column if not exists valid_to timestamptz;
alter table app.memory_version add column if not exists conditions text not null default '';
create table if not exists app.memory_relation(
 organization_id uuid not null references app.organization(id),
 from_id uuid not null references app.memory_version(id),to_id uuid not null references app.memory_version(id),
 kind text not null check(kind in('duplicate_of','supersedes','contradicts','derived_from','merged_from')),
 actor_id uuid not null,created_at timestamptz not null default clock_timestamp(),
 primary key(from_id,to_id,kind),check(from_id<>to_id)
);
alter table app.memory_relation enable row level security;
alter table app.memory_relation force row level security;
drop policy if exists memory_relation_private on app.memory_relation;
create policy memory_relation_private on app.memory_relation for select to supplyagent_api using(
 organization_id=app.current_organization_id() and exists(select 1 from app.memory_version v where v.id=from_id and (v.owner_id=app.current_actor_id() or app.is_runtime_worker())));
grant select on app.memory_relation to supplyagent_api;
create or replace function app.memory_qualify(input_id uuid,input_event timestamptz,input_from timestamptz,input_to timestamptz,input_conditions text) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
declare v app.memory_version%rowtype;
begin
 select * into v from app.memory_version where id=input_id and organization_id=app.current_organization_id() and owner_id=app.current_actor_id();
 if v.id is null or app.is_runtime_worker() or v.status not in('candidate','ready','active') or not exists(select 1 from app.memory_evidence where version_id=v.id)
 or exists(select 1 from app.memory_evidence where version_id=v.id and not app.memory_source_readable(source_id))
 or input_conditions is null or octet_length(input_conditions)>2048 or (input_to is not null and input_from is not null and input_to<=input_from)
 then raise exception 'memory_denied' using errcode='42501';end if;
 perform app.memory_domain_lock(v.organization_id,v.owner_id,v.object_scope);
 select * into v from app.memory_version where id=input_id;
 if v.status not in('candidate','ready','active') or exists(select 1 from app.memory_evidence where version_id=v.id and not app.memory_source_readable(source_id)) then raise exception 'memory_source_denied' using errcode='42501';end if;
 -- Qualifiers freeze before publication; correcting them requires a new version.
 if exists(select 1 from app.memory_projection where version_id=v.id) then raise exception 'memory_denied' using errcode='42501';end if;
 update app.memory_version set event_at=input_event,valid_from=input_from,valid_to=input_to,conditions=input_conditions where id=v.id;
end $$;
create or replace function app.memory_relate(input_from uuid,input_to uuid,input_kind text) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
declare a app.memory_version%rowtype;declare b app.memory_version%rowtype;
begin
 select * into a from app.memory_version where id=input_from and organization_id=app.current_organization_id() and owner_id=app.current_actor_id();
 select * into b from app.memory_version where id=input_to and organization_id=app.current_organization_id() and owner_id=app.current_actor_id();
 if a.id is null or b.id is null or a.id=b.id or app.is_runtime_worker() or a.object_scope<>b.object_scope
 or input_kind is null or input_kind not in('duplicate_of','supersedes','contradicts','derived_from','merged_from') then raise exception 'memory_denied' using errcode='42501';end if;
 perform app.memory_domain_lock(a.organization_id,a.owner_id,a.object_scope);
 if not app.memory_version_readable(a.id) or not app.memory_version_readable(b.id) then raise exception 'memory_source_denied' using errcode='42501';end if;
 select * into a from app.memory_version where id=input_from;
 select * into b from app.memory_version where id=input_to;
 if input_kind='duplicate_of' and (a.body<>b.body or a.trust<>b.trust or a.content_kind<>b.content_kind or a.conditions<>b.conditions
 or a.event_at is distinct from b.event_at or a.valid_from is distinct from b.valid_from or a.valid_to is distinct from b.valid_to)
 then raise exception 'memory_invalid' using errcode='22023';end if;
 -- Every derivation carries all original supports, never a new independent source.
 if input_kind in('duplicate_of','derived_from','merged_from') then
  insert into app.memory_evidence select a.organization_id,a.id,e.source_id from app.memory_evidence e where e.version_id=b.id on conflict do nothing;
 end if;
 insert into app.memory_relation values(a.organization_id,a.id,b.id,input_kind,app.current_actor_id(),clock_timestamp()) on conflict do nothing;
 if input_kind='supersedes' then
  update app.memory_version set status='superseded' where id=b.id;
  update app.memory_projection set state='retired' where version_id=b.id;
 end if;
end $$;
-- Include shared supports as well as consumed Context in the conservative erase closure.
create or replace function app.memory_affected_assertions(input_id uuid) returns table(id uuid)
language sql stable security definer set search_path=app,pg_temp as $$
 with recursive assertions(id) as (
 select v.assertion_id from app.memory_version v where v.id=input_id and v.organization_id=app.current_organization_id() and (v.owner_id=app.current_actor_id() or app.is_runtime_worker())
 union
 select child.assertion_id from assertions a join app.memory_version parent on parent.assertion_id=a.id
 join app.memory_evidence pe on pe.version_id=parent.id
 join app.memory_evidence ce on ce.source_id=pe.source_id or ce.source_id in(
 select s.id from app.memory_dependency d join app.memory_source s on s.conversation_id=d.conversation_id where d.version_id=parent.id)
 join app.memory_version child on child.id=ce.version_id where child.organization_id=app.current_organization_id()
 ) select id from assertions
$$;
revoke all on function app.memory_qualify(uuid,timestamptz,timestamptz,timestamptz,text),app.memory_relate(uuid,uuid,text) from public;
grant execute on function app.memory_qualify(uuid,timestamptz,timestamptz,timestamptz,text),app.memory_relate(uuid,uuid,text) to supplyagent_api;
create or replace function app.memory_erase_qualifiers() returns trigger
language plpgsql set search_path=app,pg_temp as $$
begin
 if new.body is null then new.conditions:='';end if;
 return new;
end $$;
drop trigger if exists memory_erase_qualifiers on app.memory_version;
create trigger memory_erase_qualifiers before update of body on app.memory_version for each row execute function app.memory_erase_qualifiers();
revoke all on function app.memory_erase_qualifiers() from public;
commit;
