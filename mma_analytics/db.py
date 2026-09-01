"""Base declarativa, engine e sessão síncronos do SQLAlchemy.

A sessão é síncrona por decisão de arquitetura (casa com Alembic e com o Pandas
da camada de análise). ``Base.metadata`` é a fonte da verdade do schema,
consumida pela migration inicial do Alembic.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from mma_analytics.settings import settings


class Base(DeclarativeBase):
    """Base declarativa comum a todos os models do domínio."""


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """Dependência FastAPI: sessão síncrona por request, fechada no teardown.

    A sessão **não commita**: a API v1 nasceu somente-leitura e a dependência
    apenas abre e fecha. A exceção é o endpoint de predição de card
    (``apps.predictions.api``), primeiro a escrever -- lá o commit é explícito no
    service (``apps.predictions.services.predict_event_card``), porque sem ele a
    gravação se perderia no teardown. Nos testes, esta dependência é sobreposta
    (``dependency_overrides``) pela sessão transacional da fixture.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
