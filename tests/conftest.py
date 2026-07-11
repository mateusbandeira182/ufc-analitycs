"""Fixtures dos testes de migration do Alembic.

Provemos o ``Config`` do Alembic e um engine dedicado ao teste de round-trip da
migration (que aplica DDL upgrade/downgrade e não convive com rollback por
transação). A fixture transacional ``db_session`` e o guard de ambiente foram
promovidos para o ``conftest.py`` raiz (SPEC ``api-rest-v1-leitura``, Slice 01),
que passou a ter três callsites: ingestão, Selector e API.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine

from alembic import command
from conftest import garante_ambiente_de_teste
from mma_analytics.settings import settings

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def alembic_cfg() -> Config:
    """``Config`` do Alembic apontando para o alembic.ini do repositório."""
    garante_ambiente_de_teste()
    return Config(str(_REPO_ROOT / "alembic.ini"))


@pytest.fixture
def migration_engine(alembic_cfg: Config) -> Iterator[Engine]:
    """Engine dedicado ao teste de migration, com limpeza garantida no teardown."""
    migration_engine = create_engine(settings.database_url, future=True)
    try:
        yield migration_engine
    finally:
        # Garante que a suíte não deixe schema remanescente, mesmo em falha.
        command.downgrade(alembic_cfg, "base")
        migration_engine.dispose()
