"""Factories compartilhadas de ``Event``, ``Bout`` e ``BoutFighter`` para os testes.

Fonte única reusável entre os apps (``fighters``, ``events`` e ``bouts`` importam
daqui) -- elimina a duplicação que antes existia em ``apps/events/tests/factories.py``.

Produzem objetos válidos por padrão (``source="kaggle"``); o teste sobrescreve
só os campos sob prova (FKs reais, canto e stats granulares) e persiste com
``session.add``. As factories são agnósticas de sessão para serem reusáveis.

``__set_primary_key__ = False``: o ``id`` é deixado a cargo da sequence do
Postgres (autoincrement do PK), não gerado aleatoriamente pelo polyfactory. Isso
evita colisões de PK não-determinísticas (``UniqueViolation``) quando um teste
semeia várias linhas -- o banco garante ids únicos e determinísticos por transação.
"""

from __future__ import annotations

from polyfactory.factories.sqlalchemy_factory import SQLAlchemyFactory

from apps.bouts.enums import BoutMethod
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event


class EventFactory(SQLAlchemyFactory[Event]):
    """Constrói ``Event`` válido; passe ``name``/``date`` no teste se relevante."""

    __model__ = Event
    __set_relationships__ = False
    __set_primary_key__ = False

    @classmethod
    def source(cls) -> str:
        return "kaggle"


class BoutFactory(SQLAlchemyFactory[Bout]):
    """Constrói ``Bout`` válido; passe ``event_id`` do event já persistido no teste.

    ``winner_id`` fica nulo por padrão (empate/no contest ou vencedor não semeado),
    evitando a FK de ``fighters`` quando o teste só se importa com o card; passe o
    id real quando o vencedor importar. ``method`` assume decisão por padrão.
    """

    __model__ = Bout
    __set_relationships__ = False
    __set_primary_key__ = False

    @classmethod
    def winner_id(cls) -> None:
        return None

    @classmethod
    def method(cls) -> BoutMethod:
        return BoutMethod.DECISION

    @classmethod
    def source(cls) -> str:
        return "kaggle"


class BoutFighterFactory(SQLAlchemyFactory[BoutFighter]):
    """Constrói ``BoutFighter`` válido; passe ``bout_id``/``fighter_id``/``corner``."""

    __model__ = BoutFighter
    __set_relationships__ = False
    __set_primary_key__ = False

    @classmethod
    def source(cls) -> str:
        return "kaggle"


class BoutFighterRoundFactory(SQLAlchemyFactory[BoutFighterRound]):
    """Constrói ``BoutFighterRound`` válido; passe ``bout_fighter_id``/``round``.

    Origem ``cito`` por padrão (o round-a-round é populado pelo backfill da Cito no
    M5), refletindo a fonte real desse dado granular por round.
    """

    __model__ = BoutFighterRound
    __set_relationships__ = False
    __set_primary_key__ = False

    @classmethod
    def source(cls) -> str:
        return "cito"
