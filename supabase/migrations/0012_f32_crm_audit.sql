-- F32：CRM 事实变更写入 F31 audit_event；不把内容/密钥写入审计元数据。
begin;
drop policy if exists audit_service_insert on app.audit_event;
create policy audit_service_insert on app.audit_event for insert to public with check(organization_id=app.current_organization_id() and app.is_member(organization_id,app.current_actor_id()));
create or replace function app.audit_crm_change() returns trigger language plpgsql security definer set search_path=app,pg_temp as $$
declare entity_id text; entity_version integer;
begin
 if app.current_actor_id() is null or app.current_organization_id() is null then return coalesce(new,old); end if;
 entity_id:=coalesce(to_jsonb(new)->>'id',to_jsonb(old)->>'id'); entity_version:=case when tg_op='DELETE' then null else nullif(to_jsonb(new)->>'version','')::integer end;
 perform app.append_audit_event('crm.'||lower(tg_op),tg_table_name,entity_id,jsonb_strip_nulls(jsonb_build_object('version',entity_version)));
 return coalesce(new,old);
end $$;
do $$ declare t text; begin foreach t in array array['crm_customer','crm_contact','crm_opportunity','crm_conversation','crm_task','crm_proposal'] loop
 execute format('drop trigger if exists %I on app.%I',t||'_audit',t);
 execute format('create trigger %I after insert or update or delete on app.%I for each row execute function app.audit_crm_change()',t||'_audit',t);
end loop; end $$;
commit;
