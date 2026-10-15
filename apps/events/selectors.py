"""Leitura de events (sem efeito colateral).

Recebe a ``Session`` por injeção e devolve models do domínio -- ou dataclasses de
resumo, quando o que a tela precisa é um agregado calculado on demand.

``list_events`` ordena por data decrescente (mais recentes primeiro), com desempate
por ``id`` decrescente para paginação determinística no Postgres, e aceita a partição
``when=past|upcoming`` (M9, Slice 07), derivada do ``status`` da **luta**.
``get_latest_completed_event`` devolve o último evento com desfecho a mostrar.

``list_event_bouts`` faz a leitura cross-app de ``Bout`` -- na ordem do card
(``bout_order``, nulos por último) e **sem** filtrar ``status`` -- e carrega, via
``selectinload``, os cantos de cada luta com a identidade do lutador
(``Bout.bout_fighters`` -> ``BoutFighter.fighter``) para o card exibir os participantes
-- sem o box-score granular, que fica no detalhe da luta, e sem N+1.

``summarize_event_bouts`` e ``collect_event_highlights`` consomem essas mesmas lutas já
carregadas e devolvem ``None`` quando o evento não tem luta realizada classificável:
ausência declarada, nunca zero. Nenhum deles pré-agrega nada -- tudo é calculado a cada
leitura, sobre o granular (ADR 0001).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, assert_never

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from apps.bouts.enums import BoutMethod, BoutStatus
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from apps.predictions.models import BoutPrediction

logger = logging.getLogger(__name__)

EventWhen = Literal["past", "upcoming"]


def _upcoming_predicate() -> ColumnElement[bool]:
    """Evento por vir: tem luta agendada, ou não tem luta nenhuma e a data não passou.

    O segundo ramo existe pelo ``UFC 335`` (no banco **sem nenhuma luta**): sem ele, um
    evento futuro de card não publicado cairia em "Realizados".

    Deriva do ``status`` da **luta**, não da data do evento (DT-1 da Slice 07, que emenda a
    D-06). Medido no banco em 2026-10-15: seis eventos têm data passada e zero luta
    realizada, porque o ``gap_sync`` é manual -- pela partição por data eles seriam
    "Realizados" com o resumo contando 0 KO, 0 finalização e 0 decisão, zeros que se leem
    como medição. A D-06 rejeitava uma coluna de status **no evento**, que envelheceria sem
    ninguém escrevê-la; ``bouts.status`` não tem esse defeito, porque quem o escreve é a
    própria ingestão do resultado -- a partição se auto-corrige quando o ``gap_sync`` roda.

    Usa ``func.current_date()`` (o relógio do Postgres), nunca ``date.today()`` em Python:
    dois relógios na mesma comparação é defeito silencioso.
    """
    tem_agendada = (
        select(Bout.id)
        .where(Bout.event_id == Event.id, Bout.status == BoutStatus.SCHEDULED)
        .exists()
    )
    tem_alguma = select(Bout.id).where(Bout.event_id == Event.id).exists()
    return or_(tem_agendada, and_(~tem_alguma, Event.date >= func.current_date()))


def list_events(
    session: Session,
    *,
    when: EventWhen | None = None,
    limit: int,
    offset: int,
) -> tuple[list[Event], int]:
    """Devolve ``(linhas, total)`` de events, opcionalmente particionados por ``when``.

    ``when=None`` lista o acervo por data desc (mais recentes primeiro), com desempate por
    ``id`` desc para manter a janela reprodutível quando há datas iguais.

    ``when="upcoming"`` aplica ``_upcoming_predicate()`` e ordena por data **crescente** --
    o próximo evento primeiro, que é o que a tela de "Próximos" pede. ``when="past"`` é a
    **negação** do mesmo predicado, escrito uma única vez: com dois predicados independentes
    bastaria um deles esquecer o ramo do evento sem lutas para que um evento caísse nos dois
    conjuntos (ou em nenhum), e nenhum teste de conjunto único acusaria. Assim a identidade
    ``total(past) + total(upcoming) == total`` vale **por construção**, e o teste a confirma
    em vez de a sustentar.

    ``total`` conta o **conjunto filtrado**, não o acervo: contar sem filtro faria a
    paginação de cada seção mentir enquanto os itens pareceriam certos.
    """
    stmt = select(Event)
    count_stmt = select(func.count()).select_from(Event)
    if when is not None:
        predicado = _upcoming_predicate() if when == "upcoming" else ~_upcoming_predicate()
        stmt = stmt.where(predicado)
        count_stmt = count_stmt.where(predicado)

    if when == "upcoming":
        stmt = stmt.order_by(Event.date.asc(), Event.id.asc())
    else:
        stmt = stmt.order_by(Event.date.desc(), Event.id.desc())

    total = session.scalar(count_stmt) or 0
    rows = session.scalars(stmt.limit(limit).offset(offset)).all()
    return list(rows), total


def get_event_by_id(session: Session, event_id: int) -> Event | None:
    """Devolve o event pelo id, ou ``None`` quando não existe."""
    return session.get(Event, event_id)


def get_latest_completed_event(session: Session) -> Event | None:
    """Devolve o último evento **realizado**, ou ``None`` quando o acervo não tem nenhum.

    Elegível é o evento com ``date < current_date`` **e** ao menos uma luta ``completed``.
    As duas condições são necessárias: a data sozinha deixaria passar os eventos de data
    passada cujas lutas seguem todas ``scheduled`` (o ``gap_sync`` é manual), e o "último
    evento" da Home se abriria sem nenhum desfecho a resumir.

    Ordem ``date desc, id desc`` com ``limit 1`` -- o desempate por ``id`` mantém a escolha
    reprodutível quando dois eventos caem na mesma data.
    """
    tem_realizada = (
        select(Bout.id)
        .where(Bout.event_id == Event.id, Bout.status == BoutStatus.COMPLETED)
        .exists()
    )
    stmt = (
        select(Event)
        .where(Event.date < func.current_date(), tem_realizada)
        .order_by(Event.date.desc(), Event.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def list_event_bouts(session: Session, event_id: int) -> list[Bout]:
    """Devolve os bouts do card do event, na ordem do card (``bout_order``).

    ``bout_order`` chegou no M6 e é **ordenável, mas não sequencial nem denso** -- o payload
    real da Cito traz 1001 na luta principal --, então só a ordem relativa dentro do evento
    é confiável. Os nulos vão para o fim (``nulls_last``): luta sem contexto de card não pode
    encabeçar o card, e o desempate por ``id`` mantém a lista reprodutível.

    **Não filtra ``status``** (M9, RF-07), e isso é decisão ativa: o card de um evento futuro
    é feito inteiro de lutas agendadas, e quem decide o que exibir é a tela. É o único
    selector de leitura do M9 que enxerga luta ainda não realizada.

    Carrega os cantos (``bout_fighters``) e a identidade do lutador de cada canto via
    ``selectinload``, para o card expor os participantes sem N+1 -- apenas identidade
    (id/nome/canto), o box-score granular fica no detalhe da luta.
    """
    stmt = (
        select(Bout)
        .where(Bout.event_id == event_id)
        .options(selectinload(Bout.bout_fighters).selectinload(BoutFighter.fighter))
        .order_by(Bout.bout_order.asc().nulls_last(), Bout.id.asc())
    )
    return list(session.scalars(stmt).all())


@dataclass(frozen=True)
class EventSummary:
    """Contagem por método das lutas **realizadas** do card.

    ``n_bouts`` é o denominador declarado -- lutas ``completed`` com ``method`` conhecido,
    as únicas classificáveis. A soma dos seis baldes é igual a ``n_bouts`` por construção, e
    há teste que o afirma: é ele que falha alto no dia em que ``BoutMethod`` ganhar um
    membro novo. Sem o balde ``dq``, as lutas por desqualificação sairiam do resumo e a soma
    ficaria menor que o total **sem erro nenhum**.

    ``draws`` não vem de ``method``: ``BoutMethod.DRAW`` não existe, e o empate mora na
    nulabilidade de ``winner_id``.
    """

    n_bouts: int
    ko_tko: int
    submissions: int
    decisions: int
    draws: int
    no_contests: int
    dq: int


def summarize_event_bouts(bouts: Sequence[Bout]) -> EventSummary | None:
    """Resume o card por método; ``None`` sem luta realizada classificável.

    ``None`` é **ausência declarada**, não zero: zero finalização é uma afirmação sobre o
    evento, e ausência de resultado não é zero. Um card inteiro agendado contaria 0 KO, 0
    finalização e 0 decisão -- zeros que se leem como medição.

    Recebe as lutas **já carregadas** por ``list_event_bouts`` (sem segunda query): um card
    tem ~15 lutas, e uma agregação SQL aqui só acrescentaria uma ida ao banco.

    Ordem de avaliação (mutuamente exclusiva, só sobre luta ``completed``):

    1. ``method is None`` -- fora de tudo e fora de ``n_bouts``, com aviso no log. É estado
       impossível por construção (o ``gap_sync`` grava desfecho e ``status`` juntos).
    2. ``no_contests`` -- ``method == NO_CONTEST``.
    3. ``draws`` -- ``winner_id is None`` (e o método não é no contest).
    4. ``ko_tko`` / ``submissions`` / ``decisions`` / ``dq`` -- pelo ``method``.

    O ``match`` sobre ``BoutMethod`` é exaustivo de propósito: é o que faz o ``mypy
    --strict`` apontar o balde faltante se o enum crescer.
    """
    ko_tko = submissions = decisions = draws = no_contests = dq = 0
    n_bouts = 0
    for bout in bouts:
        if bout.status is not BoutStatus.COMPLETED:
            continue
        if bout.method is None:
            logger.warning(
                "Luta %s do evento %s está completed sem method -- fora do resumo do card.",
                bout.id,
                bout.event_id,
            )
            continue
        n_bouts += 1
        if bout.method is BoutMethod.NO_CONTEST:
            no_contests += 1
            continue
        if bout.winner_id is None:
            draws += 1
            continue
        match bout.method:
            case BoutMethod.KO_TKO:
                ko_tko += 1
            case BoutMethod.SUBMISSION:
                submissions += 1
            case BoutMethod.DECISION:
                decisions += 1
            case BoutMethod.DQ:
                dq += 1
            case restante:
                assert_never(restante)

    if n_bouts == 0:
        return None
    return EventSummary(
        n_bouts=n_bouts,
        ko_tko=ko_tko,
        submissions=submissions,
        decisions=decisions,
        draws=draws,
        no_contests=no_contests,
        dq=dq,
    )


@dataclass(frozen=True)
class FinishHighlight:
    """A finalização mais rápida do card: o lutador, o método e **onde** ela parou.

    ``ending_time_seconds`` é o tempo **dentro** do round, não o decorrido total -- e não há
    conversão para total aqui. Converter exigiria assumir 300s por round, número plausível e
    ainda assim fabricado: rounds 2 a 5 têm máximo exatamente 300, mas 48 lutas de
    1994-1999 passam disso no round 1 (o ``UFC Ultimate Ultimate '95`` tem 1080s num round
    só, e está historicamente correto -- a era não tinha round de duração fixa).
    """

    bout_id: int
    fighter: Fighter
    method: BoutMethod
    round: int
    ending_time_seconds: int


@dataclass(frozen=True)
class BoutHighlight:
    """Um destaque de volume: o maior valor medido por **lutador-por-luta** do card.

    Sem ``round``: num destaque de volume o round não se explica (o valor é da luta
    inteira), e campo que não se explica é campo a mais.
    """

    bout_id: int
    fighter: Fighter
    value: int


@dataclass(frozen=True)
class UpsetHighlight:
    """A maior zebra do card, **lida** do registro persistido em ``bout_predictions``.

    ``prob_of_winner`` é ``1 - prob_predicted_winner``: a probabilidade que o modelo da
    época deu a quem efetivamente venceu.

    ``n_bouts_with_prediction`` é o tamanho do universo da comparação -- lutas realizadas
    com vencedor conhecido **e** predição registrada --, e ``n_bouts`` o mesmo denominador
    de ``EventSummary``. Os dois juntos é que permitem a tela dizer "entre as 9 de 13 lutas
    previstas" em vez de apresentar a zebra como se o card inteiro tivesse sido previsto.
    """

    bout_id: int
    winner: Fighter
    prob_of_winner: float
    n_bouts_with_prediction: int
    n_bouts: int


@dataclass(frozen=True)
class EventHighlights:
    """Os quatro destaques **calculados** do card; cada um nulo quando não há base.

    Nenhum é premiação oficial: o dado não existe em nenhuma das duas fontes (``awards`` da
    Cito é sempre nulo e a fonte oficial não publica). A tela rotula "calculado, não
    oficial" por isso.
    """

    fastest_finish: FinishHighlight | None
    most_sig_strikes: BoutHighlight | None
    most_takedowns: BoutHighlight | None
    biggest_upset: UpsetHighlight | None


_FINISH_METHODS = (BoutMethod.KO_TKO, BoutMethod.SUBMISSION)


def _winner_of(bout: Bout) -> Fighter | None:
    """Devolve o ``Fighter`` do canto vencedor da luta, ou ``None`` quando não há como saber.

    Avisa no log quando a luta tem ``winner_id`` mas nenhum canto corresponde: é linha
    corrompida, e deixá-la cair em silêncio apagaria a luta do destaque sem explicação.
    """
    if bout.winner_id is None:
        return None
    for bf in bout.bout_fighters:
        if bf.fighter_id == bout.winner_id:
            return bf.fighter
    logger.warning(
        "Luta %s tem winner_id %s que não é nenhum dos seus cantos -- fora dos destaques.",
        bout.id,
        bout.winner_id,
    )
    return None


def _fastest_finish(bouts: Sequence[Bout]) -> FinishHighlight | None:
    """Finalização mais rápida por ``(round, ending_time_seconds)``, nessa ordem.

    Ordenar só por ``ending_time_seconds`` escolheria uma finalização de R3 aos 0:30 (30s)
    em vez de uma de R1 aos 4:50 (290s) -- e erraria calado, porque o campo é tempo dentro
    do round. Só KO/TKO e finalização entram: decisão e no contest não são finalização.
    """
    candidatas: list[FinishHighlight] = []
    for bout in bouts:
        if bout.method not in _FINISH_METHODS or bout.round is None:
            continue
        if bout.ending_time_seconds is None:
            continue
        winner = _winner_of(bout)
        if winner is None:
            continue
        candidatas.append(
            FinishHighlight(
                bout_id=bout.id,
                fighter=winner,
                method=bout.method,
                round=bout.round,
                ending_time_seconds=bout.ending_time_seconds,
            )
        )
    if not candidatas:
        return None
    return min(candidatas, key=lambda c: (c.round, c.ending_time_seconds, c.bout_id))


def _top_volume(bouts: Sequence[Bout], atributo: str) -> BoutHighlight | None:
    """Maior valor medido de ``atributo`` entre as linhas lutador-por-luta do card.

    ``None`` só quando **todas** as linhas têm o valor nulo -- ausência de medição. Um
    máximo **zero** continua sendo destaque com ``value: 0``: uma luta que acabou em 11
    segundos com zero golpe significativo conectado é uma medição, e tratá-la como ausência
    é o defeito que a DT-2 existe para evitar.

    Desempate por ``bout_fighters.id`` ascendente (a relationship vem ordenada por canto,
    não por id), para a escolha ser reprodutível entre leituras.
    """
    melhor: BoutHighlight | None = None
    maior: int | None = None
    linhas = sorted(
        ((bout, bf) for bout in bouts for bf in bout.bout_fighters),
        key=lambda par: par[1].id,
    )
    for bout, bf in linhas:
        valor = getattr(bf, atributo)
        if valor is None:
            continue
        if maior is None or valor > maior:
            maior = valor
            melhor = BoutHighlight(bout_id=bout.id, fighter=bf.fighter, value=valor)
    return melhor


def _latest_predictions(session: Session, bout_ids: Sequence[int]) -> dict[int, BoutPrediction]:
    """Devolve, por luta, a predição registrada mais recente (desempate por ``id`` desc).

    ``bout_predictions`` tem chave natural ``(bout_id, model_version)``: a mesma luta
    acumula uma linha por versão de modelo. Uma única query ``WHERE bout_id IN (...)`` e a
    seleção por luta em Python -- sem N+1 e sem window function.
    """
    if not bout_ids:
        return {}
    stmt = (
        select(BoutPrediction)
        .where(BoutPrediction.bout_id.in_(bout_ids))
        .order_by(BoutPrediction.predicted_at.asc(), BoutPrediction.id.asc())
    )
    mais_recente: dict[int, BoutPrediction] = {}
    for prediction in session.scalars(stmt):
        mais_recente[prediction.bout_id] = prediction
    return mais_recente


def _biggest_upset(session: Session, bouts: Sequence[Bout], n_bouts: int) -> UpsetHighlight | None:
    """Maior zebra do card: a luta em que o modelo errou com a maior confiança.

    **Lida** do registro persistido, nunca recalculada com o modelo de hoje (D-01): o
    artefato atual descreveria um evento passado com um modelo que não existia na época.

    ``None`` quando ninguém tem predição ou quando o modelo acertou todas -- nunca valor
    sobre amostra vazia.
    """
    com_vencedor = {bout.id: bout for bout in bouts if bout.winner_id is not None}
    predicoes = _latest_predictions(session, list(com_vencedor))
    if not predicoes:
        return None

    candidatas: list[UpsetHighlight] = []
    for bout_id, prediction in predicoes.items():
        bout = com_vencedor[bout_id]
        if prediction.predicted_winner_id is None:
            continue
        if prediction.predicted_winner_id == bout.winner_id:
            continue
        winner = _winner_of(bout)
        if winner is None:
            continue
        candidatas.append(
            UpsetHighlight(
                bout_id=bout_id,
                winner=winner,
                prob_of_winner=1.0 - prediction.prob_predicted_winner,
                n_bouts_with_prediction=len(predicoes),
                n_bouts=n_bouts,
            )
        )
    if not candidatas:
        return None
    return min(candidatas, key=lambda c: (c.prob_of_winner, c.bout_id))


def collect_event_highlights(
    session: Session, *, bouts: Sequence[Bout], n_bouts: int
) -> EventHighlights | None:
    """Destaques **calculados** do granular do card; ``None`` sem luta realizada.

    Nenhum deles é premiação oficial -- o dado não existe em nenhuma das duas fontes
    (``awards`` da Cito é sempre nulo), então a tela os rotula "calculado, não oficial".

    O universo é o mesmo de ``summarize_event_bouts``: luta ``completed`` com ``method``
    conhecido. Luta agendada não entra em destaque nenhum, mesmo que tenha linha granular.
    ``None`` para o retorno inteiro é **ausência declarada**, não destaques zerados; e cada
    destaque dentro dele é ``None`` quando falta base (RF-13).

    ``n_bouts`` é o denominador declarado, recebido de ``EventSummary`` para que a zebra e a
    contagem por método falem do mesmo total.

    Recebe as lutas já carregadas por ``list_event_bouts`` (cantos e identidade do lutador
    eager-loaded); a única ida extra ao banco é a leitura de ``bout_predictions``.
    """
    realizadas = [
        bout for bout in bouts if bout.status is BoutStatus.COMPLETED and bout.method is not None
    ]
    if not realizadas:
        return None
    return EventHighlights(
        fastest_finish=_fastest_finish(realizadas),
        most_sig_strikes=_top_volume(realizadas, "sig_strikes_landed"),
        most_takedowns=_top_volume(realizadas, "takedowns_landed"),
        biggest_upset=_biggest_upset(session, realizadas, n_bouts),
    )
