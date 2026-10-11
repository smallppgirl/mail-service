import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from mail_service.config import settings


def now():
    return datetime.now(timezone.utc)


def aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@lru_cache
def engine():
    return create_engine(settings().database_url, pool_pre_ping=True)


def session():
    return sessionmaker(engine(), expire_on_commit=False)()


class MailboxBusy(Exception):
    pass


@contextmanager
def mailbox_lock(mailbox_id):
    """All Graph jobs for one mailbox are serialized, even across worker processes.

    Lock connection is separate from application transactions. Before each Graph
    operation the client checks this connection, avoiding continued work after a
    detected lost lock. External requests already in flight remain ambiguous.
    """
    if engine().dialect.name != "postgresql":
        # SQLite is only used by offline tests; production requires PostgreSQL.
        yield lambda: None
        return
    key = int.from_bytes(hashlib.sha256(mailbox_id.encode()).digest()[:8], "big", signed=True)
    with engine().connect() as conn:
        if conn.dialect.name == "postgresql":
            if not conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar():
                raise MailboxBusy()
            backend_pid = conn.execute(text("SELECT pg_backend_pid()")).scalar()
            conn.commit()
        try:

            def guard():
                if conn.dialect.name == "postgresql":
                    if conn.execute(text("SELECT pg_backend_pid()")).scalar() != backend_pid:
                        raise RuntimeError("Mailbox lock connection was replaced")
                else:
                    conn.execute(text("SELECT 1"))
                conn.commit()

            yield guard
        finally:
            if conn.dialect.name == "postgresql" and not conn.invalidated:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                conn.commit()
