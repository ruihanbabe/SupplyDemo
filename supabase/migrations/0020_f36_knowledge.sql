-- F36 P1：组织知识、不可变版本、角色 ACL、Chunk 与 Citation 有效性。
begin;

create table if not exists app.knowledge_document (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  title text not null check (char_length(title) between 1 and 240),
  document_kind text not null check (document_kind in ('product_supply','sales_guidance','business_policy')),
  visibility text not null check (visibility in ('organization','restricted')),
  source_kind text not null check (source_kind in ('official','synthetic')),
  source_url text check (source_url is null or char_length(source_url) between 1 and 2000),
  created_by uuid not null,
  created_at timestamptz not null default now(),
  unique (organization_id, id)
);

create table if not exists app.knowledge_document_acl (
  document_id uuid not null,
  organization_id uuid not null references app.organization(id) on delete restrict,
  role text not null check (role in (
    'organization_admin', 'sales_manager', 'buyer', 'approver',
    'hardware_engineer', 'planner', 'auditor', 'maintainer'
  )),
  granted_by uuid not null,
  granted_at timestamptz not null default now(),
  primary key (document_id, role),
  foreign key (organization_id, document_id)
    references app.knowledge_document(organization_id, id) on delete restrict
);

create table if not exists app.knowledge_document_version (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  document_id uuid not null,
  version_number integer not null check (version_number > 0),
  version_label text not null check (char_length(version_label) between 1 and 120),
  source_revision text not null check (char_length(source_revision) between 1 and 240),
  content_hash text not null check (content_hash ~ '^[0-9a-f]{64}$'),
  content_bytes integer not null check (content_bytes > 0 and content_bytes <= 5242880),
  published_by uuid not null,
  published_at timestamptz not null default now(),
  unique (organization_id, document_id, version_number),
  unique (organization_id, document_id, content_hash),
  unique (organization_id, id, document_id),
  unique (organization_id, id),
  foreign key (organization_id, document_id)
    references app.knowledge_document(organization_id, id) on delete restrict
);

create table if not exists app.knowledge_chunk (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references app.organization(id) on delete restrict,
  document_id uuid not null,
  version_id uuid not null,
  ordinal integer not null check (ordinal >= 0 and ordinal < 10000),
  locator text not null check (char_length(locator) between 1 and 240),
  body text not null check (char_length(body) between 1 and 20000),
  fragment_hash text not null check (fragment_hash ~ '^[0-9a-f]{64}$'),
  body_search tsvector generated always as (to_tsvector('simple', body)) stored,
  unique (organization_id, version_id, ordinal),
  unique (organization_id, version_id, locator),
  foreign key (organization_id, document_id)
    references app.knowledge_document(organization_id, id) on delete restrict,
  foreign key (organization_id, version_id, document_id)
    references app.knowledge_document_version(organization_id, id, document_id) on delete restrict
);

create table if not exists app.knowledge_version_revocation (
  version_id uuid primary key,
  organization_id uuid not null references app.organization(id) on delete restrict,
  reason text not null check (char_length(reason) between 1 and 1000),
  revoked_by uuid not null,
  revoked_at timestamptz not null default now(),
  foreign key (organization_id, version_id)
    references app.knowledge_document_version(organization_id, id) on delete restrict
);

create table if not exists app.knowledge_document_withdrawal (
  document_id uuid primary key,
  organization_id uuid not null references app.organization(id) on delete restrict,
  reason text not null check (char_length(reason) between 1 and 1000),
  withdrawn_by uuid not null,
  withdrawn_at timestamptz not null default now(),
  foreign key (organization_id, document_id)
    references app.knowledge_document(organization_id, id) on delete restrict
);

create index if not exists knowledge_document_org_kind_idx
  on app.knowledge_document (organization_id, document_kind, created_at desc);
create index if not exists knowledge_version_document_idx
  on app.knowledge_document_version (organization_id, document_id, version_number desc);
create index if not exists knowledge_chunk_search_idx on app.knowledge_chunk using gin (body_search);

create or replace function app.knowledge_can_manage()
returns boolean language sql stable security definer set search_path = app, pg_temp as $$
  select exists (
    select 1 from app.organization_member
    where organization_id = app.current_organization_id()
      and user_id = app.current_actor_id()
      and role in ('organization_admin', 'maintainer')
  )
$$;

create or replace function app.knowledge_can_read(target_document_id uuid)
returns boolean language sql stable security definer set search_path = app, pg_temp as $$
  select exists (
    select 1
    from app.knowledge_document d
    join app.organization_member m
      on m.organization_id = d.organization_id and m.user_id = app.current_actor_id()
    where d.id = target_document_id
      and d.organization_id = app.current_organization_id()
      and not exists (
        select 1 from app.knowledge_document_withdrawal w where w.document_id = d.id
      )
      and (
        d.visibility = 'organization'
        or m.role in ('organization_admin', 'maintainer')
        or exists (
          select 1 from app.knowledge_document_acl a
          where a.document_id = d.id and a.organization_id = d.organization_id and a.role = m.role
        )
      )
  )
$$;

create or replace function app.create_knowledge_document(
  input_title text,
  input_document_kind text,
  input_visibility text,
  input_source_kind text,
  input_source_url text default null
)
returns uuid language plpgsql security definer set search_path = app, public, pg_temp as $$
declare document_id uuid;
begin
  if not app.knowledge_can_manage() then
    raise exception 'knowledge manage capability required' using errcode = '42501';
  end if;
  insert into app.knowledge_document (
    organization_id, title, document_kind, visibility, source_kind, source_url, created_by
  ) values (
    app.current_organization_id(), input_title, input_document_kind, input_visibility,
    input_source_kind, input_source_url, app.current_actor_id()
  ) returning id into document_id;
  return document_id;
end;
$$;

create or replace function app.grant_knowledge_document_role(input_document_id uuid, input_role text)
returns void language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if not app.knowledge_can_manage() then
    raise exception 'knowledge manage capability required' using errcode = '42501';
  end if;
  insert into app.knowledge_document_acl (document_id, organization_id, role, granted_by)
  select d.id, d.organization_id, input_role, app.current_actor_id()
  from app.knowledge_document d
  where d.id = input_document_id and d.organization_id = app.current_organization_id()
  on conflict (document_id, role) do nothing;
  if not found then
    raise exception 'knowledge document not found in organization' using errcode = '42501';
  end if;
end;
$$;

create or replace function app.publish_knowledge_version(
  input_document_id uuid,
  input_version_label text,
  input_source_revision text,
  input_normalized_content text,
  input_chunks jsonb
)
returns uuid language plpgsql security definer set search_path = app, public, pg_temp as $$
declare new_version_id uuid;
declare next_version integer;
declare item jsonb;
declare item_body text;
begin
  if not app.knowledge_can_manage() then
    raise exception 'knowledge manage capability required' using errcode = '42501';
  end if;
  if input_normalized_content is null or octet_length(input_normalized_content) not between 1 and 5242880 then
    raise exception 'normalized content must be 1-5242880 bytes' using errcode = '22023';
  end if;
  if jsonb_typeof(input_chunks) <> 'array' or jsonb_array_length(input_chunks) not between 1 and 1000 then
    raise exception 'chunks must be an array of 1-1000 items' using errcode = '22023';
  end if;
  if not exists (
    select 1 from app.knowledge_document d
    where d.id = input_document_id and d.organization_id = app.current_organization_id()
      and not exists (select 1 from app.knowledge_document_withdrawal w where w.document_id = d.id)
  ) then
    raise exception 'active knowledge document not found in organization' using errcode = '42501';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(input_document_id::text, 0));
  select coalesce(max(version_number), 0) + 1 into next_version
  from app.knowledge_document_version
  where organization_id = app.current_organization_id() and document_id = input_document_id;
  insert into app.knowledge_document_version (
    organization_id, document_id, version_number, version_label, source_revision,
    content_hash, content_bytes, published_by
  ) values (
    app.current_organization_id(), input_document_id, next_version, input_version_label,
    input_source_revision,
    encode(public.digest(convert_to(input_normalized_content, 'UTF8'), 'sha256'), 'hex'),
    octet_length(input_normalized_content), app.current_actor_id()
  ) returning id into new_version_id;
  for item in select value from jsonb_array_elements(input_chunks) loop
    item_body := item->>'body';
    if jsonb_typeof(item) <> 'object'
       or (item->>'ordinal') is null
       or (item->>'locator') is null
       or item_body is null then
      raise exception 'each chunk requires ordinal, locator and body' using errcode = '22023';
    end if;
    insert into app.knowledge_chunk (
      organization_id, document_id, version_id, ordinal, locator, body, fragment_hash
    ) values (
      app.current_organization_id(), input_document_id, new_version_id,
      (item->>'ordinal')::integer, item->>'locator', item_body,
      encode(public.digest(convert_to(item_body, 'UTF8'), 'sha256'), 'hex')
    );
  end loop;
  return new_version_id;
end;
$$;

create or replace function app.revoke_knowledge_version(input_version_id uuid, input_reason text)
returns void language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if not app.knowledge_can_manage() then
    raise exception 'knowledge manage capability required' using errcode = '42501';
  end if;
  insert into app.knowledge_version_revocation (version_id, organization_id, reason, revoked_by)
  select v.id, v.organization_id, input_reason, app.current_actor_id()
  from app.knowledge_document_version v
  where v.id = input_version_id and v.organization_id = app.current_organization_id();
  if not found then
    raise exception 'knowledge version not found in organization' using errcode = '42501';
  end if;
end;
$$;

create or replace function app.withdraw_knowledge_document(input_document_id uuid, input_reason text)
returns void language plpgsql security definer set search_path = app, pg_temp as $$
begin
  if not app.knowledge_can_manage() then
    raise exception 'knowledge manage capability required' using errcode = '42501';
  end if;
  insert into app.knowledge_document_withdrawal (document_id, organization_id, reason, withdrawn_by)
  select d.id, d.organization_id, input_reason, app.current_actor_id()
  from app.knowledge_document d
  where d.id = input_document_id and d.organization_id = app.current_organization_id();
  if not found then
    raise exception 'knowledge document not found in organization' using errcode = '42501';
  end if;
end;
$$;

create or replace function app.knowledge_citation_is_valid(
  input_version_id uuid,
  input_locator text,
  input_fragment_hash text
)
returns boolean language sql stable security invoker set search_path = app, pg_temp as $$
  select exists (
    select 1
    from app.knowledge_chunk c
    join app.knowledge_document_version v on v.id = c.version_id
    where c.version_id = input_version_id
      and c.locator = input_locator
      and c.fragment_hash = input_fragment_hash
      and app.knowledge_can_read(c.document_id)
      and not exists (
        select 1 from app.knowledge_version_revocation r where r.version_id = v.id
      )
      and v.version_number = (
        select max(latest.version_number)
        from app.knowledge_document_version latest
        where latest.organization_id = v.organization_id and latest.document_id = v.document_id
      )
  )
$$;

create or replace function app.search_knowledge(input_query text, input_limit integer default 20)
returns table (
  document_id uuid, document_title text, document_kind text, version_id uuid,
  version_number integer, chunk_id uuid, locator text, body text, fragment_hash text, rank real
)
language sql stable security invoker set search_path = app, pg_temp as $$
  select d.id, d.title, d.document_kind, v.id, v.version_number,
         c.id, c.locator, c.body, c.fragment_hash,
         ts_rank(c.body_search, websearch_to_tsquery('simple', input_query)) as rank
  from app.knowledge_chunk c
  join app.knowledge_document_version v on v.id = c.version_id
  join app.knowledge_document d on d.id = c.document_id
  where input_query is not null and char_length(input_query) between 1 and 500
    and input_limit between 1 and 100
    and c.body_search @@ websearch_to_tsquery('simple', input_query)
    and app.knowledge_can_read(d.id)
    and not exists (select 1 from app.knowledge_version_revocation r where r.version_id = v.id)
    and v.version_number = (
      select max(latest.version_number)
      from app.knowledge_document_version latest
      where latest.organization_id = v.organization_id and latest.document_id = v.document_id
    )
  order by rank desc, d.id, v.version_number desc, c.ordinal
  limit input_limit
$$;

alter table app.knowledge_document enable row level security;
alter table app.knowledge_document force row level security;
alter table app.knowledge_document_acl enable row level security;
alter table app.knowledge_document_acl force row level security;
alter table app.knowledge_document_version enable row level security;
alter table app.knowledge_document_version force row level security;
alter table app.knowledge_chunk enable row level security;
alter table app.knowledge_chunk force row level security;
alter table app.knowledge_version_revocation enable row level security;
alter table app.knowledge_version_revocation force row level security;
alter table app.knowledge_document_withdrawal enable row level security;
alter table app.knowledge_document_withdrawal force row level security;

drop policy if exists knowledge_document_read on app.knowledge_document;
create policy knowledge_document_read on app.knowledge_document for select to supplyagent_api
  using (app.knowledge_can_read(id));
drop policy if exists knowledge_acl_read on app.knowledge_document_acl;
create policy knowledge_acl_read on app.knowledge_document_acl for select to supplyagent_api
  using (organization_id = app.current_organization_id() and app.knowledge_can_read(document_id));
drop policy if exists knowledge_version_read on app.knowledge_document_version;
create policy knowledge_version_read on app.knowledge_document_version for select to supplyagent_api
  using (organization_id = app.current_organization_id() and app.knowledge_can_read(document_id));
drop policy if exists knowledge_chunk_read on app.knowledge_chunk;
create policy knowledge_chunk_read on app.knowledge_chunk for select to supplyagent_api
  using (organization_id = app.current_organization_id() and app.knowledge_can_read(document_id));
drop policy if exists knowledge_revocation_read on app.knowledge_version_revocation;
create policy knowledge_revocation_read on app.knowledge_version_revocation for select to supplyagent_api
  using (organization_id = app.current_organization_id() and exists (
    select 1 from app.knowledge_document_version v
    where v.id = version_id and app.knowledge_can_read(v.document_id)
  ));
drop policy if exists knowledge_withdrawal_read on app.knowledge_document_withdrawal;
create policy knowledge_withdrawal_read on app.knowledge_document_withdrawal for select to supplyagent_api
  using (organization_id = app.current_organization_id() and app.knowledge_can_manage());

create or replace function app.reject_knowledge_mutation()
returns trigger language plpgsql as $$
begin
  raise exception 'knowledge content is immutable' using errcode = '55000';
end;
$$;
drop trigger if exists knowledge_version_immutable on app.knowledge_document_version;
create trigger knowledge_version_immutable before update or delete on app.knowledge_document_version
  for each row execute function app.reject_knowledge_mutation();
drop trigger if exists knowledge_chunk_immutable on app.knowledge_chunk;
create trigger knowledge_chunk_immutable before update or delete on app.knowledge_chunk
  for each row execute function app.reject_knowledge_mutation();
drop trigger if exists knowledge_revocation_immutable on app.knowledge_version_revocation;
create trigger knowledge_revocation_immutable before update or delete on app.knowledge_version_revocation
  for each row execute function app.reject_knowledge_mutation();
drop trigger if exists knowledge_withdrawal_immutable on app.knowledge_document_withdrawal;
create trigger knowledge_withdrawal_immutable before update or delete on app.knowledge_document_withdrawal
  for each row execute function app.reject_knowledge_mutation();

revoke all on app.knowledge_document, app.knowledge_document_acl,
  app.knowledge_document_version, app.knowledge_chunk,
  app.knowledge_version_revocation, app.knowledge_document_withdrawal from public;
grant select on app.knowledge_document, app.knowledge_document_acl,
  app.knowledge_document_version, app.knowledge_chunk,
  app.knowledge_version_revocation, app.knowledge_document_withdrawal to supplyagent_api;
revoke all on function app.knowledge_can_manage() from public;
revoke all on function app.knowledge_can_read(uuid) from public;
revoke all on function app.create_knowledge_document(text,text,text,text,text) from public;
revoke all on function app.grant_knowledge_document_role(uuid,text) from public;
revoke all on function app.publish_knowledge_version(uuid,text,text,text,jsonb) from public;
revoke all on function app.revoke_knowledge_version(uuid,text) from public;
revoke all on function app.withdraw_knowledge_document(uuid,text) from public;
revoke all on function app.knowledge_citation_is_valid(uuid,text,text) from public;
revoke all on function app.search_knowledge(text,integer) from public;
grant execute on function app.knowledge_can_manage() to supplyagent_api;
grant execute on function app.knowledge_can_read(uuid) to supplyagent_api;
grant execute on function app.create_knowledge_document(text,text,text,text,text) to supplyagent_api;
grant execute on function app.grant_knowledge_document_role(uuid,text) to supplyagent_api;
grant execute on function app.publish_knowledge_version(uuid,text,text,text,jsonb) to supplyagent_api;
grant execute on function app.revoke_knowledge_version(uuid,text) to supplyagent_api;
grant execute on function app.withdraw_knowledge_document(uuid,text) to supplyagent_api;
grant execute on function app.knowledge_citation_is_valid(uuid,text,text) to supplyagent_api;
grant execute on function app.search_knowledge(text,integer) to supplyagent_api;

commit;
