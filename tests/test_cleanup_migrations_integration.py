"""Approved legacy retirement is transactional and preserves product data."""
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import make_conninfo
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from alembic import command


@pytest.mark.integration
def test_cleanup_refuses_nonempty_legacy_and_preserves_product():
    import os

    load_dotenv('.env')
    source = os.environ['SUPPLYAGENT_PRODUCT_DATABASE_URL']
    name = 'cleanup_test_' + uuid4().hex
    url = make_conninfo(source, dbname=name)
    with psycopg.connect(make_conninfo(source, dbname='postgres'), autocommit=True) as admin:
        admin.execute(sql.SQL('create database {}').format(sql.Identifier(name)))
        try:
            engine_url = make_url(source).set(database=name, drivername='postgresql+psycopg')
            engine = create_engine(engine_url, hide_parameters=True)
            cfg = Config('alembic.ini')
            try:
                with engine.connect() as connection:
                    cfg.attributes['connection'] = connection
                    command.upgrade(cfg, '0008')
                    connection.commit()
                with psycopg.connect(url) as db:
                    db.execute("insert into public.hw_part(project_id,commit_sha,mpn,\"values\",footprints,designators,quantity) values ('test','fixed','P', '{}','{}','{U1}',1)")
                    db.execute("insert into public.env_snapshot(run_id,day,state,state_hash) values ('guard',1,'{}','test')")
                    db.execute('create schema app')
                    db.execute('create table app.retained_fact(id int primary key)')
                    db.execute('insert into app.retained_fact values (7)')
                    db.execute('create extension if not exists pgcrypto with schema public')
                    for signature in ('propose_long_term_memory(jsonb,uuid,uuid,uuid)', 'propose_long_term_memory(jsonb,jsonb,uuid,uuid,uuid)', 'begin_runtime_run(text)'):
                        db.execute('create function app.' + signature + " returns int language sql as 'select 1'")
                with pytest.raises(RuntimeError, match='nonempty legacy table: env_snapshot'), engine.connect() as connection:
                    cfg.attributes['connection'] = connection
                    command.upgrade(cfg, 'head')
                with psycopg.connect(url) as db:
                    assert db.execute('select version_num from alembic_version').fetchone()[0] == '0008'
                    assert db.execute("select to_regclass('public.artifact')").fetchone()[0]
                    db.execute('truncate public.env_snapshot')
                with engine.connect() as connection:
                    cfg.attributes['connection'] = connection
                    command.upgrade(cfg, 'head')
                    connection.commit()
                with psycopg.connect(url) as db:
                    cleanup = Path('supabase/migrations/0029_retire_obsolete_entrypoints.sql').read_text()
                    db.execute(cleanup)
                    db.execute(cleanup)
                    assert db.execute('select version_num from alembic_version').fetchone()[0] == '0009'
                    assert db.execute('select count(*) from public.hw_part').fetchone()[0] == 1
                    assert db.execute('select id from app.retained_fact').fetchone()[0] == 7
                    assert db.execute("select to_regclass('public.artifact')").fetchone()[0] is None
                    assert db.execute("select to_regprocedure('app.begin_runtime_run(text)')").fetchone()[0] is None
                    assert db.execute("select to_regprocedure('app.propose_long_term_memory(jsonb,uuid,uuid,uuid)')").fetchone()[0] is None
                    assert db.execute("select to_regprocedure('app.propose_long_term_memory(jsonb,jsonb,uuid,uuid,uuid)')").fetchone()[0]
                    assert db.execute("select public.digest('retained','sha256')").fetchone()[0]
            finally:
                engine.dispose()
        finally:
            admin.execute(sql.SQL('drop database {} with (force)').format(sql.Identifier(name)))
