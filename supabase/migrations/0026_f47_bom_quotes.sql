-- F47: versioned source facts and read-only commercial investigation. No F41.
begin;
create unique index if not exists crm_opportunity_org_id on app.crm_opportunity(organization_id,id);
create table if not exists app.catalog_product(
 id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id),
 code text not null check(char_length(code) between 1 and 160), name text not null check(char_length(name) between 1 and 240),
 scope jsonb not null default '{}' check(jsonb_typeof(scope)='object'),
 unique(organization_id,id), unique(organization_id,code)
);
create table if not exists app.bom_revision(
 id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id), product_id uuid not null,
 revision text not null check(char_length(revision) between 1 and 80), schema_version integer not null default 1 check(schema_version=1),
 lines jsonb not null, content_hash text not null default '',
 source_kind text not null check(source_kind in ('synthetic','official','user_supplied')),
 source_reference text not null check(char_length(source_reference) between 1 and 240),
 revoked_at timestamptz, created_at timestamptz not null default clock_timestamp(),
 foreign key(organization_id,product_id) references app.catalog_product(organization_id,id),
 unique(organization_id,id), unique(organization_id,product_id,revision)
);
create table if not exists app.opportunity_bom_binding(
 organization_id uuid not null references app.organization(id), opportunity_id uuid primary key,
 bom_revision_id uuid not null, units numeric(18,6) not null check(units>0 and units<>'NaN'::numeric), required_on date not null,
 version integer not null default 1 check(version>0), updated_at timestamptz not null default clock_timestamp(),
 foreign key(organization_id,opportunity_id) references app.crm_opportunity(organization_id,id),
 foreign key(organization_id,bom_revision_id) references app.bom_revision(organization_id,id)
);
create table if not exists app.commercial_quote_revision(
 id uuid primary key default gen_random_uuid(), organization_id uuid not null references app.organization(id), opportunity_id uuid not null,
 bom_revision_id uuid not null, reference text not null check(char_length(reference) between 1 and 120), revision integer not null check(revision>0),
 quote_kind text not null check(quote_kind in ('customer_offer','supplier_offer')), bom_line_key text,
 quantity numeric(18,6) not null check(quantity>0 and quantity<>'NaN'::numeric), unit_price numeric(18,6) check(unit_price is null or (unit_price>=0 and unit_price<>'NaN'::numeric)),
 currency text check(currency is null or currency ~ '^[A-Z]{3}$'), issued_at timestamptz not null, valid_until timestamptz,
 estimated_ship_on date, confirmed_arrival_on date, stock_reserved boolean not null default false,
 scope jsonb not null default '{}' check(jsonb_typeof(scope)='object'),
 source_kind text not null check(source_kind in ('synthetic','official','user_supplied')),
 content_hash text not null default '', revoked_at timestamptz, created_at timestamptz not null default clock_timestamp(),
 foreign key(organization_id,opportunity_id) references app.crm_opportunity(organization_id,id),
 foreign key(organization_id,bom_revision_id) references app.bom_revision(organization_id,id),
 unique(organization_id,id), unique(organization_id,opportunity_id,reference,revision),
 check(valid_until is null or valid_until>issued_at),
 check((unit_price is null)=(currency is null)),
 check((quote_kind='customer_offer' and bom_line_key is null) or (quote_kind='supplier_offer' and bom_line_key is not null))
);

create or replace function app.guard_commercial_source() returns trigger
language plpgsql set search_path=app,pg_temp as $$
declare item jsonb; declare normalized jsonb := '[]'; declare quantity numeric(18,6);
begin
 if tg_op='DELETE' then raise exception 'published source cannot be deleted' using errcode='22023'; end if;
 if tg_op='UPDATE' then
  if (to_jsonb(new)-array['revoked_at','scope']) is distinct from (to_jsonb(old)-array['revoked_at','scope'])
    or (old.revoked_at is not null and new.revoked_at is distinct from old.revoked_at) then
   raise exception 'published source is immutable; create a new revision' using errcode='22023';
  end if;
  return new;
 end if;
 if tg_table_name='bom_revision' then
  if jsonb_typeof(new.lines)<>'array' or jsonb_array_length(new.lines) not between 1 and 1024 then
   raise exception 'BOM lines must be a bounded array' using errcode='22023'; end if;
  for item in select value from jsonb_array_elements(new.lines) loop
   if jsonb_typeof(item)<>'object' or (select array_agg(key order by key) from jsonb_object_keys(item) key)
      <> array['lineKey','manufacturer','mpn','quantityPerUnit'] then
    raise exception 'invalid BOM line schema' using errcode='22023'; end if;
   if jsonb_typeof(item->'lineKey')<>'string' or char_length(item->>'lineKey') not between 1 and 160
      or jsonb_typeof(item->'mpn')<>'string' or char_length(item->>'mpn') not between 1 and 160
      or jsonb_typeof(item->'manufacturer')<>'string' or char_length(item->>'manufacturer') not between 1 and 160 then
    raise exception 'invalid BOM identity' using errcode='22023'; end if;
   quantity := (item->>'quantityPerUnit')::numeric(18,6);
   if quantity is null or quantity::text='NaN' or quantity<=0 or quantity<>(item->>'quantityPerUnit')::numeric then
    raise exception 'invalid exact BOM quantity' using errcode='22023'; end if;
   normalized := normalized || jsonb_build_array(jsonb_build_object('lineKey',item->>'lineKey','manufacturer',item->>'manufacturer','mpn',item->>'mpn','quantityPerUnit',quantity::text));
  end loop;
  if (select count(*)<>count(distinct value->>'lineKey') from jsonb_array_elements(normalized)) then
   raise exception 'duplicate BOM line key' using errcode='22023'; end if;
  select jsonb_agg(value order by value->>'lineKey') into new.lines from jsonb_array_elements(normalized);
 else
  if new.quote_kind='supplier_offer' and not exists(select 1 from app.bom_revision b,jsonb_array_elements(b.lines) line
    where b.id=new.bom_revision_id and b.organization_id=new.organization_id and line->>'lineKey'=new.bom_line_key) then
   raise exception 'quote line outside BOM revision' using errcode='23503'; end if;
 end if;
 new.content_hash := encode(public.digest(convert_to((to_jsonb(new)-array['content_hash','revoked_at','created_at','scope'])::text,'UTF8'),'sha256'),'hex');
 return new;
end $$;
drop trigger if exists bom_source_guard on app.bom_revision;
create trigger bom_source_guard before insert or update or delete on app.bom_revision for each row execute function app.guard_commercial_source();
drop trigger if exists quote_source_guard on app.commercial_quote_revision;
create trigger quote_source_guard before insert or update or delete on app.commercial_quote_revision for each row execute function app.guard_commercial_source();

create or replace function app.guard_bom_binding() returns trigger language plpgsql set search_path=app,pg_temp as $$
begin
 if tg_op='DELETE' then raise exception 'binding cannot be deleted; revise it instead' using errcode='22023'; end if;
 if tg_op='INSERT' then new.version:=1;
 else
  if new.organization_id<>old.organization_id or new.opportunity_id<>old.opportunity_id then
   raise exception 'binding identity is immutable' using errcode='22023'; end if;
  if (new.bom_revision_id,new.units,new.required_on) is distinct from (old.bom_revision_id,old.units,old.required_on) then
   new.version:=old.version+1;
  else new.version:=old.version; end if;
 end if;
 new.updated_at:=clock_timestamp(); return new;
end $$;
drop trigger if exists bom_binding_guard on app.opportunity_bom_binding;
create trigger bom_binding_guard before insert or update or delete on app.opportunity_bom_binding for each row execute function app.guard_bom_binding();

create or replace function app.can_read_commercial_opportunity(input_id uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select exists(select 1 from app.crm_opportunity o join app.crm_customer c on c.id=o.customer_id
  where o.id=input_id and o.organization_id=app.current_organization_id()
    and app.is_member(o.organization_id,app.current_actor_id()) and app.can_read_crm_scope(o.scope) and app.can_read_crm_scope(c.scope));
$$;
create or replace function app.can_read_bom_revision(input_id uuid) returns boolean
language sql stable security definer set search_path=app,pg_temp as $$
 select exists(select 1 from app.bom_revision b join app.catalog_product p on p.id=b.product_id
  where b.id=input_id and b.organization_id=app.current_organization_id()
    and app.is_member(b.organization_id,app.current_actor_id()) and app.can_read_crm_scope(p.scope));
$$;
do $$ declare t text; begin foreach t in array array['catalog_product','bom_revision','opportunity_bom_binding','commercial_quote_revision'] loop
 execute format('alter table app.%I enable row level security',t);
 execute format('alter table app.%I force row level security',t);
 execute format('revoke all on app.%I from public',t);
 execute format('grant select on app.%I to supplyagent_api',t);
 execute format('drop policy if exists commercial_read on app.%I',t);
end loop; end $$;
create policy commercial_read on app.catalog_product for select to supplyagent_api using(organization_id=app.current_organization_id() and app.is_member(organization_id,app.current_actor_id()) and app.can_read_crm_scope(scope));
create policy commercial_read on app.bom_revision for select to supplyagent_api using(app.can_read_bom_revision(id));
create policy commercial_read on app.opportunity_bom_binding for select to supplyagent_api using(app.can_read_commercial_opportunity(opportunity_id) and app.can_read_bom_revision(bom_revision_id));
create policy commercial_read on app.commercial_quote_revision for select to supplyagent_api using(app.can_read_commercial_opportunity(opportunity_id) and app.can_read_bom_revision(bom_revision_id) and app.can_read_crm_scope(scope));
revoke all on function app.can_read_commercial_opportunity(uuid),app.can_read_bom_revision(uuid) from public;
grant execute on function app.can_read_commercial_opportunity(uuid),app.can_read_bom_revision(uuid) to supplyagent_api;
alter table app.intelligence_attempt drop constraint if exists intelligence_attempt_role_check;
alter table app.intelligence_attempt add constraint intelligence_attempt_role_check check(role in ('customer_facts','business_policy','supply_risk','opportunity_diagnosis','quote_validity'));

create or replace function app.begin_commercial_intelligence_run(
  input_opportunity_id uuid, input_question text, input_key text, input_model_policy_id uuid, input_as_of timestamptz
) returns table(mission_id uuid, run_id uuid, job_id uuid)
language plpgsql security definer set search_path = app, pg_temp as $$
declare m app.runtime_mission%rowtype; declare r app.runtime_run%rowtype;
declare trace_id uuid; declare created_job_id uuid;
declare linked_case_id uuid; declare linked_part text; declare linked_parts jsonb; declare desired_context jsonb; declare bound_bom_id uuid; declare bound_version integer;
begin
  if app.current_actor_id() is null or app.current_organization_id() is null
     or not app.is_member(app.current_organization_id(), app.current_actor_id()) then
    raise exception 'missing organization membership' using errcode = '42501';
  end if;
  if input_question is null or char_length(btrim(input_question)) not between 1 and 600 then
    raise exception 'invalid question' using errcode='22023'; end if;
  if input_key is null or char_length(input_key) not between 1 and 120 then
    raise exception 'invalid run input' using errcode = '22023';
  end if;
  select o.supply_case_id into linked_case_id from app.crm_opportunity o
  where o.id = input_opportunity_id and o.organization_id = app.current_organization_id();
  if not found then raise exception 'opportunity outside scope' using errcode = '42501'; end if;
  if not exists(select 1 from app.crm_opportunity o join app.crm_customer c on c.id=o.customer_id
    where o.id=input_opportunity_id and app.can_read_crm_scope(o.scope) and app.can_read_crm_scope(c.scope)) then
    raise exception 'opportunity outside scope' using errcode='42501'; end if;
  if linked_case_id is not null then
    select c.scope->'partNumbers'->>0 into linked_part from app.supply_case c
    where c.id = linked_case_id and c.organization_id = app.current_organization_id();
    if not found or linked_part is null then
      raise exception 'linked supply case has no part scope' using errcode = '42501';
    end if;
    select jsonb_agg(p.value order by p.value) into linked_parts
      from (select distinct value from app.supply_case c,
        jsonb_array_elements(c.scope->'partNumbers') as part(value) where c.id=linked_case_id) p;
  end if;
  if input_model_policy_id is not null and not exists (
    select 1 from app.model_policy p where p.id = input_model_policy_id
      and p.organization_id = app.current_organization_id() and p.active
  ) then raise exception 'model policy outside organization' using errcode = '42501'; end if;
  select b.bom_revision_id,b.version into bound_bom_id,bound_version from app.opportunity_bom_binding b
    where b.organization_id=app.current_organization_id() and b.opportunity_id=input_opportunity_id;
  if bound_bom_id is not null and (not app.can_read_bom_revision(bound_bom_id) or exists(
    select 1 from app.bom_revision where id=bound_bom_id and revoked_at is not null)) then
    raise exception 'BOM outside scope or withdrawn' using errcode='42501'; end if;
  desired_context := jsonb_build_object(
    'opportunityId', input_opportunity_id, 'caseId', linked_case_id,
    'partNumber', linked_part, 'modelPolicyId', input_model_policy_id, 'businessNodes', true, 'partNumbers', linked_parts, 'commercialNodes', true, 'bomRevisionId', bound_bom_id, 'bomBindingVersion', bound_version
  );
  insert into app.runtime_mission(organization_id,kind,created_by,idempotency_key,context_ref)
  values(app.current_organization_id(),'opportunity_intelligence',app.current_actor_id(),input_key,desired_context)
  on conflict (organization_id,idempotency_key) where idempotency_key is not null
    do update set idempotency_key=excluded.idempotency_key returning * into m;
  if m.kind <> 'opportunity_intelligence' or m.context_ref <> desired_context then
    raise exception 'idempotency key reused with different input' using errcode = '22023';
  end if;
  select * into r from app.runtime_run where app.runtime_run.mission_id=m.id;
  if found then
    if not exists(select 1 from app.intelligence_request q where q.mission_id=m.id and q.requested_by=app.current_actor_id() and q.question=input_question and q.as_of is not distinct from input_as_of) then
      raise exception 'idempotency input mismatch' using errcode='22023'; end if;
    select id into created_job_id from app.runtime_job where app.runtime_job.run_id=r.id;
    return query select m.id,r.id,created_job_id; return;
  end if;
  insert into app.intelligence_request(mission_id,organization_id,requested_by,opportunity_id,question,as_of)
    values(m.id,m.organization_id,app.current_actor_id(),input_opportunity_id,input_question,input_as_of);
  trace_id := app.begin_trace_run('worker',linked_case_id);
  insert into app.runtime_run(organization_id,mission_id,trace_run_id)
    values(m.organization_id,m.id,trace_id) returning * into r;
  insert into app.runtime_job(organization_id,run_id,idempotency_key,kind)
    values(m.organization_id,r.id,input_key,'opportunity_intelligence') returning id into created_job_id;
  return query select m.id,r.id,created_job_id;
end;
$$;

revoke all on function app.begin_commercial_intelligence_run(uuid,text,text,uuid,timestamptz) from public;
grant execute on function app.begin_commercial_intelligence_run(uuid,text,text,uuid,timestamptz) to supplyagent_api;
commit;
