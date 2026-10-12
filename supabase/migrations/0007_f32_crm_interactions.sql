-- F32：联系人、会话和任务的受控人工录入；渠道发送与执行不在此 migration 中。
begin;
create or replace function app.create_crm_contact(input_customer_id uuid, input_display_name text, input_email text default null)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare contact_id uuid;
begin
 if not app.crm_can_propose() then raise exception 'crm proposal capability required' using errcode='42501'; end if;
 if not exists (select 1 from app.crm_customer where id=input_customer_id and organization_id=app.current_organization_id()) then raise exception 'customer outside organization' using errcode='42501'; end if;
 insert into app.crm_contact (organization_id,customer_id,display_name,email,created_by) values (app.current_organization_id(),input_customer_id,input_display_name,input_email,app.current_actor_id()) returning id into contact_id; return contact_id;
end; $$;
create or replace function app.create_crm_conversation(input_customer_id uuid, input_opportunity_id uuid, input_channel text, input_summary text, input_external_reference text default null)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare conversation_id uuid;
begin
 if not app.crm_can_propose() then raise exception 'crm proposal capability required' using errcode='42501'; end if;
 if input_customer_id is not null and not exists (select 1 from app.crm_customer where id=input_customer_id and organization_id=app.current_organization_id()) then raise exception 'customer outside organization' using errcode='42501'; end if;
 if input_opportunity_id is not null and not exists (select 1 from app.crm_opportunity where id=input_opportunity_id and organization_id=app.current_organization_id()) then raise exception 'opportunity outside organization' using errcode='42501'; end if;
 insert into app.crm_conversation (organization_id,customer_id,opportunity_id,channel,summary,external_reference,created_by) values (app.current_organization_id(),input_customer_id,input_opportunity_id,input_channel,input_summary,input_external_reference,app.current_actor_id()) returning id into conversation_id; return conversation_id;
end; $$;
create or replace function app.create_crm_task(input_opportunity_id uuid, input_title text)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare task_id uuid;
begin
 if not app.crm_can_propose() then raise exception 'crm proposal capability required' using errcode='42501'; end if;
 if input_opportunity_id is not null and not exists (select 1 from app.crm_opportunity where id=input_opportunity_id and organization_id=app.current_organization_id()) then raise exception 'opportunity outside organization' using errcode='42501'; end if;
 insert into app.crm_task (organization_id,opportunity_id,title,created_by) values (app.current_organization_id(),input_opportunity_id,input_title,app.current_actor_id()) returning id into task_id; return task_id;
end; $$;
drop policy if exists crm_contact_service_insert on app.crm_contact;
drop policy if exists crm_conversation_service_insert on app.crm_conversation;
drop policy if exists crm_task_service_insert on app.crm_task;
create policy crm_contact_service_insert on app.crm_contact for insert to public with check (organization_id=app.current_organization_id() and app.crm_can_propose());
create policy crm_conversation_service_insert on app.crm_conversation for insert to public with check (organization_id=app.current_organization_id() and app.crm_can_propose());
create policy crm_task_service_insert on app.crm_task for insert to public with check (organization_id=app.current_organization_id() and app.crm_can_propose());
grant execute on function app.create_crm_contact(uuid,text,text), app.create_crm_conversation(uuid,uuid,text,text,text), app.create_crm_task(uuid,text) to supplyagent_api;
commit;
