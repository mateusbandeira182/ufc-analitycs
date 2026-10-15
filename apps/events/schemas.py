"""Schemas Pydantic de saída do app de events.

``EventOut`` é o contrato de leitura reusado em list e detail (identidade do event
+ ``source``, RF-09). ``BoutCardOut`` é o resumo de uma luta no card do event --
inclui a dupla de participantes (``BoutCardFighterOut``: id, nome, canto e retrato)
para a SPA renderizar o confronto, mas **sem** as stats granulares de
``bout_fighters`` (box-score), que ficam no detalhe da luta. ``EventDetailOut``
estende ``EventOut`` com o card, a contagem por método (``EventSummaryOut``) e os
destaques calculados (``EventHighlightsOut``).

``summary`` e ``highlights`` são **anuláveis de propósito** (M9, Slice 07): ``None``
declara que o evento não tem luta realizada classificável. Zero finalização é uma
afirmação sobre o evento; ausência de resultado não é zero, e um card inteiro
agendado contaria 0 KO, 0 finalização e 0 decisão -- zeros que se leem como medição.

Nenhum destaque é premiação oficial: o dado não existe em nenhuma das duas fontes
(``awards`` da Cito é sempre nulo e a fonte oficial não publica), então a tela os
rotula "calculado, não oficial".
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from apps.bouts.enums import BoutMethod, BoutStatus, Corner
from apps.predictions.schemas import MatchupFighterOut


class EventOut(BaseModel):
    """Representação pública de um event."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    date: date  # data de calendário do event, não instante -- sem timezone
    location: str | None
    source: str


class BoutCardFighterOut(BaseModel):
    """Um participante da luta no card do event: identidade, canto e retrato.

    Só identidade (id, nome, canto, retrato) -- o box-score granular vive no
    detalhe da luta. A SPA cruza ``fighter_id`` com ``BoutCardOut.winner_id`` para
    destacar o vencedor.
    """

    model_config = ConfigDict(from_attributes=True)

    fighter_id: int
    name: str
    corner: Corner
    # O ``Fighter`` já vem eager-loaded no selector, então o retrato custa zero query; sem
    # ele a Home teria de buscar lutador por luta -- o N+1 que o ADR 0003 eliminou.
    # Ausência é o caso comum (o retrato vem da Cito e cobre só parte do acervo).
    headshot_url: str | None


class BoutCardOut(BaseModel):
    """Resumo de uma luta no card do event (sem stats granulares por canto)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    winner_id: int | None
    # Nulo na luta agendada: ela ainda não tem desfecho (M9, RF-07).
    method: BoutMethod | None
    round: int | None
    ending_time_seconds: int | None
    weight_class: str | None
    source: str
    # Luta realizada x agendada: é o ``status`` que decide o modo da página do evento
    # (RF-31 / D-14), nunca a data -- há evento de data passada com o card inteiro
    # ``scheduled``, porque o ``gap_sync`` é manual.
    status: BoutStatus
    # Rótulo **cru** da Cito ('Main Card' / 'Prelims'), vindo do payload e **nunca**
    # derivado de ``bout_order`` (RF-27): a fórmula (``1000 x seção + posição``) é dela e
    # pode mudar. Nulo é o caso comum -- o contexto de card só cobre evento recente.
    card_section: str | None
    # Ordenável, **não** sequencial nem denso: o payload real traz 1001 na luta principal,
    # que é o **menor** valor. Só a ordem relativa dentro do evento é utilizável.
    bout_order: int | None
    fighters: list[BoutCardFighterOut]  # a dupla de participantes (um item por canto)


class EventSummaryOut(BaseModel):
    """Contagem por método das lutas **realizadas** do card; ``n_bouts`` é o denominador.

    A interface mostra ``n_bouts`` junto da contagem para que um card misto nunca se leia
    como o card inteiro.

    Os seis baldes somam ``n_bouts``. O balde ``dq`` é obrigatório: ``BoutMethod`` tem cinco
    membros, e sem ele as lutas por desqualificação sairiam do resumo com a soma ficando
    menor que o total **sem erro nenhum**. ``draws`` não vem de ``method`` -- ``BoutMethod``
    não tem ``DRAW`` --, e sim de ``winner_id`` nulo.
    """

    model_config = ConfigDict(from_attributes=True)

    n_bouts: int
    ko_tko: int
    submissions: int
    decisions: int
    draws: int
    no_contests: int
    dq: int


class FinishHighlightOut(BaseModel):
    """A finalização mais rápida do card.

    ``ending_time_seconds`` é o tempo **dentro** do round, não o decorrido total, e não há
    campo de total: convertê-lo exigiria assumir 300s por round, e 48 lutas de 1994-1999
    passam disso no round 1. Por isso o par ``(round, ending_time_seconds)`` viaja junto.
    """

    bout_id: int
    fighter: MatchupFighterOut
    method: BoutMethod
    round: int
    ending_time_seconds: int


class BoutHighlightOut(BaseModel):
    """Destaque de volume (golpes significativos conectados ou quedas) por lutador-por-luta.

    ``value: 0`` é medição, não ausência: ausência de base faz o destaque inteiro ser nulo.
    """

    bout_id: int
    fighter: MatchupFighterOut
    value: int


class UpsetHighlightOut(BaseModel):
    """A maior zebra do card, lida do registro persistido em ``bout_predictions`` (D-01).

    ``prob_of_winner`` é a probabilidade que o modelo **da época** deu a quem venceu.
    ``n_bouts_with_prediction`` de ``n_bouts`` é o denominador declarado, para a tela poder
    dizer "entre as 9 de 13 lutas previstas".
    """

    bout_id: int
    winner: MatchupFighterOut
    prob_of_winner: float
    n_bouts_with_prediction: int
    n_bouts: int


class EventHighlightsOut(BaseModel):
    """Os quatro destaques **calculados** do granular; cada um nulo quando não há base."""

    fastest_finish: FinishHighlightOut | None
    most_sig_strikes: BoutHighlightOut | None
    most_takedowns: BoutHighlightOut | None
    biggest_upset: UpsetHighlightOut | None


class EventDetailOut(EventOut):
    """Detalhe do event com o card, a contagem por método e os destaques calculados."""

    bouts: list[BoutCardOut]
    summary: EventSummaryOut | None  # None = sem luta realizada classificável, nunca zeros
    highlights: EventHighlightsOut | None  # None pelo mesmo critério de ``summary``
