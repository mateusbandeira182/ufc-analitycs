"""Base declarativa, engine e sessão síncronos do SQLAlchemy.

A sessão é síncrona por decisão de arquitetura (casa com Alembic e com o Pandas
da camada de análise). ``Base.metadata`` é a fonte da verdade do schema,
consumida pela migration inicial do Alembic.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from mma_analytics.settings import settings


class Base(DeclarativeBase):
    """Base declarativa comum a todos os models do domínio."""


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
