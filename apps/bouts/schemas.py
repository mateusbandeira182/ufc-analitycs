"""Schemas Pydantic de saída do app de bouts.

``BoutFighterStatsOut`` é o contrato das stats granulares de um lutador numa luta
(uma linha de ``bout_fighters``) -- reusado pelas Slices 04 (histórico) e 05
(head-to-head). Reflete o **model real do M0**: ``landed``/``attempted`` separados,
``knockdowns`` e ``submission_attempts``, todos ``int | None`` (o snippet
simplificado da SPEC foi preterido em favor do schema real -- ADR 0001). Nenhum
campo é média agregada: os números vêm por luta.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from apps.bouts.enums import BoutMethod, Corner


class BoutFighterStatsOut(BaseModel):
    """Stats granulares de um lutador numa luta (uma linha de ``bout_fighters``)."""

    model_config = ConfigDict(from_attributes=True)

    fighter_id: int
    corner: Corner
    knockdowns: int | None
    sig_strikes_landed: int | None
    sig_strikes_attempted: int | None
    takedowns_landed: int | None
    takedowns_attempted: int | None
    submission_attempts: int | None
    control_time_seconds: int | None
    source: str


class BoutEventOut(BaseModel):
    """Evento resumido exposto no detalhe da luta."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    date: date
    location: str | None
    source: str


class BoutDetailOut(BaseModel):
    """Detalhe de uma luta: evento, resultado e os dois cantos com stats granulares."""

    id: int
    event: BoutEventOut
    winner_id: int | None
    method: BoutMethod
    round: int | None
    ending_time_seconds: int | None
    weight_class: str | None
    source: str
    fighters: list[BoutFighterStatsOut]


class HeadToHeadOut(BaseModel):
    """Confrontos diretos entre dois lutadores.

    Envelope com os dois ids consultados e a lista de bouts em que ambos
    participaram, em ordem cronológica. Cada item reusa ``BoutDetailOut`` (Slice
    03): resultado + stats granulares dos dois cantos, nunca médias agregadas.
    """

    fighter_a_id: int
    fighter_b_id: int
    bouts: list[BoutDetailOut]
