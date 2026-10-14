-- F35：HTTP 只能通过此受限命令创建 Mission/Run；不能直接写 runtime 基础表。
begin;

create or replace function app.begin_runtime_run(input_mission_kind text)
returns table(mission_id uuid, run_id uuid)
language plpgsql security definer set search_path = app, pg_temp as $$
declare created_mission_id uuid;
declare created_run_id uuid;
begin
  -- F31 已在同一事务核验 actor 与组织；缺上下文时绝不创建匿名运行。
  if app.current_actor_id() is null or app.current_organization_id() is null then
    raise exception 'missing request context' using errcode = '42501';
  end if;
  if char_length(input_mission_kind) not between 1 and 96 then
    raise exception 'invalid mission kind' using errcode = '22023';
  end if;
  insert into app.runtime_mission (organization_id, kind, created_by)
  values (app.current_organization_id(), input_mission_kind, app.current_actor_id())
  returning id into created_mission_id;
  insert into app.runtime_run (organization_id, mission_id)
  values (app.current_organization_id(), created_mission_id)
  returning id into created_run_id;
  return query select created_mission_id, created_run_id;
end;
$$;

grant execute on function app.begin_runtime_run(text) to supplyagent_api;
commit;
