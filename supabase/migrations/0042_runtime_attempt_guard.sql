-- R1: lock a live attempt without granting direct UPDATE on runtime_job.
begin;
create or replace function app.lock_runtime_attempt(
  input_job uuid, input_trace uuid, input_owner text, input_generation integer
) returns boolean language plpgsql security definer set search_path=app,pg_temp as $$
declare j app.runtime_job%rowtype;
begin
  if not app.is_runtime_worker() then return false; end if;
  select x.* into j from app.runtime_job x join app.runtime_run r on r.id=x.run_id
    where x.id=input_job and x.organization_id=app.current_organization_id()
      and r.trace_run_id=input_trace for update of x;
  return j.id is not null and j.status='leased' and j.lease_owner is not distinct from input_owner
    and j.attempts=input_generation and j.lease_expires_at>clock_timestamp();
end $$;
revoke all on function app.lock_runtime_attempt(uuid,uuid,text,integer) from public;
grant execute on function app.lock_runtime_attempt(uuid,uuid,text,integer) to supplyagent_api;
commit;
