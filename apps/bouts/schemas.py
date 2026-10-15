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

from apps.bouts.enums import BoutMethod, BoutStatus, Corner
from apps.predictions.schemas import MatchupFighterOut


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
    # Nulo na luta agendada: ela ainda não tem desfecho (M9, RF-07).
    method: BoutMethod | None
    round: int | None
    ending_time_seconds: int | None
    weight_class: str | None
    # Realizada x agendada: é o ``status`` que decide o modo da página (RF-31 / D-14), nunca a
    # ausência de ``method`` -- uma luta realizada pode chegar sem método conhecido, e o proxy
    # a rotularia "agendada".
    status: BoutStatus
    # Rótulo **cru** da Cito ('Main Card' / 'Prelims'), vindo do payload e **nunca** derivado de
    # ``bout_order`` (RF-27). Nulo é o caso comum no acervo: ``fill_bout_context`` tem o
    # ``bout_order`` como porteiro, e o contexto só cobre evento recente.
    card_section: str | None
    # Contexto de luta do M5 (ADR 0004), todos nullable (backfill do CSV do seed).
    title_bout: bool | None
    scheduled_rounds: int | None
    referee: str | None
    source: str
    fighters: list[BoutFighterStatsOut]
    # Breakdown round-a-round por canto (vazio quando não há dado round-a-round).
    rounds: list[BoutFighterRoundOut]


class CommonOpponentBoutOut(BaseModel):
    """Uma luta contra o adversário em comum, pelo desfecho -- sem box-score.

    ``winner_id`` nulo é empate ou no contest (a convenção que ``apps.bouts.enums``
    documenta). Deliberadamente **não** reusa o ``won: bool`` de ``FighterBoutOut``:
    ali empate colapsa em ``False``, e nesta seção -- onde os dois lutadores são
    comparados contra o MESMO terceiro -- "perdeu para X" e "empatou com X" não
    podem virar a mesma afirmação.

    Sem stats granulares de propósito: a pergunta é "como cada um se saiu contra X",
    não o box-score. Quem quer o detalhe clica na luta (``GET /bouts/{id}``).
    """

    model_config = ConfigDict(from_attributes=True)

    bout_id: int
    event_id: int
    event_name: str
    event_date: date
    winner_id: int | None
    # Nulo na luta agendada -- que, aqui, nunca aparece: a seção filtra ``completed``.
    method: BoutMethod | None
    round: int | None


class CommonOpponentOut(BaseModel):
    """Um adversário que os dois lutadores enfrentaram, com o desfecho de cada lado."""

    opponent: MatchupFighterOut
    # O lutador pode ter enfrentado o mesmo adversário mais de uma vez: o adversário
    # não duplica (é a chave da entrada), as lutas somam.
    bouts_a: list[CommonOpponentBoutOut]
    bouts_b: list[CommonOpponentBoutOut]


class HeadToHeadOut(BaseModel):
    """Confrontos diretos entre dois lutadores, mais os adversários em comum.

    Envelope com os dois ids consultados e a lista de bouts em que ambos
    participaram, em ordem cronológica. Cada item reusa ``BoutDetailOut`` (Slice
    03): resultado + stats granulares dos dois cantos, nunca médias agregadas.

    ``common_opponents`` (M9, RF-16) é a seção que faz a página servir para a dupla
    que **nunca** se enfrentou -- o caso comum, e a razão de o humano dizer que "a
    página não faz nada": antes dela, o envelope vinha com ``bouts: []`` e nada mais.
    As duas seções convivem, e o próprio par nunca aparece em ``common_opponents``.
    """

    fighter_a_id: int
    fighter_b_id: int
    bouts: list[BoutDetailOut]
    common_opponents: list[CommonOpponentOut]
