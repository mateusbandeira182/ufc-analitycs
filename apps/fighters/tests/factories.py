"""Factory de ``Fighter`` para os testes (polyfactory ``SQLAlchemyFactory``).

Produz um lutador válido por padrão -- ``wins``/``losses``/``draws`` zerados,
``source="kaggle"`` e ``name_normalized`` derivado do ``name`` (chave de dedup
coerente, como o seed faria). O teste sobrescreve só o campo sob prova e persiste
o objeto com ``session.add``; a factory é agnóstica de sessão para ser reusável.

``__set_primary_key__ = False``: o ``id`` é deixado a cargo da sequence do
Postgres (autoincrement do PK), não gerado aleatoriamente pelo polyfactory. Isso
evita colisões de PK não-determinísticas (``UniqueViolation``) quando um teste
semeia vários lutadores na mesma transação -- o banco garante ids únicos.
"""

from __future__ import annotations

from polyfactory.decorators import post_generated
from polyfactory.factories.sqlalchemy_factory import SQLAlchemyFactory

from apps.fighters.models import Fighter
from ingestion.normalize import normalize_name


class FighterFactory(SQLAlchemyFactory[Fighter]):
    """Constrói ``Fighter`` válido; use ``build(...)`` e ``session.add`` no teste."""

    __model__ = Fighter
    __set_relationships__ = False
    __set_primary_key__ = False

    @classmethod
    def wins(cls) -> int:
        return 0

    @classmethod
    def losses(cls) -> int:
        return 0

    @classmethod
    def draws(cls) -> int:
        return 0

    @classmethod
    def source(cls) -> str:
        return "kaggle"

    @post_generated
    @classmethod
    def name_normalized(cls, name: str) -> str:
        """Deriva a chave de dedup do ``name`` gerado, mantendo-os coerentes."""
        return normalize_name(name)
