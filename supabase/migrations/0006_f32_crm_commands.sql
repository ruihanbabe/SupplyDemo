-- F32 第二批：人工 CRM 录入与 Proposal 乐观并发检测；不执行 Proposal。
begin;

create or replace function app.create_crm_customer(input_display_name text, input_lifecycle text default 'lead')
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare customer_id uuid;
begin
  if not app.crm_can_propose() then raise exception 'crm proposal capability required' using errcode = '42501'; end if;
  insert into app.crm_customer (organization_id,display_name,lifecycle,created_by)
  values (app.current_organization_id(),input_display_name,input_lifecycle,app.current_actor_id()) returning id into customer_id;
  return customer_id;
end;
$$;
create or replace function app.create_crm_opportunity(input_customer_id uuid, input_title text, input_stage text default 'lead', input_kanban_position numeric default 0, input_supply_case_id uuid default null)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare opportunity_id uuid;
begin
  if not app.crm_can_propose() then raise exception 'crm proposal capability required' using errcode = '42501'; end if;
  if not exists (select 1 from app.crm_customer where id=input_customer_id and organization_id=app.current_organization_id()) then raise exception 'customer outside organization' using errcode='42501'; end if;
  if input_supply_case_id is not null and not exists (select 1 from app.supply_case where id=input_supply_case_id and organization_id=app.current_organization_id()) then raise exception 'case outside organization' using errcode='42501'; end if;
  insert into app.crm_opportunity (organization_id,customer_id,title,stage,kanban_position,supply_case_id,created_by)
  values (app.current_organization_id(),input_customer_id,input_title,input_stage,input_kanban_position,input_supply_case_id,app.current_actor_id()) returning id into opportunity_id;
  return opportunity_id;
end;
$$;
create or replace function app.create_crm_proposal(input_target_type text, input_target_id uuid, input_target_version integer, input_proposal_type text, input_content jsonb, input_content_hash text, input_evidence_refs jsonb, input_risk_level text)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare proposal_id uuid; current_version integer; proposal_status text := 'draft';
begin
  if not app.crm_can_propose() then raise exception 'crm proposal capability required' using errcode = '42501'; end if;
  if input_target_type='customer' then select version into current_version from app.crm_customer where id=input_target_id and organization_id=app.current_organization_id();
  elsif input_target_type='opportunity' then select version into current_version from app.crm_opportunity where id=input_target_id and organization_id=app.current_organization_id();
  elsif input_target_type='task' then select version into current_version from app.crm_task where id=input_target_id and organization_id=app.current_organization_id();
  elsif input_target_type <> 'reply_draft' then raise exception 'invalid proposal target' using errcode='22023'; end if;
  if input_target_type <> 'reply_draft' and current_version is null then raise exception 'target outside organization' using errcode='42501'; end if;
  if input_target_type <> 'reply_draft' and input_target_version is distinct from current_version then proposal_status := 'stale'; end if;
  insert into app.crm_proposal (organization_id,target_type,target_id,target_version,proposal_type,content,content_hash,evidence_refs,risk_level,status,requested_by)
  values (app.current_organization_id(),input_target_type,input_target_id,input_target_version,input_proposal_type,input_content,input_content_hash,coalesce(input_evidence_refs,'[]'::jsonb),input_risk_level,proposal_status,app.current_actor_id()) returning id into proposal_id;
  return proposal_id;
end;
$$;
drop policy if exists crm_customer_service_insert on app.crm_customer;
create policy crm_customer_service_insert on app.crm_customer for insert to public with check (organization_id=app.current_organization_id() and app.crm_can_propose());
drop policy if exists crm_opportunity_service_insert on app.crm_opportunity;
create policy crm_opportunity_service_insert on app.crm_opportunity for insert to public with check (organization_id=app.current_organization_id() and app.crm_can_propose());
grant execute on function app.create_crm_customer(text,text) to supplyagent_api;
grant execute on function app.create_crm_opportunity(uuid,text,text,numeric,uuid) to supplyagent_api;
commit;
