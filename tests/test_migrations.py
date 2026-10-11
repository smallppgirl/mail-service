import os
from uuid import uuid4
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql
from mail_service.models import Base


def test_frozen_migration_runs_on_empty_sqlite_database(tmp_path, monkeypatch):
    from mail_service.config import settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'migration.db'}")
    settings.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_engine(settings().database_url)
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    engine.dispose()
    settings.cache_clear()


def test_all_models_compile_for_postgresql():
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "CREATE TABLE" in ddl


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="Requires PostgreSQL; run Compose tests")
def test_postgres_migration_and_advisory_lock(monkeypatch):
    from mail_service.db import mailbox_lock, MailboxBusy
    from mail_service.models import Mailbox
    from mail_service.db import session

    url = os.environ["TEST_DATABASE_URL"]
    schema = "test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    # An isolated schema protects all existing application tables.
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    import mail_service.db as db_module

    monkeypatch.setattr(db_module, "engine", lambda: engine)
    try:
        with engine.begin() as conn:
            config = Config("alembic.ini")
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
        with session() as db:
            mailbox = Mailbox(graph_user_id="integration-test", address="test@example.com")
            db.add(mailbox)
            db.commit()
            with mailbox_lock(mailbox.id):
                with pytest.raises(MailboxBusy):
                    with mailbox_lock(mailbox.id):
                        pass
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
