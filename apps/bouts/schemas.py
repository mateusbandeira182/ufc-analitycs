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
    name: str  # nome do lutador daquele canto (via ``BoutFighter.fighter``), para a SPA
    corner: Corner
    knockdowns: int | None
    sig_strikes_landed: int | None
    sig_strikes_attempted: int | None
    takedowns_landed: int | None
    takedowns_attempted: int | None
    submission_attempts: int | None
    control_time_seconds: int | None
    # Splits de golpe do M5 (ADR 0004): contagens granulares POR LUTA, todas
    # nullable (lutas antigas anteriores ao backfill não têm esse detalhe). Nunca
    # médias -- distribuição/acurácia derivam-se on demand na camada de consumo.
    total_strikes_landed: int | None
    total_strikes_attempted: int | None
    head_landed: int | None
    head_attempted: int | None
    body_landed: int | None
    body_attempted: int | None
    leg_landed: int | None
    leg_attempted: int | None
    distance_landed: int | None
    distance_attempted: int | None
    clinch_landed: int | None
    clinch_attempted: int | None
    ground_landed: int | None
    ground_attempted: int | None
    reversals: int | None
    source: str


class BoutFighterRoundOut(BaseModel):
    """Stats de um lutador num round (uma linha de ``bout_fighter_rounds``).

    ``fighter_id`` e ``corner`` identificam o canto dono do round (herdados do
    ``bout_fighter``, que é o dono do enum ``corner`` -- a tabela de rounds não o
    duplica). ``round`` é o número do round. Todas as stats são granulares por
    round e nullable (a Cito preenche o round-a-round só a partir de 2019).
    """

    model_config = ConfigDict(from_attributes=True)

    fighter_id: int
    corner: Corner
    round: int
    knockdowns: int | None
    sig_strikes_landed: int | None
    sig_strikes_attempted: int | None
    takedowns_landed: int | None
    takedowns_attempted: int | None
    submission_attempts: int | None
    control_time_seconds: int | None
    total_strikes_landed: int | None
    total_strikes_attempted: int | None
    head_landed: int | None
    head_attempted: int | None
    body_landed: int | None
    body_attempted: int | None
    leg_landed: int | None
    leg_attempted: int | None
    distance_landed: int | None
    distance_attempted: int | None
    clinch_landed: int | None
    clinch_attempted: int | None
    ground_landed: int | None
    ground_attempted: int | None
    reversals: int | None
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
    """Detalhe de uma luta: evento, resultado, contexto, cantos e round-a-round."""

    id: int
    event: BoutEventOut
    winner_id: int | None
    method: BoutMethod
    round: int | None
    ending_time_seconds: int | None
    weight_class: str | None
    # Contexto de luta do M5 (ADR 0004), todos nullable (backfill do CSV do seed).
    title_bout: bool | None
    scheduled_rounds: int | None
    referee: str | None
    source: str
    fighters: list[BoutFighterStatsOut]
    # Breakdown round-a-round por canto (vazio quando não há dado round-a-round).
    rounds: list[BoutFighterRoundOut]


class HeadToHeadOut(BaseModel):
    """Confrontos diretos entre dois lutadores.

    Envelope com os dois ids consultados e a lista de bouts em que ambos
    participaram, em ordem cronológica. Cada item reusa ``BoutDetailOut`` (Slice
    03): resultado + stats granulares dos dois cantos, nunca médias agregadas.
    """

    fighter_a_id: int
    fighter_b_id: int
    bouts: list[BoutDetailOut]
