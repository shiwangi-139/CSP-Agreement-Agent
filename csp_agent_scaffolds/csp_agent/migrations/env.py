"""Alembic environment. The database URL comes from app.config (which reads
.env), never from alembic.ini, so no connection string is stored here."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import DATABASE_URL
from app.db import Base
import app.models  # noqa: F401  (registers every table on Base.metadata)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ConfigParser treats "%" as interpolation, so escape it in passwords.
config.set_main_option("sqlalchemy.url", (DATABASE_URL or "").replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
