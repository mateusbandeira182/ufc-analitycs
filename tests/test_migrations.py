"""Teste de round-trip da migration inicial contra o Postgres de teste real.

``upgrade head`` deve criar as quatro tabelas; ``downgrade base`` deve reverter
sem resíduo -- nenhuma das tabelas e nenhum dos tipos enum (``stance``,
``bout_method``, ``corner``) pode permanecer (CA-01, CA-02).
"""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import Engine, inspect, text

from alembic import command

TABELAS = {"fighters", "events", "bouts", "bout_fighters"}
TIPOS_ENUM = {"stance", "bout_method", "corner"}


def _tabelas_existentes(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _tipos_enum_existentes(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        linhas = conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'")).scalars()
        return set(linhas)


def test_upgrade_cria_as_quatro_tabelas(alembic_cfg: Config, migration_engine: Engine) -> None:
    """``alembic upgrade head`` cria as quatro tabelas no Postgres de teste."""
    command.upgrade(alembic_cfg, "head")
    assert _tabelas_existentes(migration_engine) >= TABELAS


def test_downgrade_reverte_sem_residuo(alembic_cfg: Config, migration_engine: Engine) -> None:
    """``alembic downgrade base`` remove tabelas e tipos enum sem deixar resíduo."""
    command.upgrade(alembic_cfg, "head")
    assert _tipos_enum_existentes(migration_engine) >= TIPOS_ENUM

    command.downgrade(alembic_cfg, "base")
    assert not (TABELAS & _tabelas_existentes(migration_engine))
    assert not (TIPOS_ENUM & _tipos_enum_existentes(migration_engine))
