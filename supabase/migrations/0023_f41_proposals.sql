-- F41: reviewable, non-executable action proposals. Execution is a later migration.
begin;

create table if not exists app.action_proposal (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  action_kind text not null check (action_kind in ('customer_update')),
  target_id uuid not null,
  target_version integer not null check (target_version > 0),
  content jsonb not null check (jsonb_typeof(content) = 'object'),
  content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  evidence_artifact_ids uuid[] not null check (cardinality(evidence_artifact_ids) between 1 and 16),
  risk_level text not null check (risk_level = 'r1'),
  status text not null default 'pending_review' check (status in ('pending_review','approved','rejected')),
  expires_at timestamptz not null,
  requested_by uuid not null,
  trace_run_id uuid not null references app.trace_run(id) on delete restrict,
  created_at timestamptz not null default now()
);

create table if not exists app.action_approval (
  proposal_id uuid primary key references app.action_proposal(id) on delete restrict,
  organization_id uuid not null references app.organization(id) on delete restrict,
  decision text not null check (decision in ('approved','rejected')),
  content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  decided_by uuid not null,
  decided_at timestamptz not null default now()
);

alter table app.action_proposal enable row level security;
alter table app.action_proposal force row level security;
alter table app.action_approval enable row level security;
alter table app.action_approval force row level security;
create policy action_proposal_read on app.action_proposal for select to supplyagent_api
  using (organization_id = app.current_organization_id() and app.is_member(organization_id, app.current_actor_id()));
create policy action_approval_read on app.action_approval for select to supplyagent_api
  using (organization_id = app.current_organization_id() and app.is_member(organization_id, app.current_actor_id()));

create function app.create_action_proposal(
  input_target_id uuid, input_target_version integer, input_content jsonb,
  input_evidence_artifact_ids uuid[], input_expires_at timestamptz
) returns table(proposal_id uuid, proposal_status text)
language plpgsql security definer set search_path = app, public, pg_temp as $$
declare evidence_run_id uuid; calculated_hash text;
begin
  if not app.crm_can_propose() then
    raise exception 'proposal capability required' using errcode = '42501';
  end if;
  if input_expires_at <= now() or input_target_version <= 0 then
    raise exception 'proposal expiry or target version invalid' using errcode = '22023';
  end if;
  if jsonb_typeof(input_content) <> 'object'
    or jsonb_object_length(input_content) <> 1
    or not input_content ? 'displayName'
    or jsonb_typeof(input_content->'displayName') <> 'string'
    or char_length(input_content->>'displayName') not between 1 and 160 then
    raise exception 'invalid customer update content' using errcode = '22023';
  end if;
  if not exists (
    select 1 from app.crm_customer where id = input_target_id
      and organization_id = app.current_organization_id() and version = input_target_version
  ) then raise exception 'target missing or stale' using errcode = '40901'; end if;
  if cardinality(input_evidence_artifact_ids) not between 1 and 16
    or cardinality(input_evidence_artifact_ids) <> (
      select count(distinct a.id) from app.trace_artifact a
      where a.id = any(input_evidence_artifact_ids)
        and a.organization_id = app.current_organization_id()
    ) then raise exception 'evidence missing or outside organization' using errcode = '42501'; end if;
  select run_id into evidence_run_id from app.trace_artifact
    where id = input_evidence_artifact_ids[1] and organization_id = app.current_organization_id();
  calculated_hash := encode(public.digest(convert_to(input_content::text, 'UTF8'), 'sha256'), 'hex');
  insert into app.action_proposal(organization_id,action_kind,target_id,target_version,content,
    content_hash,evidence_artifact_ids,risk_level,expires_at,requested_by,trace_run_id)
  values(app.current_organization_id(),'customer_update',input_target_id,input_target_version,input_content,
    calculated_hash,input_evidence_artifact_ids,'r1',input_expires_at,app.current_actor_id(),evidence_run_id)
  returning id,status into proposal_id,proposal_status;
  perform app.append_trace_event(evidence_run_id,null,'proposal.created','Action proposal created',
    jsonb_build_object('proposalId',proposal_id,'contentHash',calculated_hash),null);
  return next;
end; $$;

create function app.review_action_proposal(input_proposal_id uuid, input_decision text)
returns text language plpgsql security definer set search_path = app, public, pg_temp as $$
declare current_proposal app.action_proposal%rowtype;
begin
  if not app.crm_can_review() then
    raise exception 'approval capability required' using errcode = '42501';
  end if;
  if input_decision not in ('approved','rejected') then
    raise exception 'invalid decision' using errcode = '22023';
  end if;
  select * into current_proposal from app.action_proposal
    where id=input_proposal_id and organization_id=app.current_organization_id() for update;
  if not found or current_proposal.status <> 'pending_review' then
    raise exception 'proposal missing or already decided' using errcode = '40901';
  end if;
  if current_proposal.expires_at <= now() then
    raise exception 'proposal expired' using errcode = '40901';
  end if;
  if current_proposal.requested_by = app.current_actor_id() then
    raise exception 'requester cannot approve own proposal' using errcode = '42501';
  end if;
  update app.action_proposal set status=input_decision where id=input_proposal_id;
  insert into app.action_approval(proposal_id,organization_id,decision,content_hash,decided_by)
  values(input_proposal_id,app.current_organization_id(),input_decision,current_proposal.content_hash,app.current_actor_id());
  perform app.append_trace_event(current_proposal.trace_run_id,null,'proposal.reviewed','Action proposal reviewed',
    jsonb_build_object('proposalId',input_proposal_id,'decision',input_decision),null);
  return input_decision;
end; $$;

revoke all on app.action_proposal, app.action_approval from public;
grant select on app.action_proposal, app.action_approval to supplyagent_api;
revoke all on function app.create_action_proposal(uuid,integer,jsonb,uuid[],timestamptz),
  app.review_action_proposal(uuid,text) from public;
grant execute on function app.create_action_proposal(uuid,integer,jsonb,uuid[],timestamptz),
  app.review_action_proposal(uuid,text) to supplyagent_api;
commit;
