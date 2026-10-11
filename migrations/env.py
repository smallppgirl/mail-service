from alembic import context
from sqlalchemy import create_engine, pool
from mail_service.config import settings
from mail_service.models import Base

if context.is_offline_mode():
    context.configure(
        url=settings().database_url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    provided = context.config.attributes.get("connection")
    if provided is not None:
        context.configure(connection=provided, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(settings().database_url, poolclass=pool.NullPool)
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata)
            with context.begin_transaction():
                context.run_migrations()
