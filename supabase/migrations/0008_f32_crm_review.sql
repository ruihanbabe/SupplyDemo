-- F32：人工 Kanban 排序与 Proposal 决策。决策不等于执行，执行留给 F41。
begin;
create or replace function app.crm_can_review() returns boolean language sql stable security definer set search_path=app,pg_temp as $$
 select exists (select 1 from app.organization_member where organization_id=app.current_organization_id() and user_id=app.current_actor_id() and role in ('organization_admin','approver'))
$$;
create or replace function app.reorder_crm_opportunity(input_id uuid,input_expected_version integer,input_stage text,input_position numeric)
returns integer language plpgsql security definer set search_path=app,pg_temp as $$
declare next_version integer;
begin
 if not app.crm_can_propose() then raise exception 'crm proposal capability required' using errcode='42501'; end if;
 update app.crm_opportunity set stage=input_stage,kanban_position=input_position,version=version+1,updated_at=now()
 where id=input_id and organization_id=app.current_organization_id() and version=input_expected_version returning version into next_version;
 if not found then raise exception 'opportunity missing or version conflict' using errcode='40001'; end if;
 return next_version;
end; $$;
create or replace function app.review_crm_proposal(input_id uuid,input_decision text)
returns text language plpgsql security definer set search_path=app,pg_temp as $$
declare next_status text;
begin
 if not app.crm_can_review() then raise exception 'crm approval capability required' using errcode='42501'; end if;
 if input_decision not in ('accepted','rejected') then raise exception 'invalid proposal decision' using errcode='22023'; end if;
 update app.crm_proposal set status=input_decision where id=input_id and organization_id=app.current_organization_id() and status in ('draft','submitted') returning status into next_status;
 if not found then raise exception 'proposal missing or not reviewable' using errcode='40901'; end if;
 return next_status;
end; $$;
drop policy if exists crm_opportunity_service_update on app.crm_opportunity;
drop policy if exists crm_proposal_service_update on app.crm_proposal;
create policy crm_opportunity_service_update on app.crm_opportunity for update to public using (organization_id=app.current_organization_id() and app.crm_can_propose()) with check (organization_id=app.current_organization_id() and app.crm_can_propose());
create policy crm_proposal_service_update on app.crm_proposal for update to public using (organization_id=app.current_organization_id() and app.crm_can_review()) with check (organization_id=app.current_organization_id() and app.crm_can_review());
grant execute on function app.crm_can_review(),app.reorder_crm_opportunity(uuid,integer,text,numeric),app.review_crm_proposal(uuid,text) to supplyagent_api;
commit;
