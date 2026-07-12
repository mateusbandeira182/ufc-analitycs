"""Fixtures compartilhadas por toda a suíte (raiz do repositório).

Promovido nesta slice (SPEC ``api-rest-v1-leitura``, Slice 01): a fixture
transacional ``db_session`` -- antes em ``tests/conftest.py`` -- sobe para a raiz
porque agora tem três callsites (ingestão em ``tests/ingestion/``, Selector e API
em ``apps/**/tests/``), fora do subtree onde o conftest antigo era visível. Junto
vem a fixture ``client`` (``TestClient`` com a dependência ``get_session``
sobreposta pela sessão transacional), reusada por todos os testes de API da SPEC.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

# APP_ENV precisa estar definido ANTES de qualquer import de primeira-parte: ``apps.*``
# e ``mma_analytics.*`` carregam ``mma_analytics.settings``, cujo objeto ``settings`` é
# instanciado no import. ``setdefault`` preserva um APP_ENV vindo do ambiente.
os.environ.setdefault("APP_ENV", "test")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# Importa os models (só side-effect) para popular ``Base.metadata`` antes do ``create_all``.
from apps.bouts import models as _bouts_models  # noqa: F401
from apps.events import models as _events_models  # noqa: F401
from apps.features import models as _features_models  # noqa: F401
from apps.fighters import models as _fighters_models  # noqa: F401
from mma_analytics.app import create_app
from mma_analytics.db import Base, engine, get_session
from mma_analytics.settings import settings


def garante_ambiente_de_teste() -> None:
    """Confirma que a suíte roda contra o banco de teste, nunca o de desenvolvimento."""
    if settings.effective_db_name != "ufc_bum_test":
        raise RuntimeError(
            "Os testes devem rodar com APP_ENV=test (banco ufc_bum_test); "
            f"banco efetivo atual: {settings.effective_db_name!r}."
        )


@pytest.fixture
def db_session() -> Iterator[Session]:
    """``Session`` transacional isolada por teste contra o Postgres de teste.

    Abre uma conexão e uma transação externa, materializa o schema via
    ``create_all`` dentro dela e entrega uma ``Session`` ligada à mesma conexão.
    O ``rollback`` no teardown descarta dados e DDL -- o banco volta limpo entre
    testes, independente do estado deixado pelo teste de migration.
    """
    garante_ambiente_de_teste()
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


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """``TestClient`` da app FastAPI com ``get_session`` sobreposto.

    A dependência de sessão da API aponta para a mesma ``Session`` transacional do
    teste, garantindo que a asserção enxergue o estado semeado na fixture e que
    nada escape do rollback.
    """
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
