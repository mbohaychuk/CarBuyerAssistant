from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from carbuyer.db import models  # noqa: F401  -- ensure models register with metadata
from carbuyer.db.base import Base
from carbuyer.shared.config import settings

# psycopg3's "postgresql+psycopg" URL is consumed by both create_engine (sync)
# and create_async_engine (async). Alembic only needs a sync engine, so we use
# create_engine directly with the same URL — no rewrite required.
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Prefer an explicit URL set on the alembic Config (used by test helpers
    # that target a separate database). Fall back to the app settings so that
    # the normal `alembic upgrade head` CLI flow is unchanged.
    url = config.get_main_option("sqlalchemy.url") or settings.database_url
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
