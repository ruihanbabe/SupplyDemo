-- F32：对象 scope 的最小可执行语义。scope.member_ids 缺失=组织内可见；存在=仅所列成员可见。
begin;
create or replace function app.can_read_crm_scope(input_scope jsonb)
returns boolean language sql stable security definer set search_path=app,pg_temp as $$
 select case when not (input_scope ? 'member_ids') then true
             when jsonb_typeof(input_scope->'member_ids') <> 'array' then false
             else exists(select 1 from jsonb_array_elements_text(input_scope->'member_ids') as member_id where member_id=app.current_actor_id()::text) end
$$;
do $$ declare t text; begin foreach t in array array['crm_customer','crm_contact','crm_opportunity','crm_conversation','crm_task'] loop
 execute format('drop policy if exists %I on app.%I',t||'_isolation',t);
 execute format('create policy %I on app.%I for select to supplyagent_api using(organization_id=app.current_organization_id() and app.is_member(organization_id,app.current_actor_id()) and app.can_read_crm_scope(scope))',t||'_isolation',t);
end loop; end $$;
grant execute on function app.can_read_crm_scope(jsonb) to supplyagent_api;
commit;
