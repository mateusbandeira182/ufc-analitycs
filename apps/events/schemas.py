"""Schemas Pydantic de saída do app de events.

``EventOut`` é o contrato de leitura reusado em list e detail (identidade do event
+ ``source``, RF-09). ``BoutCardOut`` é o resumo de uma luta no card do event --
inclui a dupla de participantes (``BoutCardFighterOut``: id, nome e canto) para a
SPA renderizar o confronto, mas **sem** as stats granulares de ``bout_fighters``
(box-score), que ficam no detalhe da luta. ``EventDetailOut`` estende ``EventOut``
com o card.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from apps.bouts.enums import BoutMethod, Corner


class EventOut(BaseModel):
    """Representação pública de um event."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    date: date  # data de calendário do event, não instante -- sem timezone
    location: str | None
    source: str


class BoutCardFighterOut(BaseModel):
    """Um participante da luta no card do event: identidade e canto.

    Só identidade (id, nome, canto) -- o box-score granular vive no detalhe da
    luta. A SPA cruza ``fighter_id`` com ``BoutCardOut.winner_id`` para destacar o
    vencedor.
    """

    model_config = ConfigDict(from_attributes=True)

    fighter_id: int
    name: str
    corner: Corner


class BoutCardOut(BaseModel):
    """Resumo de uma luta no card do event (sem stats granulares por canto)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    winner_id: int | None
    method: BoutMethod
    round: int | None
    ending_time_seconds: int | None
    weight_class: str | None
    source: str
    fighters: list[BoutCardFighterOut]  # a dupla de participantes (um item por canto)


class EventDetailOut(EventOut):
    """Detalhe do event com os bouts do card."""

    bouts: list[BoutCardOut]
