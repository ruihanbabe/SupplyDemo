-- M2: explicit export/import/reconcile/read-route bridge; old storage is retained.
begin;
alter table app.memory_source add column if not exists legacy_id uuid unique references app.long_term_memory(id);
create table if not exists app.memory_read_route(
 organization_id uuid not null references app.organization(id),owner_id uuid not null,
 route text not null check(route in('legacy','governed')),primary key(organization_id,owner_id)
);
alter table app.memory_read_route enable row level security;
alter table app.memory_read_route force row level security;
create or replace function app.memory_legacy_readable(input_id uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select exists(select 1 from app.long_term_memory m where m.id=input_id and m.organization_id=app.current_organization_id()
 and (m.proposed_by=app.current_actor_id() or app.is_runtime_worker()) and m.status in('candidate','confirmed')
 and m.source_version_id is not null and exists(select 1 from app.knowledge_document_version v where v.id=m.source_version_id
 and app.knowledge_can_read(v.document_id) and not exists(select 1 from app.knowledge_version_revocation r where r.version_id=v.id)))
$$;
create or replace function app.memory_source_base_readable(input_id uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select exists(select 1 from app.memory_source s where s.id=input_id and s.organization_id=app.current_organization_id()
 and s.state='ready' and s.created_at>clock_timestamp()-interval '7 days' and (s.owner_id=app.current_actor_id() or app.is_runtime_worker())
 and app.is_member(s.organization_id,s.owner_id) and (s.conversation_id is null or app.context_can_read(s.conversation_id))
 and (s.legacy_id is null or (app.memory_legacy_readable(s.legacy_id) and exists(select 1 from app.long_term_memory m where m.id=s.legacy_id and m.content::text=s.explicit_text))))
$$;
create or replace function app.memory_import_legacy(input_id uuid,input_scope text) returns uuid
language plpgsql security definer set search_path=app,pg_temp as $$
declare m app.long_term_memory%rowtype;declare sid uuid;
begin
 if app.is_runtime_worker() or not app.memory_legacy_readable(input_id) then raise exception 'memory_source_denied' using errcode='42501';end if;
 select * into m from app.long_term_memory where id=input_id and proposed_by=app.current_actor_id();
 if m.id is null then raise exception 'memory_denied' using errcode='42501';end if;
 perform pg_advisory_xact_lock(hashtextextended('legacy:'||m.id::text,0));
 select id into sid from app.memory_source where legacy_id=m.id;
 if sid is not null then
  if not exists(select 1 from app.memory_source where id=sid and object_scope=input_scope and state='ready') then raise exception 'memory_denied' using errcode='42501';end if;
  return sid;
 end if;
 sid:=app.memory_remember(input_scope,m.content::text);
 update app.memory_source set legacy_id=m.id where id=sid;
 return sid;
end $$;
create or replace function app.memory_reconcile_legacy(input_id uuid) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
declare v app.memory_version%rowtype;
begin
 select * into v from app.memory_version where id=input_id and organization_id=app.current_organization_id() and owner_id=app.current_actor_id();
 if v.id is null or v.status<>'candidate' or not exists(select 1 from app.memory_evidence e join app.memory_source s on s.id=e.source_id
 join app.long_term_memory m on m.id=s.legacy_id where e.version_id=v.id and m.status='confirmed' and m.confirmed_by is not null
 and s.explicit_text=v.body and app.memory_source_readable(s.id)) then raise exception 'memory_source_denied' using errcode='42501';end if;
 perform app.memory_confirm(v.id);
end $$;
create or replace function app.memory_set_read_route(input_route text) returns void
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if app.is_runtime_worker() or not app.is_member(app.current_organization_id(),app.current_actor_id()) or input_route is null or input_route not in('legacy','governed') then raise exception 'memory_denied' using errcode='42501';end if;
 if input_route='governed' and exists(select 1 from app.long_term_memory m where m.organization_id=app.current_organization_id() and m.proposed_by=app.current_actor_id()
 and m.status='confirmed' and app.memory_legacy_readable(m.id) and not exists(select 1 from app.memory_source denied where denied.legacy_id=m.id and denied.state<>'ready') and not exists(select 1 from app.memory_source s join app.memory_evidence e on e.source_id=s.id
 join app.memory_version v on v.id=e.version_id join app.memory_projection p on p.version_id=v.id
 where s.legacy_id=m.id and p.state='indexed' and app.memory_version_readable(v.id))) then raise exception 'memory_denied' using errcode='42501';end if;
 insert into app.memory_read_route values(app.current_organization_id(),app.current_actor_id(),input_route)
 on conflict(organization_id,owner_id) do update set route=excluded.route;
end $$;
create or replace function app.memory_read_current() returns jsonb
language sql stable security definer set search_path=app,pg_temp as $$
 select case when not app.memory_recovery_ready() or app.is_runtime_worker() then '[]'::jsonb
 when coalesce((select route from app.memory_read_route where organization_id=app.current_organization_id() and owner_id=app.current_actor_id()),'legacy')='governed'
 then coalesce((select jsonb_agg(jsonb_build_object('id',v.id,'body',v.body,'trust',v.trust)) from app.memory_version v join app.memory_projection p on p.version_id=v.id
 where v.owner_id=app.current_actor_id() and p.state='indexed' and app.memory_version_readable(v.id)),'[]'::jsonb)
 else coalesce((select jsonb_agg(jsonb_build_object('id',m.id,'content',m.content,'trust','confirmed')) from app.long_term_memory m
 where m.organization_id=app.current_organization_id() and m.proposed_by=app.current_actor_id() and m.status='confirmed' and app.memory_legacy_readable(m.id)
 and not exists(select 1 from app.memory_source s where s.legacy_id=m.id and (s.state<>'ready' or exists(select 1 from app.memory_evidence e join app.memory_version v on v.id=e.version_id where e.source_id=s.id and v.status in('revoked','deleted'))))),'[]'::jsonb) end
$$;
revoke all on function app.memory_legacy_readable(uuid),app.memory_import_legacy(uuid,text),app.memory_reconcile_legacy(uuid),app.memory_set_read_route(text),app.memory_read_current() from public;
grant execute on function app.memory_legacy_readable(uuid),app.memory_import_legacy(uuid,text),app.memory_reconcile_legacy(uuid),app.memory_set_read_route(text),app.memory_read_current() to supplyagent_api;
create or replace function app.memory_erase_legacy() returns trigger
language plpgsql security definer set search_path=app,pg_temp as $$
begin
 if new.state='backup_pending' and new.mode='hard' and new.include_source then
  update app.long_term_memory set content='{"erased":true}',status='revoked',confirmed_by=null where id in(
   select s.legacy_id from app.memory_source s join app.memory_evidence e on e.source_id=s.id join app.memory_version v on v.id=e.version_id
   where v.assertion_id in(select value::uuid from jsonb_array_elements_text(new.inventory->'assertions')));
 end if;
 return null;
end $$;
drop trigger if exists memory_erase_legacy on app.memory_delete_request;
create trigger memory_erase_legacy after update of state on app.memory_delete_request for each row execute function app.memory_erase_legacy();
revoke all on function app.memory_erase_legacy() from public;
commit;
