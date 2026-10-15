"""Leitura de bouts (sem efeito colateral).

``get_bout_by_id`` compõe a luta com o seu evento e os dois cantos de
``bout_fighters``, trazendo as stats granulares de cada lutador **como foram
gravadas** -- nunca médias agregadas (invariante do CLAUDE.md, ADR 0001). Os joins
principais são explícitos; a identidade do lutador de cada canto entra via
``selectinload(BoutFighter.fighter)`` (relationship só-leitura, sem migration),
para o schema expor o nome sem N+1. Os cantos saem em ordem determinística por
``corner`` para o response ser estável.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased, selectinload

from apps.bouts.enums import BoutMethod, BoutStatus, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.fighters.models import Fighter


@dataclass(frozen=True)
class BoutFighterRoundRow:
    """Uma linha round-a-round (``bout_fighter_rounds``) com o canto e o lutador.

    ``round`` é o model ``BoutFighterRound`` (stats do round). O ``fighter_id`` e o
    ``corner`` vêm do ``bout_fighter`` dono do round -- resolvidos em memória a
    partir dos cantos já carregados, sem join em ``bout_fighters`` na query (mantém
    o custo de leitura enxuto e não interfere na contagem de queries do card).
    """

    round: BoutFighterRound
    fighter_id: int
    corner: Corner


@dataclass(frozen=True)
class BoutDetail:
    """Composição de leitura de uma luta: a luta, o evento, os cantos e os rounds."""

    bout: Bout
    event: Event
    fighters: list[BoutFighter]  # ordem determinística por corner
    rounds: list[BoutFighterRoundRow]  # round-a-round por canto (vazio se ausente)


def _load_corners(session: Session, bout_id: int) -> list[BoutFighter]:
    """Carrega os dois cantos de uma luta em ordem determinística por ``corner``.

    Traz junto a identidade do lutador de cada canto (``BoutFighter.fighter``) via
    ``selectinload``, para o schema expor o nome sem N+1.
    """
    return list(
        session.scalars(
            select(BoutFighter)
            .where(BoutFighter.bout_id == bout_id)
            .options(selectinload(BoutFighter.fighter))
            .order_by(BoutFighter.corner)
        )
    )


def _load_corners_batch(session: Session, bout_ids: Sequence[int]) -> dict[int, list[BoutFighter]]:
    """Carrega os cantos de vários bouts em UMA query, agrupados por ``bout_id``.

    Evita o N+1 de consultar canto a canto: uma única ``SELECT ... WHERE bout_id
    IN (...)`` ordenada por ``bout_id`` e ``corner`` alimenta o agrupamento em
    memória, preservando a mesma ordem determinística por ``corner`` dentro de
    cada luta que ``_load_corners`` garante para o caso singular.
    """
    if not bout_ids:
        return {}
    corners_por_bout: dict[int, list[BoutFighter]] = defaultdict(list)
    corners = session.scalars(
        select(BoutFighter)
        .where(BoutFighter.bout_id.in_(bout_ids))
        .options(selectinload(BoutFighter.fighter))
        .order_by(BoutFighter.bout_id, BoutFighter.corner)
    )
    for bout_fighter in corners:
        corners_por_bout[bout_fighter.bout_id].append(bout_fighter)
    return corners_por_bout


def _rounds_for_corners(
    session: Session, corners: Sequence[BoutFighter]
) -> list[BoutFighterRoundRow]:
    """Carrega o round-a-round dos cantos dados, agrupado por canto e ordenado por round.

    A query filtra ``bout_fighter_rounds`` por ``bout_fighter_id IN (...)`` -- sem
    join em ``bout_fighters`` -- e o canto/lutador de cada round é resolvido em
    memória a partir dos cantos já carregados. A ordem de saída segue a ordem dos
    ``corners`` (já determinística por ``corner``), com os rounds de cada canto em
    ordem crescente. Cantos sem round-a-round (backfill parcial) simplesmente não
    contribuem linhas.
    """
    if not corners:
        return []
    rounds_por_canto: dict[int, list[BoutFighterRound]] = defaultdict(list)
    linhas = session.scalars(
        select(BoutFighterRound)
        .where(BoutFighterRound.bout_fighter_id.in_([bf.id for bf in corners]))
        .order_by(BoutFighterRound.bout_fighter_id, BoutFighterRound.round)
    )
    for linha in linhas:
        rounds_por_canto[linha.bout_fighter_id].append(linha)
    return [
        BoutFighterRoundRow(round=linha, fighter_id=bf.fighter_id, corner=bf.corner)
        for bf in corners
        for linha in rounds_por_canto.get(bf.id, [])
    ]


def get_bout_by_id(session: Session, bout_id: int) -> BoutDetail | None:
    """Devolve a composição da luta pelo id, ou ``None`` quando não existe."""
    bout = session.get(Bout, bout_id)
    if bout is None:
        return None
    # ``event_id`` é FK NOT NULL: o evento sempre existe. O guard mantém o tipo
    # de ``BoutDetail.event`` como ``Event`` (não ``Event | None``) sem assert.
    event = session.get(Event, bout.event_id)
    if event is None:
        return None
    corners = _load_corners(session, bout_id)
    return BoutDetail(
        bout=bout,
        event=event,
        fighters=corners,
        rounds=_rounds_for_corners(session, corners),
    )


def get_head_to_head(session: Session, a_id: int, b_id: int) -> list[BoutDetail]:
    """Devolve os confrontos diretos entre dois lutadores em ordem cronológica.

    Intersecta os bouts em que **ambos** os ``fighter_id`` aparecem em
    ``bout_fighters`` via dois aliases sobre o mesmo ``bout_id`` -- sem ``OR`` nos
    dois cantos, o ganho direto do schema long (ADR 0001). O join em ``events`` só
    ordena por ``date`` ascendente (desempate por ``bouts.id`` para lutas na mesma
    data). Cada bout carrega os dois cantos com as stats granulares como gravadas.
    Não valida existência dos lutadores (responsabilidade do router): dois
    lutadores sem confronto direto devolvem lista vazia. Os cantos de todos os
    bouts do resultado são carregados numa única query em lote (sem N+1).
    """
    bf_a = aliased(BoutFighter)
    bf_b = aliased(BoutFighter)
    stmt = (
        select(Bout, Event)
        .join(bf_a, bf_a.bout_id == Bout.id)
        .join(bf_b, bf_b.bout_id == Bout.id)
        .join(Event, Event.id == Bout.event_id)
        .where(
            bf_a.fighter_id == a_id,
            bf_b.fighter_id == b_id,
            Bout.status == BoutStatus.COMPLETED,
        )
        .order_by(Event.date.asc(), Bout.id.asc())
    )
    rows = session.execute(stmt).tuples().all()
    corners_por_bout = _load_corners_batch(session, [bout.id for bout, _ in rows])
    rounds_por_bout = _rounds_for_corners_batch(session, corners_por_bout)
    return [
        BoutDetail(
            bout=bout,
            event=event,
            fighters=corners_por_bout.get(bout.id, []),
            rounds=rounds_por_bout.get(bout.id, []),
        )
        for bout, event in rows
    ]


def _rounds_for_corners_batch(
    session: Session, corners_por_bout: dict[int, list[BoutFighter]]
) -> dict[int, list[BoutFighterRoundRow]]:
    """Carrega o round-a-round de vários bouts em UMA query, agrupado por ``bout_id``.

    Evita o N+1 de consultar round a round por luta: junta os ids de canto de todos
    os bouts numa única ``SELECT ... WHERE bout_fighter_id IN (...)`` e reagrupa em
    memória, reusando ``BoutFighterRoundRow`` (canto/lutador resolvidos a partir dos
    cantos já carregados). A query não referencia ``bout_fighters``, então não conta
    para o orçamento de queries de cantos do card.
    """
    todos_os_cantos = [bf for cantos in corners_por_bout.values() for bf in cantos]
    if not todos_os_cantos:
        return {}
    canto_por_id = {bf.id: bf for bf in todos_os_cantos}
    rounds_por_canto: dict[int, list[BoutFighterRound]] = defaultdict(list)
    linhas = session.scalars(
        select(BoutFighterRound)
        .where(BoutFighterRound.bout_fighter_id.in_(list(canto_por_id)))
        .order_by(BoutFighterRound.bout_fighter_id, BoutFighterRound.round)
    )
    for linha in linhas:
        rounds_por_canto[linha.bout_fighter_id].append(linha)
    resultado: dict[int, list[BoutFighterRoundRow]] = {}
    for bout_id, cantos in corners_por_bout.items():
        resultado[bout_id] = [
            BoutFighterRoundRow(round=linha, fighter_id=bf.fighter_id, corner=bf.corner)
            for bf in cantos
            for linha in rounds_por_canto.get(bf.id, [])
        ]
    return resultado


@dataclass(frozen=True)
class CommonOpponentBout:
    """Uma luta contra o adversário em comum, pelo desfecho -- sem box-score.

    ``winner_id`` nulo é empate ou no contest, a convenção que ``apps.bouts.enums`` documenta
    e que o resto da API já segue. Deliberadamente **não** reusa o ``won: bool`` de
    ``FighterBoutOut``: ali empate colapsa em ``False``, e nesta seção -- onde os dois
    lutadores são comparados contra o MESMO terceiro -- "perdeu para X" e "empatou com X" não
    podem virar a mesma afirmação.

    Sem stats granulares de propósito: a pergunta da seção é "como cada um se saiu contra X",
    não o box-score de cada luta. Quem quer o detalhe clica na luta (``GET /bouts/{id}``).
    """

    bout_id: int
    event_id: int
    event_name: str
    event_date: date
    winner_id: int | None
    method: BoutMethod | None
    round: int | None


@dataclass(frozen=True)
class CommonOpponent:
    """Um adversário que os dois lutadores enfrentaram, com o desfecho de cada lado."""

    opponent_id: int
    opponent_name: str
    # O lutador pode ter enfrentado o mesmo adversário mais de uma vez: o adversário não
    # duplica (é a chave da entrada), as lutas somam.
    bouts_a: list[CommonOpponentBout]
    bouts_b: list[CommonOpponentBout]


def _bouts_by_opponent(
    session: Session, fighter_id: int
) -> dict[int, list[tuple[CommonOpponentBout, str]]]:
    """Lutas ``completed`` do lutador agrupadas por adversário, cronológicas, em UMA query.

    Dois aliases sobre o mesmo ``bout_id`` (o ganho direto do schema long, ADR 0001): um fixa
    o lutador, o outro é o adversário -- sem ``OR`` nos dois cantos. O nome do adversário vem
    resolvido no join, então quem chama não faz N+1 para exibi-lo.
    """
    eu = aliased(BoutFighter)
    adversario = aliased(BoutFighter)
    stmt = (
        select(
            adversario.fighter_id,
            Fighter.name,
            Bout.id,
            Bout.event_id,
            Event.name,
            Event.date,
            Bout.winner_id,
            Bout.method,
            Bout.round,
        )
        .join(eu, eu.bout_id == Bout.id)
        .join(adversario, adversario.bout_id == Bout.id)
        .join(Event, Event.id == Bout.event_id)
        .join(Fighter, Fighter.id == adversario.fighter_id)
        .where(
            eu.fighter_id == fighter_id,
            adversario.fighter_id != fighter_id,
            Bout.status == BoutStatus.COMPLETED,
        )
        .order_by(Event.date.asc(), Bout.id.asc())
    )
    por_adversario: dict[int, list[tuple[CommonOpponentBout, str]]] = defaultdict(list)
    for linha in session.execute(stmt).tuples().all():
        (
            opponent_id,
            opponent_name,
            bout_id,
            event_id,
            event_name,
            event_date,
            winner_id,
            method,
            bout_round,
        ) = linha
        por_adversario[opponent_id].append(
            (
                CommonOpponentBout(
                    bout_id=bout_id,
                    event_id=event_id,
                    event_name=event_name,
                    event_date=event_date,
                    winner_id=winner_id,
                    method=method,
                    round=bout_round,
                ),
                opponent_name,
            )
        )
    return por_adversario


def get_common_opponents(session: Session, a_id: int, b_id: int) -> list[CommonOpponent]:
    """Adversários que os DOIS lutadores enfrentaram, com o desfecho de cada lado (RF-16).

    Duas queries (uma por lado), interseção em memória -- sem N+1. Hoje o head-to-head só
    devolve confronto direto, logo responde lista vazia para qualquer dupla que nunca se
    enfrentou: o caso comum, e a razão de a página "não fazer nada".

    Filtra ``status='completed'``: luta agendada não tem desfecho, e deixá-la entrar faria um
    adversário **futuro** aparecer como histórico -- com ``winner_id`` nulo, que a tela leria
    como empate ou derrota numa luta que não aconteceu. É o modo de falha que a varredura de
    ``bout_fighters`` desta SPEC existe para fechar; luta agendada chega à tela pelo card do
    evento, não por aqui.

    O próprio par é excluído do conjunto de adversários: sem isso, o confronto **direto** entre
    A e B voltaria aqui como "adversário em comum", duplicando a seção ao lado.

    Ordem determinística: entradas por nome do adversário ascendente, lutas de cada lado em
    ordem cronológica. Não valida existência dos lutadores (responsabilidade do router).
    """
    de_a = _bouts_by_opponent(session, a_id)
    de_b = _bouts_by_opponent(session, b_id)
    em_comum = (set(de_a) & set(de_b)) - {a_id, b_id}
    entradas = [
        CommonOpponent(
            opponent_id=opponent_id,
            # O nome vem resolvido no join dos dois lados; qualquer linha serve.
            opponent_name=de_a[opponent_id][0][1],
            bouts_a=[luta for luta, _ in de_a[opponent_id]],
            bouts_b=[luta for luta, _ in de_b[opponent_id]],
        )
        for opponent_id in em_comum
    ]
    return sorted(entradas, key=lambda entrada: (entrada.opponent_name, entrada.opponent_id))
