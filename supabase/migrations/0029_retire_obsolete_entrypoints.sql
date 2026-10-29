-- Approved cleanup only; does not apply pending F41/F45-F50 schemas.
begin;
drop function if exists app.propose_long_term_memory(jsonb,uuid,uuid,uuid) restrict;
drop function if exists app.begin_runtime_run(text) restrict;
commit;
