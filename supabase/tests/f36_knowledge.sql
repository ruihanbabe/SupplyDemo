-- F36 P1：组织知识、角色 ACL、不可变版本、检索与 Citation 失效。
\set ON_ERROR_STOP on
begin;

insert into app.organization (id, slug, display_name) values
  ('36363636-3636-3636-3636-363636363636', 'knowledge-alpha', 'Knowledge Alpha'),
  ('46464646-4646-4646-4646-464646464646', 'knowledge-bravo', 'Knowledge Bravo')
on conflict (id) do nothing;
insert into app.organization_member (organization_id, user_id, role) values
  ('36363636-3636-3636-3636-363636363636', 'aaaaaaaa-3636-3636-3636-363636363636', 'organization_admin'),
  ('36363636-3636-3636-3636-363636363636', 'bbbbbbbb-3636-3636-3636-363636363636', 'sales_manager'),
  ('36363636-3636-3636-3636-363636363636', 'cccccccc-3636-3636-3636-363636363636', 'buyer'),
  ('46464646-4646-4646-4646-464646464646', 'dddddddd-4646-4646-4646-464646464646', 'organization_admin')
on conflict do nothing;

set local role supplyagent_api;
select app.set_request_context(
  'aaaaaaaa-3636-3636-3636-363636363636', '36363636-3636-3636-3636-363636363636'
);
select app.create_knowledge_document(
  'HackRF component change', 'product_supply', 'organization', 'official',
  'https://github.com/greatscottgadgets/hackrf/blob/main/docs/source/list_of_hardware_revisions.rst'
) as public_document_id \gset
select app.publish_knowledge_version(
  :'public_document_id', '2026-10-20', 'github-main-observed-2026-10-20',
  'HackRF revision 9 replaced MAX2837 with MAX2839 because component availability changed.',
  '[{"ordinal":0,"locator":"section:HackRF-One-r9","body":"HackRF revision 9 replaced MAX2837 with MAX2839 because component availability changed."}]'::jsonb
) as public_version_id \gset
select fragment_hash as public_fragment_hash
from app.knowledge_chunk where version_id = :'public_version_id'::uuid \gset

select app.create_knowledge_document(
  'Synthetic discount approval', 'business_policy', 'restricted', 'synthetic', null
) as restricted_document_id \gset
select app.grant_knowledge_document_role(:'restricted_document_id', 'sales_manager');
select app.publish_knowledge_version(
  :'restricted_document_id', 'v1', 'synthetic-policy-v1',
  'Discounts above the demo threshold require approver review.',
  '[{"ordinal":0,"locator":"rule:discount-approval","body":"Discounts above the demo threshold require approver review."}]'::jsonb
) as restricted_version_id \gset

-- psql variables are copied into transaction-local settings for PL/pgSQL assertions.
select set_config('test.public_version_id', :'public_version_id', true);
select set_config('test.public_fragment_hash', :'public_fragment_hash', true);
select set_config('test.public_document_id', :'public_document_id', true);
select set_config('test.restricted_document_id', :'restricted_document_id', true);

do $$
begin
  if (select count(*) from app.search_knowledge('MAX2837', 10)) <> 1 then
    raise exception 'organization knowledge search failed';
  end if;
  if not app.knowledge_citation_is_valid(
    current_setting('test.public_version_id')::uuid,
    'section:HackRF-One-r9', current_setting('test.public_fragment_hash')
  ) then raise exception 'valid citation rejected'; end if;
  if app.knowledge_citation_is_valid(
    current_setting('test.public_version_id')::uuid,
    'section:HackRF-One-r9', repeat('0', 64)
  ) then raise exception 'fabricated fragment hash accepted'; end if;
end;
$$;

-- sales_manager 可读组织文档和显式授予其角色的受限政策，但不能管理知识。
select app.set_request_context(
  'bbbbbbbb-3636-3636-3636-363636363636', '36363636-3636-3636-3636-363636363636'
);
do $$
begin
  if (select count(*) from app.knowledge_document) <> 2 then
    raise exception 'sales manager ACL read failed';
  end if;
  begin
    perform app.create_knowledge_document('forbidden', 'sales_guidance', 'organization', 'synthetic', null);
    raise exception 'sales manager unexpectedly managed knowledge';
  exception when insufficient_privilege then null;
  end;
end;
$$;

-- buyer 只能读取组织文档，不能读取 restricted 政策。
select app.set_request_context(
  'cccccccc-3636-3636-3636-363636363636', '36363636-3636-3636-3636-363636363636'
);
do $$
begin
  if (select count(*) from app.knowledge_document) <> 1 then
    raise exception 'buyer restricted ACL failed';
  end if;
end;
$$;

-- 另一组织看不到任何文档、版本或 chunk，也不能验证 citation。
select app.set_request_context(
  'dddddddd-4646-4646-4646-464646464646', '46464646-4646-4646-4646-464646464646'
);
do $$
begin
  if exists (select 1 from app.knowledge_document)
     or exists (select 1 from app.knowledge_document_version)
     or exists (select 1 from app.knowledge_chunk) then
    raise exception 'cross-organization knowledge leakage';
  end if;
  if app.knowledge_citation_is_valid(
    current_setting('test.public_version_id')::uuid,
    'section:HackRF-One-r9', current_setting('test.public_fragment_hash')
  ) then raise exception 'cross-organization citation accepted'; end if;
end;
$$;

-- 新版本自动使旧 citation 过期；撤销当前版本后也不能回退信任旧版本。
select app.set_request_context(
  'aaaaaaaa-3636-3636-3636-363636363636', '36363636-3636-3636-3636-363636363636'
);
select app.publish_knowledge_version(
  :'public_document_id', '2026-10-21', 'github-main-observed-2026-10-21',
  'HackRF revision 9 uses MAX2839 in place of MAX2837; confirm the product revision before action.',
  '[{"ordinal":0,"locator":"section:HackRF-One-r9","body":"HackRF revision 9 uses MAX2839 in place of MAX2837; confirm the product revision before action."}]'::jsonb
) as latest_version_id \gset
select fragment_hash as latest_fragment_hash
from app.knowledge_chunk where version_id = :'latest_version_id'::uuid \gset
select set_config('test.latest_version_id', :'latest_version_id', true);
select set_config('test.latest_fragment_hash', :'latest_fragment_hash', true);
do $$
begin
  if app.knowledge_citation_is_valid(
    current_setting('test.public_version_id')::uuid,
    'section:HackRF-One-r9', current_setting('test.public_fragment_hash')
  ) then raise exception 'superseded citation remained valid'; end if;
  if not app.knowledge_citation_is_valid(
    current_setting('test.latest_version_id')::uuid,
    'section:HackRF-One-r9', current_setting('test.latest_fragment_hash')
  ) then raise exception 'latest citation rejected'; end if;
  begin
    update app.knowledge_chunk set body = 'tampered'
    where version_id = current_setting('test.public_version_id')::uuid;
    raise exception 'knowledge chunk mutation unexpectedly succeeded';
  exception when insufficient_privilege or sqlstate '55000' then null;
  end;
end;
$$;
select app.revoke_knowledge_version(:'latest_version_id', 'source withdrawn');
select app.withdraw_knowledge_document(:'restricted_document_id', 'demo policy retired');
do $$
begin
  if app.knowledge_citation_is_valid(
    current_setting('test.latest_version_id')::uuid,
    'section:HackRF-One-r9', current_setting('test.latest_fragment_hash')
  ) then raise exception 'revoked latest citation remained valid'; end if;
  if app.knowledge_can_read(current_setting('test.restricted_document_id')::uuid) then
    raise exception 'withdrawn document remained readable';
  end if;
end;
$$;

reset role;
do $$
begin
  if has_function_privilege('public', 'app.publish_knowledge_version(uuid,text,text,text,jsonb)'::regprocedure, 'execute')
     or has_function_privilege('public', 'app.knowledge_citation_is_valid(uuid,text,text)'::regprocedure, 'execute') then
    raise exception 'knowledge function executable by public';
  end if;
end;
$$;

rollback;
