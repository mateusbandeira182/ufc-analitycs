"""Fixtures dos testes de infraestrutura de dados e de ingestão.

Provemos o ``Config`` do Alembic e um engine dedicado ao teste de round-trip da
migration (que aplica DDL upgrade/downgrade e não convive com rollback por
transação), e a fixture transacional ``db_session`` -- introduzida nesta slice 02,
no primeiro callsite real -- que dá a cada teste de ingestão uma ``Session`` sobre
o Postgres de teste ``ufc_bum_test`` com rollback ao final (isolamento sem TRUNCATE).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from alembic import command

# APP_ENV precisa estar definido ANTES dos imports de primeira-parte: tanto ``apps.*``
# quanto ``mma_analytics.*`` carregam ``mma_analytics.settings``, cujo objeto ``settings``
# é instanciado no import. Definir APP_ENV só depois (ou dentro de uma fixture) não muda
# ``effective_db_name`` -- a proteção ``_garante_ambiente_de_teste`` deixaria de valer e a
# suíte não seria auto-contida. ``setdefault`` preserva um APP_ENV vindo do ambiente.
os.environ.setdefault("APP_ENV", "test")

# Importa os models (só side-effect) para popular ``Base.metadata`` antes do ``create_all``.
from apps.bouts import models as _bouts_models  # noqa: F401
from apps.events import models as _events_models  # noqa: F401
from apps.fighters import models as _fighters_models  # noqa: F401
from mma_analytics.db import Base, engine
from mma_analytics.settings import settings

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _garante_ambiente_de_teste() -> None:
    """Confirma que a suíte roda contra o banco de teste, nunca o de desenvolvimento."""
    if settings.effective_db_name != "ufc_bum_test":
        raise RuntimeError(
            "Os testes devem rodar com APP_ENV=test (banco ufc_bum_test); "
            f"banco efetivo atual: {settings.effective_db_name!r}."
        )


@pytest.fixture
def alembic_cfg() -> Config:
    """``Config`` do Alembic apontando para o alembic.ini do repositório."""
    _garante_ambiente_de_teste()
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


@pytest.fixture
def db_session() -> Iterator[Session]:
    """``Session`` transacional isolada por teste contra o Postgres de teste.

    Abre uma conexão e uma transação externa, materializa o schema via
    ``create_all`` dentro dela e entrega uma ``Session`` ligada à mesma conexão.
    O ``rollback`` no teardown descarta dados e DDL -- o banco volta limpo entre
    testes, independente do estado deixado pelo teste de migration.
    """
    _garante_ambiente_de_teste()
    connection = engine.connect()
    transaction = connection.begin()
    Base.metadata.create_all(bind=connection, checkfirst=True)
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
