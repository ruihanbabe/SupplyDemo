-- F41 local SQL execution; requires 0023 and 0042, clone validation only.
begin;
alter table app.crm_customer add column if not exists action_opt_out boolean not null default false;
alter table app.action_proposal add column if not exists service_revision integer not null default 0;
alter table app.action_proposal add column if not exists idempotency_key text;
create unique index if not exists action_proposal_request_key on app.action_proposal(organization_id,requested_by,idempotency_key) where idempotency_key is not null;
alter table app.action_approval add column if not exists authorization_id uuid not null default gen_random_uuid();
create table if not exists app.action_service_policy(
 organization_id uuid primary key references app.organization(id),
 revision integer not null check(revision>0), enabled boolean not null default false,
 max_approval_seconds integer not null default 86400 check(max_approval_seconds between 60 and 604800)
);
create table if not exists app.action_execution(
 proposal_id uuid primary key references app.action_proposal(id),
 organization_id uuid not null references app.organization(id),
 logical_action_id uuid not null unique default gen_random_uuid(),
 job_id uuid not null unique references app.runtime_job(id),
 trace_run_id uuid not null references app.trace_run(id),
 state text not null default 'pending' check(state in ('pending','succeeded','rejected')),
 result_code text, completed_at timestamptz
);
create or replace function app.guard_action_service_policy() returns trigger
language plpgsql set search_path=app,pg_temp as $$
begin
 if tg_op='DELETE' then raise exception 'disable with a new revision instead' using errcode='22023';end if;
 if new.organization_id<>old.organization_id or (new is distinct from old and new.revision<=old.revision)
 then raise exception 'policy revision must advance' using errcode='22023';end if;
 return new;
end $$;
drop trigger if exists action_service_revision_guard on app.action_service_policy;
create trigger action_service_revision_guard before update or delete on app.action_service_policy
 for each row execute function app.guard_action_service_policy();
alter table app.action_service_policy enable row level security;
alter table app.action_service_policy force row level security;
alter table app.action_execution enable row level security;
alter table app.action_execution force row level security;
drop policy if exists action_service_read on app.action_service_policy;
create policy action_service_read on app.action_service_policy for select to supplyagent_api
 using(organization_id=app.current_organization_id() and app.is_member(organization_id,app.current_actor_id()));
drop policy if exists action_execution_read on app.action_execution;
create policy action_execution_read on app.action_execution for select to supplyagent_api
 using(organization_id=app.current_organization_id() and app.is_member(organization_id,app.current_actor_id()));
revoke all on app.action_service_policy,app.action_execution from public;
grant select on app.action_service_policy,app.action_execution to supplyagent_api;

create or replace function app.action_content_hash(p app.action_proposal) returns text
language sql stable set search_path=app,pg_temp as $$
 select encode(public.digest(convert_to(jsonb_build_object('target',p.target_id,'version',p.target_version,
   'content',p.content,'evidence',p.evidence_artifact_ids,'expires',extract(epoch from p.expires_at),
   'revision',p.service_revision,'requester',p.requested_by)::text,'UTF8'),'sha256'),'hex');
$$;

create or replace function app.create_action_proposal(
 input_target_id uuid,input_target_version integer,input_content jsonb,
 input_evidence_artifact_ids uuid[],input_expires_at timestamptz
) returns table(proposal_id uuid,proposal_status text)
language plpgsql security definer set search_path=app,pg_temp as $$
declare p app.action_proposal%rowtype; tr uuid;
begin
 if not app.crm_can_propose() then raise exception 'forbidden' using errcode='42501'; end if;
 if input_expires_at<=clock_timestamp() or input_expires_at>clock_timestamp()+make_interval(secs=>coalesce((select max_approval_seconds from app.action_service_policy where organization_id=app.current_organization_id()),86400))
  or input_target_version<1 or jsonb_typeof(input_content)<>'object'
  or (select count(*) from jsonb_object_keys(input_content))<>1
  or not input_content ? 'displayName' or jsonb_typeof(input_content->'displayName')<>'string'
  or char_length(btrim(input_content->>'displayName')) not between 1 and 160
 then raise exception 'invalid proposal' using errcode='22023'; end if;
 if not exists(select 1 from app.crm_customer c where c.id=input_target_id
   and c.organization_id=app.current_organization_id() and c.version=input_target_version
   and app.can_read_crm_scope(c.scope)) then raise exception 'target unavailable' using errcode='42501'; end if;
 if input_evidence_artifact_ids is null or cardinality(input_evidence_artifact_ids) not between 1 and 16
  or cardinality(input_evidence_artifact_ids)<>(select count(distinct a.id) from app.trace_artifact a
    where a.id=any(input_evidence_artifact_ids) and a.organization_id=app.current_organization_id()
    and a.artifact_type='manual_action_evidence' and a.actor_id=app.current_actor_id()
    and a.content_hash=encode(public.digest(convert_to(jsonb_build_object('artifact_type',a.artifact_type,
       'redacted_summary',a.redacted_summary,'redacted_payload',a.redacted_payload)::text,'UTF8'),'sha256'),'hex'))
 then raise exception 'evidence unavailable' using errcode='42501'; end if;
 select run_id into tr from app.trace_artifact where id=input_evidence_artifact_ids[1];
 if exists(select 1 from app.trace_artifact where id=any(input_evidence_artifact_ids) and run_id<>tr)
 then raise exception 'mixed evidence runs' using errcode='22023'; end if;
 p.id:=gen_random_uuid(); p.organization_id:=app.current_organization_id();p.action_kind:='customer_update';
 p.target_id:=input_target_id;p.target_version:=input_target_version;p.content:=input_content;
 p.evidence_artifact_ids:=input_evidence_artifact_ids;p.risk_level:='r1';p.status:='pending_review';
 p.expires_at:=input_expires_at;p.requested_by:=app.current_actor_id();p.trace_run_id:=tr;p.created_at:=clock_timestamp();
 select coalesce((select revision from app.action_service_policy where organization_id=p.organization_id),0) into p.service_revision;
 p.content_hash:=app.action_content_hash(p);
 insert into app.action_proposal select p.*;
 perform app.append_trace_event(tr,null,'proposal.created','Action proposal created',jsonb_build_object('proposalId',p.id),null);
 return query select p.id,p.status;
end $$;

create or replace function app.create_action_proposal_idempotent(
 input_target_id uuid,input_target_version integer,input_content jsonb,
 input_evidence_artifact_ids uuid[],input_expires_at timestamptz,input_key text
) returns table(proposal_id uuid,proposal_status text)
language plpgsql security definer set search_path=app,pg_temp as $$
declare p app.action_proposal%rowtype; new_id uuid; new_status text;
begin
 if not app.crm_can_propose() then raise exception 'forbidden' using errcode='42501'; end if;
 if input_key is not null then
  if char_length(input_key) not between 1 and 120 then raise exception 'invalid key' using errcode='22023'; end if;
  perform pg_advisory_xact_lock(hashtextextended(app.current_organization_id()::text||':'||app.current_actor_id()::text||':'||input_key,0));
  select * into p from app.action_proposal where organization_id=app.current_organization_id()
    and requested_by=app.current_actor_id() and idempotency_key=input_key;
  if p.id is not null then
   if not exists(select 1 from app.crm_customer c where c.id=p.target_id and c.organization_id=p.organization_id
     and app.can_read_crm_scope(c.scope)) then raise exception 'target unavailable' using errcode='42501'; end if;
   if p.target_id is distinct from input_target_id or p.target_version is distinct from input_target_version
    or p.content is distinct from input_content or p.evidence_artifact_ids is distinct from input_evidence_artifact_ids
    or p.expires_at is distinct from input_expires_at then raise exception 'idempotency conflict' using errcode='40901'; end if;
   return query select p.id,p.status; return;
  end if;
 end if;
 select c.proposal_id,c.proposal_status into new_id,new_status
  from app.create_action_proposal(input_target_id,input_target_version,input_content,input_evidence_artifact_ids,input_expires_at) c;
 update app.action_proposal set idempotency_key=input_key where id=new_id;
 return query select new_id,new_status;
end $$;
revoke all on function app.create_action_proposal_idempotent(uuid,integer,jsonb,uuid[],timestamptz,text) from public;
grant execute on function app.create_action_proposal_idempotent(uuid,integer,jsonb,uuid[],timestamptz,text) to supplyagent_api;

create or replace function app.review_action_proposal(input_proposal_id uuid,input_decision text)
returns text language plpgsql security definer set search_path=app,pg_temp as $$
declare p app.action_proposal%rowtype; m uuid; r uuid; j uuid; tr uuid;
begin
 if not app.crm_can_review() then raise exception 'forbidden' using errcode='42501'; end if;
 select * into p from app.action_proposal where id=input_proposal_id
   and organization_id=app.current_organization_id() for update;
 if p.id is null then raise exception 'unavailable' using errcode='42501'; end if;
 if p.requested_by=app.current_actor_id() then raise exception 'self approval' using errcode='42501'; end if;
 if p.status<>'pending_review' or p.expires_at<=clock_timestamp() or p.content_hash<>app.action_content_hash(p)
 then raise exception 'stale proposal' using errcode='40901'; end if;
 if input_decision not in ('approved','rejected') then raise exception 'invalid decision' using errcode='22023'; end if;
 if not exists(select 1 from app.crm_customer c where c.id=p.target_id and c.organization_id=p.organization_id
   and c.version=p.target_version and app.can_read_crm_scope(c.scope))
 then raise exception 'target unavailable' using errcode='42501'; end if;
 update app.action_proposal set status=input_decision where id=p.id;
 insert into app.action_approval(proposal_id,organization_id,decision,content_hash,decided_by)
 values(p.id,p.organization_id,input_decision,p.content_hash,app.current_actor_id());
 if input_decision='approved' then
  insert into app.trace_run(organization_id,run_kind,actor_id)
   values(p.organization_id,'worker',app.current_actor_id()) returning id into tr;
  insert into app.runtime_mission(organization_id,kind,created_by,idempotency_key,context_ref)
   values(p.organization_id,'action_execution',p.requested_by,'action:'||p.id,jsonb_build_object('proposalId',p.id)) returning id into m;
  insert into app.runtime_run(organization_id,mission_id,trace_run_id)
   values(p.organization_id,m,tr) returning id into r;
  insert into app.runtime_job(organization_id,run_id,idempotency_key,kind)
   values(p.organization_id,r,'action:'||p.id,'action_execution') returning id into j;
  insert into app.action_execution(proposal_id,organization_id,job_id,trace_run_id) values(p.id,p.organization_id,j,tr);
 end if;
 perform app.append_trace_event(p.trace_run_id,null,'proposal.reviewed','Action proposal reviewed',jsonb_build_object('proposalId',p.id,'decision',input_decision),null);
 return input_decision;
end $$;

create or replace function app.execute_customer_action(input_proposal uuid,input_job uuid,input_owner text,input_generation integer)
returns text language plpgsql security definer set search_path=app,pg_temp as $$
declare p app.action_proposal%rowtype; a app.action_approval%rowtype; e app.action_execution%rowtype;
 c app.crm_customer%rowtype; worker uuid:=app.current_actor_id(); valid boolean:=true; result text;
begin
 select * into p from app.action_proposal where id=input_proposal and organization_id=app.current_organization_id();
 if p.action_kind is distinct from 'customer_update' then raise exception 'wrong action' using errcode='22023';end if;
 select * into e from app.action_execution where proposal_id=input_proposal and job_id=input_job
  and organization_id=app.current_organization_id();
 if p.id is null or e.proposal_id is null or not app.lock_runtime_attempt(input_job,e.trace_run_id,input_owner,input_generation)
 then raise exception 'lease_lost' using errcode='42501'; end if;
 select * into e from app.action_execution where proposal_id=p.id and job_id=input_job for update;
 if e.proposal_id is null then raise exception 'execution unavailable' using errcode='42501'; end if;
 if e.state<>'pending' then return e.state; end if;
 select * into p from app.action_proposal where id=p.id for update;
 select * into a from app.action_approval where proposal_id=p.id for share;
 select * into c from app.crm_customer where id=p.target_id and organization_id=p.organization_id for update;
 valid:=p.status='approved' and a.decision='approved' and a.content_hash=p.content_hash
  and p.content_hash=app.action_content_hash(p) and p.expires_at>clock_timestamp()
  and c.id is not null and c.version=p.target_version and not c.action_opt_out
  and exists(select 1 from app.action_service_policy s where s.organization_id=p.organization_id
    and s.enabled and s.revision=p.service_revision);
 perform set_config('app.actor_id',p.requested_by::text,true);
 valid:=valid and app.crm_can_propose() and app.can_read_crm_scope(c.scope);
 perform set_config('app.actor_id',a.decided_by::text,true);
 valid:=valid and app.crm_can_review() and app.can_read_crm_scope(c.scope);
 perform set_config('app.actor_id',worker::text,true);
 valid:=coalesce(valid,false) and cardinality(p.evidence_artifact_ids)=(select count(*) from app.trace_artifact
   where id=any(p.evidence_artifact_ids) and organization_id=p.organization_id and run_id=p.trace_run_id
     and artifact_type='manual_action_evidence' and actor_id=p.requested_by
     and content_hash=encode(public.digest(convert_to(jsonb_build_object('artifact_type',artifact_type,
       'redacted_summary',redacted_summary,'redacted_payload',redacted_payload)::text,'UTF8'),'sha256'),'hex'));
 if valid then
  update app.crm_customer set display_name=p.content->>'displayName',version=version+1,updated_at=clock_timestamp() where id=c.id;
  result:='succeeded';
 else result:='rejected'; end if;
 update app.action_execution set state=result,result_code=case when valid then null else 'revalidation_failed' end,
  completed_at=clock_timestamp() where proposal_id=p.id;
 perform app.append_trace_event(e.trace_run_id,null,'action.'||result,'Controlled action finalized',
   jsonb_build_object('proposalId',p.id,'logicalActionId',e.logical_action_id,'state',result),null);
 return result;
end $$;
revoke all on function app.action_content_hash(app.action_proposal) from public;
revoke all on function app.execute_customer_action(uuid,uuid,text,integer) from public;
grant execute on function app.execute_customer_action(uuid,uuid,text,integer) to supplyagent_api;
commit;
