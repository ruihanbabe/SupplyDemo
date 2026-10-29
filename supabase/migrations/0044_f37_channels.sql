-- F37 encrypted local fixture transport; no real channel is enabled by this migration.
begin;
alter table app.action_proposal drop constraint if exists action_proposal_action_kind_check;
alter table app.action_proposal add constraint action_proposal_action_kind_check check(action_kind in ('customer_update','message_send'));
create table if not exists app.channel_send_ledger(
 proposal_id uuid primary key references app.action_proposal(id),
 organization_id uuid not null references app.organization(id),
 logical_action_id uuid not null unique references app.action_execution(logical_action_id),
 state text not null check(state in ('sending','accepted','delivered','failed','reconciliation_required','rejected')),
 receipt text, updated_at timestamptz not null default clock_timestamp()
);
create table if not exists app.channel_inbox(
 organization_id uuid not null references app.organization(id), event_id text not null,
 logical_action_id uuid not null references app.channel_send_ledger(logical_action_id),
 payload_tag text not null, sealed_payload text not null, received_at timestamptz not null default clock_timestamp(),
 primary key(organization_id,event_id)
);
alter table app.channel_send_ledger enable row level security;
alter table app.channel_send_ledger force row level security;
alter table app.channel_inbox enable row level security;
alter table app.channel_inbox force row level security;
drop policy if exists channel_send_read on app.channel_send_ledger;
create policy channel_send_read on app.channel_send_ledger for select to supplyagent_api using(
 organization_id=app.current_organization_id() and app.is_member(organization_id,app.current_actor_id()));
drop policy if exists channel_inbox_read on app.channel_inbox;
create policy channel_inbox_read on app.channel_inbox for select to supplyagent_api using(
 organization_id=app.current_organization_id() and app.is_runtime_worker());
revoke all on app.channel_send_ledger,app.channel_inbox from public;
grant select on app.channel_send_ledger,app.channel_inbox to supplyagent_api;

create or replace function app.action_preflight(input_proposal uuid,input_job uuid,input_owner text,input_generation integer)
returns boolean language plpgsql security definer set search_path=app,pg_temp as $$
declare p app.action_proposal%rowtype; a app.action_approval%rowtype; e app.action_execution%rowtype;
 c app.crm_customer%rowtype; worker uuid:=app.current_actor_id(); valid boolean;
begin
 select * into e from app.action_execution where proposal_id=input_proposal and job_id=input_job
   and organization_id=app.current_organization_id();
 if e.proposal_id is null or not app.lock_runtime_attempt(input_job,e.trace_run_id,input_owner,input_generation)
 then raise exception 'lease_lost' using errcode='42501'; end if;
 select * into e from app.action_execution where proposal_id=input_proposal for update;
 select * into p from app.action_proposal where id=input_proposal for update;
 select * into a from app.action_approval where proposal_id=p.id for share;
 select * into c from app.crm_customer where id=p.target_id and organization_id=p.organization_id for update;
 valid:=p.status='approved' and a.decision='approved' and a.content_hash=p.content_hash
   and p.content_hash=app.action_content_hash(p) and p.expires_at>clock_timestamp()
   and c.id is not null and c.version=p.target_version and not c.action_opt_out
   and exists(select 1 from app.action_service_policy s where s.organization_id=p.organization_id and s.enabled and s.revision=p.service_revision);
 perform set_config('app.actor_id',p.requested_by::text,true);
 valid:=valid and app.crm_can_propose() and app.can_read_crm_scope(c.scope);
 perform set_config('app.actor_id',a.decided_by::text,true);
 valid:=valid and app.crm_can_review() and app.can_read_crm_scope(c.scope);
 perform set_config('app.actor_id',worker::text,true);
 return coalesce(valid,false) and cardinality(p.evidence_artifact_ids)=(select count(*) from app.trace_artifact
   where id=any(p.evidence_artifact_ids) and organization_id=p.organization_id and run_id=p.trace_run_id
     and artifact_type='manual_action_evidence' and actor_id=p.requested_by
     and content_hash=encode(public.digest(convert_to(jsonb_build_object('artifact_type',artifact_type,
       'redacted_summary',redacted_summary,'redacted_payload',redacted_payload)::text,'UTF8'),'sha256'),'hex'));
end $$;

create or replace function app.create_message_proposal(
 input_target uuid,input_version integer,input_content jsonb,input_evidence uuid[],input_expires timestamptz,input_key text
) returns uuid language plpgsql security definer set search_path=app,pg_temp as $$
declare p app.action_proposal%rowtype; tr uuid;
begin
 if not app.crm_can_propose() then raise exception 'forbidden' using errcode='42501'; end if;
 if input_key is null or char_length(input_key) not between 1 and 120 or input_expires<=clock_timestamp()
   or input_expires>clock_timestamp()+make_interval(secs=>coalesce((select max_approval_seconds from app.action_service_policy where organization_id=app.current_organization_id()),86400)) or input_content->>'channel'<>'fixture'
   or jsonb_typeof(input_content->'sealedPayload')<>'string' or char_length(input_content->>'sealedPayload') not between 100 and 32768
   or not input_content ?& array['channel','sealedPayload','payloadTag','keyId']
   or (select count(*) from jsonb_object_keys(input_content))<>4
   or coalesce(input_content->>'payloadTag','') !~ '^[a-f0-9]{64}$'
   or coalesce(input_content->>'keyId','') !~ '^[a-f0-9]{64}$'
 then raise exception 'invalid message' using errcode='22023'; end if;
 if not exists(select 1 from app.crm_customer c where c.id=input_target and c.organization_id=app.current_organization_id()
   and c.version=input_version and app.can_read_crm_scope(c.scope)) then raise exception 'target unavailable' using errcode='42501'; end if;
 perform pg_advisory_xact_lock(hashtextextended(app.current_organization_id()::text||':'||app.current_actor_id()::text||':'||input_key,0));
 select * into p from app.action_proposal where organization_id=app.current_organization_id()
   and requested_by=app.current_actor_id() and idempotency_key=input_key;
 if p.id is not null then
  if p.action_kind<>'message_send' or p.target_id<>input_target or p.target_version<>input_version
   or p.expires_at<>input_expires or p.evidence_artifact_ids is distinct from input_evidence
   or p.content->>'payloadTag' is distinct from input_content->>'payloadTag'
  then raise exception 'idempotency conflict' using errcode='40901'; end if;
  return p.id;
 end if;
 select run_id into tr from app.trace_artifact where id=input_evidence[1] and organization_id=app.current_organization_id();
 if input_evidence is null or cardinality(input_evidence) not between 1 and 16 or cardinality(input_evidence)<>(select count(distinct id)
   from app.trace_artifact where id=any(input_evidence) and organization_id=app.current_organization_id()
   and run_id=tr and actor_id=app.current_actor_id() and artifact_type='manual_action_evidence'
   and content_hash=encode(public.digest(convert_to(jsonb_build_object('artifact_type',artifact_type,
       'redacted_summary',redacted_summary,'redacted_payload',redacted_payload)::text,'UTF8'),'sha256'),'hex'))
 then raise exception 'evidence unavailable' using errcode='42501'; end if;
 p.id:=gen_random_uuid();p.organization_id:=app.current_organization_id();p.action_kind:='message_send';
 p.target_id:=input_target;p.target_version:=input_version;p.content:=input_content;p.evidence_artifact_ids:=input_evidence;
 p.risk_level:='r1';p.status:='pending_review';p.expires_at:=input_expires;p.requested_by:=app.current_actor_id();
 p.trace_run_id:=tr;p.created_at:=clock_timestamp();p.idempotency_key:=input_key;
 select coalesce((select revision from app.action_service_policy where organization_id=p.organization_id),0) into p.service_revision;
 p.content_hash:=app.action_content_hash(p);insert into app.action_proposal select p.*;
 perform app.append_trace_event(tr,null,'message.proposed','Encrypted message proposed',jsonb_build_object('proposalId',p.id),null);
 return p.id;
end $$;

create or replace function app.prepare_channel_send(input_proposal uuid,input_job uuid,input_owner text,input_generation integer)
returns jsonb language plpgsql security definer set search_path=app,pg_temp as $$
declare p app.action_proposal%rowtype;e app.action_execution%rowtype;s app.channel_send_ledger%rowtype;valid boolean;
begin
 select * into e from app.action_execution where proposal_id=input_proposal and job_id=input_job and organization_id=app.current_organization_id();
 if e.proposal_id is null or not app.lock_runtime_attempt(input_job,e.trace_run_id,input_owner,input_generation)
 then raise exception 'lease_lost' using errcode='42501';end if;
 select * into e from app.action_execution where proposal_id=input_proposal for update;
 select * into p from app.action_proposal where id=input_proposal for update;
 if p.action_kind<>'message_send' then raise exception 'wrong action' using errcode='22023';end if;
 select * into s from app.channel_send_ledger where proposal_id=p.id for update;
 if s.proposal_id is not null then return jsonb_build_object('logicalActionId',s.logical_action_id,'state',s.state,'maySend',false,'receipt',s.receipt);end if;
 valid:=app.action_preflight(input_proposal,input_job,input_owner,input_generation);
 insert into app.channel_send_ledger(proposal_id,organization_id,logical_action_id,state)
 values(p.id,p.organization_id,e.logical_action_id,case when valid then 'sending' else 'rejected' end);
 if not valid then update app.action_execution set state='rejected',result_code='revalidation_failed',completed_at=clock_timestamp() where proposal_id=p.id;end if;
 perform app.append_trace_event(e.trace_run_id,null,'send.intent','Controlled send intent',jsonb_build_object('logicalActionId',e.logical_action_id,'authorized',valid),null);
 return jsonb_build_object('logicalActionId',e.logical_action_id,'state',case when valid then 'sending' else 'rejected' end,
   'maySend',valid,'payload',case when valid then p.content else null end,'targetId',p.target_id,'targetVersion',p.target_version,'requestedBy',p.requested_by);
end $$;

create or replace function app.finish_channel_send(input_proposal uuid,input_job uuid,input_owner text,input_generation integer,input_state text,input_receipt text)
returns text language plpgsql security definer set search_path=app,pg_temp as $$
declare e app.action_execution%rowtype;s app.channel_send_ledger%rowtype;
begin
 select * into e from app.action_execution where proposal_id=input_proposal and job_id=input_job and organization_id=app.current_organization_id();
 if e.proposal_id is null or not app.lock_runtime_attempt(input_job,e.trace_run_id,input_owner,input_generation)
 then raise exception 'lease_lost' using errcode='42501';end if;
 select * into s from app.channel_send_ledger where proposal_id=input_proposal for update;
 if s.proposal_id is null or input_state not in ('accepted','failed','reconciliation_required')
  or (input_state='accepted' and (input_receipt is null or char_length(input_receipt) not between 1 and 160))
 then raise exception 'invalid receipt' using errcode='22023';end if;
 if s.state in ('accepted','delivered','failed','rejected') then return s.state;end if;
 update app.channel_send_ledger set state=input_state,receipt=input_receipt,updated_at=clock_timestamp() where proposal_id=input_proposal;
 if input_state in ('accepted','failed') then
  update app.action_execution set state=case when input_state='accepted' then 'succeeded' else 'rejected' end,
   result_code=case when input_state='failed' then 'channel_failed' else null end,completed_at=clock_timestamp() where proposal_id=input_proposal;
 end if;
 perform app.append_trace_event(e.trace_run_id,null,'send.'||input_state,'Controlled send result',jsonb_build_object('logicalActionId',e.logical_action_id,'state',input_state),null);
 return input_state;
end $$;

create or replace function app.record_channel_receipt(input_logical uuid,input_event text,input_state text,input_receipt text,input_tag text,input_sealed text)
returns text language plpgsql security definer set search_path=app,pg_temp as $$
declare s app.channel_send_ledger%rowtype;prior app.channel_inbox%rowtype;tr uuid;
begin
 if not app.is_runtime_worker() then raise exception 'worker required' using errcode='42501';end if;
 if input_event is null or input_state is null or input_receipt is null or input_tag is null or input_sealed is null
  or char_length(input_event) not between 1 and 160 or input_state not in ('delivered','failed')
  or char_length(input_receipt) not between 1 and 160 or input_tag !~ '^[a-f0-9]{64}$'
  or char_length(input_sealed) not between 100 and 32768 then raise exception 'invalid callback' using errcode='22023';end if;
 select * into s from app.channel_send_ledger where logical_action_id=input_logical and organization_id=app.current_organization_id() for update;
 if s.proposal_id is null then raise exception 'unknown receipt' using errcode='42501';end if;
 select * into prior from app.channel_inbox where organization_id=s.organization_id and event_id=input_event;
 if prior.event_id is not null then
  if prior.payload_tag<>input_tag then raise exception 'event conflict' using errcode='40901';end if;
  return s.state;
 end if;
 if s.receipt is not null and s.receipt<>input_receipt then raise exception 'receipt mismatch' using errcode='40901';end if;
 insert into app.channel_inbox(organization_id,event_id,logical_action_id,payload_tag,sealed_payload)
 values(s.organization_id,input_event,input_logical,input_tag,input_sealed);
 if s.state not in ('delivered','failed','rejected') then
  update app.channel_send_ledger set state=input_state,receipt=input_receipt,updated_at=clock_timestamp() where proposal_id=s.proposal_id;
  update app.action_execution set state=case when input_state='delivered' then 'succeeded' else 'rejected' end,
   result_code=case when input_state='failed' then 'channel_failed' else null end,completed_at=clock_timestamp() where proposal_id=s.proposal_id;
 end if;
 select trace_run_id into tr from app.action_execution where proposal_id=s.proposal_id;
 perform app.append_trace_event(tr,null,'send.receipt','Verified fixture receipt',jsonb_build_object('logicalActionId',input_logical,'receivedState',input_state,
   'state',(select state from app.channel_send_ledger where proposal_id=s.proposal_id)),null);
 return (select state from app.channel_send_ledger where proposal_id=s.proposal_id);
end $$;
revoke all on function app.action_preflight(uuid,uuid,text,integer) from public;
revoke all on function app.create_message_proposal(uuid,integer,jsonb,uuid[],timestamptz,text),app.prepare_channel_send(uuid,uuid,text,integer),app.finish_channel_send(uuid,uuid,text,integer,text,text),app.record_channel_receipt(uuid,text,text,text,text,text) from public;
grant execute on function app.create_message_proposal(uuid,integer,jsonb,uuid[],timestamptz,text),app.prepare_channel_send(uuid,uuid,text,integer),app.finish_channel_send(uuid,uuid,text,integer,text,text),app.record_channel_receipt(uuid,text,text,text,text,text) to supplyagent_api;
grant execute on function app.action_preflight(uuid,uuid,text,integer) to supplyagent_api;
commit;
