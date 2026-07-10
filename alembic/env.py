"""Ambiente do Alembic da MMA Analytics Platform.

A URL de conexão vem de ``mma_analytics.settings`` (respeita ``APP_ENV``), não do
``alembic.ini``. Os models dos apps são importados para registrar todas as tabelas
em ``Base.metadata``, que é o ``target_metadata`` do autogenerate.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

# Importa os models para registrar as tabelas em Base.metadata.
import apps.bouts.models
import apps.events.models
import apps.fighters.models  # noqa: F401
from alembic import context
from mma_analytics.db import Base
from mma_analytics.settings import settings

config = context.config

# Injeta a URL efetiva (por ambiente) no lugar do placeholder do alembic.ini.
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Executa as migrations em modo offline (só com a URL, sem engine)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Executa as migrations em modo online (com engine e conexão)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
