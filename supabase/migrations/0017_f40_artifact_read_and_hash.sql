-- F40 Artifact 读取与内容指纹加固。
-- Artifact 仍由 trace_artifact 的 SELECT RLS 读取；不增加可绕过 RLS 的读取函数。
-- content_hash 是 Artifact 内容指纹，而不是调用方可声明的值：由本函数唯一计算。

revoke all on function app.append_trace_artifact(uuid, uuid, uuid, text, text, jsonb, text) from public;
drop function app.append_trace_artifact(uuid, uuid, uuid, text, text, jsonb, text);
drop function if exists app.append_trace_artifact(uuid, uuid, uuid, text, text, jsonb);

create function app.append_trace_artifact(
  input_run_id uuid,
  input_case_id uuid,
  input_artifact_key uuid,
  input_artifact_type text,
  input_redacted_summary text,
  input_redacted_payload jsonb
)
returns uuid language plpgsql security definer set search_path = app, pg_temp as $$
declare artifact_id uuid;
declare next_version integer;
declare current_run app.trace_run%rowtype;
declare calculated_content_hash text;
begin
  select * into current_run from app.trace_run
  where id = input_run_id and organization_id = app.current_organization_id()
  for key share;
  if not found then
    raise exception 'trace run is outside organization context' using errcode = '42501';
  end if;
  if input_case_id is distinct from current_run.case_id then
    raise exception 'case must match trace run' using errcode = '42501';
  end if;
  -- 版本是每个组织/Artifact 逻辑键的线性历史；首次写入没有可 FOR UPDATE 的父行，因此锁该逻辑键。
  perform pg_advisory_xact_lock(hashtextextended(
    app.current_organization_id()::text || ':' || input_artifact_key::text, 0
  ));
  select coalesce(max(version), 0) + 1 into next_version
  from app.trace_artifact
  where organization_id = app.current_organization_id() and artifact_key = input_artifact_key;
  -- jsonb::text 是 PostgreSQL 的规范 JSONB 文本形式。该指纹只覆盖 Artifact 的语义内容，
  -- 不覆盖 run/id/version 等定位字段，因此同内容的不同版本具有同一内容指纹。
  calculated_content_hash := encode(public.digest(convert_to(jsonb_build_object(
    'artifact_type', input_artifact_type,
    'redacted_summary', input_redacted_summary,
    'redacted_payload', coalesce(input_redacted_payload, '{}'::jsonb)
  )::text, 'UTF8'), 'sha256'), 'hex');
  insert into app.trace_artifact (
    organization_id, run_id, case_id, artifact_key, version, artifact_type,
    redacted_summary, redacted_payload, content_hash, actor_id
  ) values (
    app.current_organization_id(), input_run_id, input_case_id, input_artifact_key, next_version,
    input_artifact_type, input_redacted_summary, coalesce(input_redacted_payload, '{}'::jsonb),
    calculated_content_hash, app.current_actor_id()
  ) returning id into artifact_id;
  return artifact_id;
end;
$$;

revoke all on function app.append_trace_artifact(uuid, uuid, uuid, text, text, jsonb) from public;
grant execute on function app.append_trace_artifact(uuid, uuid, uuid, text, text, jsonb) to supplyagent_api;
