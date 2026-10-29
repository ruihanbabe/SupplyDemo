-- C3: keep compaction coverage separate from canonical Working State and F48 extraction.
begin;
alter table app.context_working_state add column if not exists compaction jsonb;
create or replace function app.context_compact(input_id uuid,input_job uuid,input_owner text,input_generation integer,
 input_epoch bigint,input_watermark bigint,input_state jsonb,input_summary jsonb) returns integer
language plpgsql security definer set search_path=app,pg_temp as $$
declare c app.context_conversation%rowtype; declare v integer; declare lo bigint; declare hi bigint; declare manifest jsonb;
begin
 c:=app.context_guard(input_id,input_job,input_owner,input_generation);
 lo:=(input_summary->>'start')::bigint; hi:=(input_summary->>'end')::bigint;
 if c.source_epoch<>input_epoch or c.committed_watermark<>input_watermark or c.state<>'open'
  or lo<2 or hi<lo or hi>=input_watermark or jsonb_typeof(input_summary->'text')<>'string'
  or octet_length(input_summary::text)>65536 then raise exception 'context_summary_invalid' using errcode='22023'; end if;
 if exists(select 1 from app.context_message a join app.context_message b on b.conversation_id=a.conversation_id and b.message_unit_id=a.message_unit_id
   where a.conversation_id=c.id and a.ordinal between lo and hi and b.ordinal not between lo and hi) then
  raise exception 'context_unpaired' using errcode='22023'; end if;
 select jsonb_agg(jsonb_build_object('ordinal',ordinal,'hash',content_hash) order by ordinal) into manifest
  from app.context_message where conversation_id=c.id and ordinal between lo and hi;
 if manifest is null or jsonb_array_length(manifest)<>hi-lo+1 then raise exception 'context_summary_invalid' using errcode='22023'; end if;
 select coalesce(max(version),0) into v from app.context_working_state where conversation_id=c.id;
 v:=app.context_checkpoint(c.id,input_job,input_owner,input_generation,v,input_watermark,input_state);
 update app.context_working_state set kind='compaction',compaction=input_summary||jsonb_build_object(
  'sourceEpoch',input_epoch,'sourceManifest',manifest,'coverageHash',encode(public.digest(convert_to(manifest::text,'UTF8'),'sha256'),'hex'))
  where conversation_id=c.id and version=v;
 return v;
end $$;
revoke all on function app.context_compact(uuid,uuid,text,integer,bigint,bigint,jsonb,jsonb) from public;
grant execute on function app.context_compact(uuid,uuid,text,integer,bigint,bigint,jsonb,jsonb) to supplyagent_api;
-- Future erasure cannot leave a readable projection/summary with the old body.
drop trigger if exists context_source_erased on app.context_message;
create trigger context_source_erased after update or delete on app.context_message
 for each row execute function app.context_bump_epoch();
commit;
