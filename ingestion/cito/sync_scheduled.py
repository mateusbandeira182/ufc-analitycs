"""Ingestão de eventos **agendados** do catálogo Cito -> base (M9, SPEC 011, Slice 04).

O catálogo com ``includeBouts=true`` traz o card de cada evento embutido, e é dele que sai o
card por vir: evento, luta sem desfecho (``bouts.status = 'scheduled'``) e os dois lutadores,
tudo com ``source="cito"``. É o insumo da tela de evento futuro -- sem isto, não há o que
prever.

Este módulo é o **terceiro** sentido do par evento/catálogo do projeto: a Sprint 007-03 tinha o
evento persistido como âncora e o catálogo dando o identificador; o ``gap_sync`` tem o catálogo
como âncora e ingere o que **já aconteceu**; aqui o catálogo também é a âncora, mas o que se
escreve é a luta **antes** de ela acontecer.

Invariantes
-----------
- **Nada de desfecho fabricado**: ``method``, ``winner_id``, ``round`` e
  ``ending_time_seconds`` ficam **nulos**, e ``status`` é ``SCHEDULED``. ``map_bout_core``
  **não** é usada -- ela degrada método nulo para ``NO_CONTEST``, conservador no enriquecimento
  e dado **falso** na criação (o método é o rótulo que o preditivo treina).
- **Exatamente um canto ``red`` e um ``blue``** (RF-32): a luta que não satisfaz isso é
  reportada e **não** é persistida pela metade. A decisão precede qualquer escrita.
- **O canto NÃO vem do campo ``corner``** do payload -- ver o comentário "POR QUE O CANTO NÃO
  VEM DO CAMPO `corner`" em ``ingestion.cito.gap_sync``, que vale igual para o agendado. A arte
  do card entrega o canto real quando tem o sufixo de lado; sem ele, o canto é atribuído pelo
  nome normalizado, e a procedência aparece em ``corner_origins``.
- **Sem corte por data**: o filtro é ``status == "scheduled"`` + ``is_ufc_catalog_item``. Um
  evento agendado de data já passada é exatamente o caso que a CA-29 manda a tela tratar.
- **Zero quota no desempate**: ambiguidade de entity resolution pula **a luta** daquele lutador
  e entra no relatório; nunca paga chamada de perfil (a execução é de teto 0) e nunca escolhe.
- **Gate humano antes da rede real** (``ingestion.cito.gate``).

A lógica testável opera sobre a ``Session`` recebida (transacional nos testes); ``main`` é
fino: teto -> gate -> cliente + cache -> execução -> commit só no sucesso.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutStatus, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from ingestion.cito.backfill_rounds import fill_bout_context
from ingestion.cito.cache import CatalogPageCache
from ingestion.cito.client import (
    CATALOG_PAGE_LIMIT,
    CallBudget,
    CitoClient,
    QuotaExceededError,
    build_cito_client,
)
from ingestion.cito.dto import CitoBout, CitoCatalogItem, CitoFighter
from ingestion.cito.gap_sync import (
    CornerOrigin,
    assign_corners,
    corner_fighters_by_slug,
    corner_sort_keys,
    map_bout_blocks,
    upsert_bout_fighter_totals,
)
from ingestion.cito.gate import HumanGateNotConfirmedError, enforce_human_gate
from ingestion.cito.matching import (
    EVENT_DATE_TOLERANCE,
    AmbiguousEventMatchError,
    is_ufc_catalog_item,
)
from ingestion.entity_resolution import AmbiguousFighterMatchError
from ingestion.incremental import (
    TableDelta,
    load_existing_fighters,
    resolve_call_budget,
    resolve_or_create_fighter,
)
from ingestion.normalize import normalize_event_name
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

SOURCE = "cito"

# Status do catálogo que marca um card por vir. O filtro é por **igualdade**, e é de propósito:
# a captura real traz um terceiro valor, ``"live"`` (3 itens em 127), que não é nem agendado nem
# realizado. Evento em andamento tem desfecho parcial e quem o ingere é o ``gap_sync``, quando
# ele virar ``completed`` -- não este módulo.
_SCHEDULED = "scheduled"

# Recorte do catálogo que o cache em disco JÁ PAGOU
# (``.cache/cito/catalog_l100_f2024-04-01_tall_ib_p1..p6.json``). A chave de cache embute
# limit/from/to/includeBouts (``CitoClient._catalog_cache_key``), então mudar **qualquer** um
# destes transforma a execução inteira em cache miss e cobra quota nova -- inclusive um default
# "mais lógico" como ``today``. Não trocar sem medir o custo.
CATALOG_WINDOW_START = date(2024, 4, 1)


def select_scheduled_events(catalog: Sequence[CitoCatalogItem]) -> list[CitoCatalogItem]:
    """Eventos UFC com status ``'scheduled'`` do catálogo, em ordem estável ``(data, slug)``.

    Filtro de promoção por **exclusão** (``is_ufc_catalog_item``), aplicado **antes** do
    casamento: 'cryptocom-ufc-331' é UFC e 'road-to-ufc-...' não é, então nenhum prefixo de slug
    serviria.

    **Sem corte por data**: evento ``scheduled`` de data passada é o caso que a CA-29 manda a
    tela tratar (card cujo evento já passou e cujas lutas seguem agendadas) -- filtrá-lo aqui
    tornaria aquela CA inalcançável. Quantos são futuros é **medido** pelo relatório, nunca
    assumido.

    Função pura: não toca o banco e não recebe ``today`` (não há o que parametrizar sem corte
    por data).
    """
    selected = [item for item in catalog if item.status == _SCHEDULED and is_ufc_catalog_item(item)]
    selected.sort(key=lambda item: (item.local_date, item.slug))
    logger.info(
        "Seleção de agendados: %d evento(s) UFC 'scheduled' de %d item(ns) de catálogo.",
        len(selected),
        len(catalog),
    )
    return selected


# --------------------------------------------------------------------------- #
# A guarda da RF-32: a decisão de NÃO persistir precede qualquer escrita.
# --------------------------------------------------------------------------- #


class ScheduledSkipReason(StrEnum):
    """Por que uma luta agendada não foi persistida -- o vocabulário do relatório.

    Local da ingestão de propósito: **não** é o ``UnavailableCode`` da API
    (``apps.predictions``), que nasce na Slice 05 e descreve o estado de uma **previsão**, não
    de uma ingestão. Antecipá-lo criaria dependência invertida entre camadas.
    """

    CANCELLED = "cancelled"
    MISSING_CORNERS = "missing_corners"
    REPEATED_FIGHTER = "repeated_fighter"
    AMBIGUOUS_FIGHTER = "ambiguous_fighter"
    HALF_PERSISTED = "half_persisted"


def persisted_bout_corners(
    session: Session, event_id: int
) -> dict[frozenset[int], tuple[int, frozenset[Corner]]]:
    """Chave natural (par não-ordenado de ``fighter_id``) -> ``(bout_id, cantos persistidos)``.

    Inclui a luta de **um canto só**, que ``incremental._existing_bout_index`` descarta (ele só
    indexa luta com exatamente dois cantos): sem ela no índice, o upsert inseriria uma
    **segunda** luta para o mesmo par em vez de reportar meia luta, e a chave natural de
    ``bouts`` passaria a ter dois donos.

    Materializado **uma vez** por evento (uma consulta), no mesmo padrão de índice pré-computado
    de ``resolve_gap_fighters`` e ``seed_bouts``.
    """
    por_luta: dict[int, dict[int, Corner]] = {}
    for bout_id, fighter_id, corner in session.execute(
        select(BoutFighter.bout_id, BoutFighter.fighter_id, BoutFighter.corner)
        .join(Bout, Bout.id == BoutFighter.bout_id)
        .where(Bout.event_id == event_id)
    ):
        por_luta.setdefault(bout_id, {})[fighter_id] = corner
    return {
        frozenset(por_lutador): (bout_id, frozenset(por_lutador.values()))
        for bout_id, por_lutador in por_luta.items()
    }


def reject_half_card(
    bout: CitoBout,
    fighter_ids: Mapping[str, int],
    persisted: Mapping[frozenset[int], tuple[int, frozenset[Corner]]],
) -> ScheduledSkipReason | None:
    """Motivo para **não** persistir esta luta; ``None`` libera a escrita.

    A decisão precede qualquer escrita (mesma disciplina de ``sync_catalog``: não há parcial a
    reverter, então não há SAVEPOINT por luta). É a pré-condição de TODO o card de evento futuro
    ter palpite: sem dois cantos, ``apps.predictions.services._corners`` marca a luta como
    indisponível e o card inteiro fica sem previsão (RF-32 / PASSO-00 da Slice 09).

    As três recusas, nesta ordem:

    1. **canto sem ``fighter_id``** -- a entity resolution ficou ambígua, e aqui ambiguidade não
       gasta quota para desempatar (a execução é de teto 0) nem escolhe o "mais provável";
    2. **os dois cantos no mesmo ``fighter_id``** -- é o furo não óbvio:
       ``upsert_bout_fighter_totals`` indexa por ``fighter_id``, então gravaria **uma** linha e a
       luta nasceria com um canto só, em silêncio;
    3. **luta já persistida sem os dois cantos** -- meia luta não se completa às cegas.

    A quarta recusa da RF-32 (luta sem exatamente dois cantos distintos no payload) acontece
    antes, em ``gap_sync.map_bout_blocks``, que a descarta -- quem chama a conta como
    ``MISSING_CORNERS``.
    """
    slugs = [corner.slug for corner in bout.corners]
    if any(slug not in fighter_ids for slug in slugs):
        return ScheduledSkipReason.AMBIGUOUS_FIGHTER

    ids = {fighter_ids[slug] for slug in slugs}
    if len(ids) != 2:
        return ScheduledSkipReason.REPEATED_FIGHTER

    existente = persisted.get(frozenset(ids))
    if existente is not None and existente[1] != {Corner.RED, Corner.BLUE}:
        return ScheduledSkipReason.HALF_PERSISTED
    return None


# --------------------------------------------------------------------------- #
# Upserts do agendado. Locais de propósito: ``upsert_event``/``upsert_bout`` do M1 não servem
# (aquele tem chave de data EXATA e não carimba identificador; este exige ``BoutCore`` com
# ``method`` não-nulo e não escreve ``status``). Alargar o ``BoutCore`` respingaria em
# ``seed_bouts``, ``incremental`` e ``_complete_scheduled_bout`` -- mudança maior, em módulo
# compartilhado, por um ganho menor.
# --------------------------------------------------------------------------- #


def upsert_scheduled_event(session: Session, item: CitoCatalogItem) -> tuple[Event, bool]:
    """Get-or-create do evento agendado; devolve ``(evento, inserido)``.

    Chaves, nesta ordem: ``cito_event_id`` (exato -- é o que um rerun encontra) e, na ausência,
    nome normalizado dentro de **+/-1 dia** da ``local_date`` (o mesmo critério de
    ``resolve_event_match``, no **sentido inverso**: lá a âncora é o evento persistido e o
    catálogo dá o identificador; aqui o catálogo é a âncora e a base é o destino -- a função de
    lá não é reusável porque o parâmetro dela é um ``Event`` já persistido).

    Mais de um candidato na janela levanta ``AmbiguousEventMatchError``: nunca casar em
    silêncio. O slug **nunca** é derivado (ADR 0005). Idempotente: identificador e ``location``
    são atribuídos só quando o valor muda, então rerun não produz UPDATE.
    """
    por_id = session.scalars(select(Event).where(Event.cito_event_id == item.id)).one_or_none()
    if por_id is not None:
        _assign_if_changed(por_id, item)
        return por_id, False

    chave = normalize_event_name(item.title)
    candidatos = [
        evento
        for evento in session.scalars(
            select(Event).where(
                Event.date >= item.local_date - EVENT_DATE_TOLERANCE,
                Event.date <= item.local_date + EVENT_DATE_TOLERANCE,
            )
        )
        if normalize_event_name(evento.name) == chave
    ]
    if len(candidatos) > 1:
        raise AmbiguousEventMatchError(
            f"O item de catálogo {item.slug!r} ({item.local_date}) casa por nome com "
            f"{len(candidatos)} eventos persistidos na janela de +/-1 dia "
            f"(ids {', '.join(str(evento.id) for evento in candidatos)}); "
            "nunca casar em silêncio. Nada foi escrito."
        )
    if candidatos:
        _assign_if_changed(candidatos[0], item)
        return candidatos[0], False

    evento = Event(
        name=item.title,
        date=item.local_date,
        location=item.location_text,
        cito_slug=item.slug,
        cito_event_id=item.id,
        source=SOURCE,
    )
    session.add(evento)
    session.flush()  # materializa o id para as FKs de bouts
    logger.info(
        "Evento agendado %r (%s, slug %r) inserido com source=%s",
        item.title,
        item.local_date,
        item.slug,
        SOURCE,
    )
    return evento, True


def _assign_if_changed(event: Event, item: CitoCatalogItem) -> None:
    """Carimba identificadores e local só quando o valor muda -- rerun não produz UPDATE.

    Mesma disciplina de ``sync_catalog._assign_if_changed``, com ``location`` a mais: o dado vem
    no payload já pago, a coluna existe e é servida em ``EventOut``; deixá-la nula renderizaria
    "sem local" nos eventos próximos e exigiria reingestão para corrigir.

    ``name``, ``date`` e ``source`` de um evento **já persistido** não são tocados: a linha é
    dele (possivelmente do seed), e ``source`` é a origem da LINHA, não do campo.
    """
    if event.cito_slug != item.slug:
        event.cito_slug = item.slug
    if event.cito_event_id != item.id:
        event.cito_event_id = item.id
    if event.location is None and item.location_text is not None:
        event.location = item.location_text


def upsert_scheduled_bout(session: Session, event_id: int, bout_id: int | None) -> int:
    """Get-or-create da luta **agendada**; devolve o ``bout_id`` persistido.

    ``method``/``round``/``ending_time``/``winner`` ficam **nulos** e ``status`` é
    ``SCHEDULED``. ``map_bout_core`` **não** é usada: ela degrada método nulo para
    ``NO_CONTEST``, que é conservador no enriquecimento e dado **falso** na criação -- o método é
    o rótulo que o preditivo treina.
    """
    if bout_id is not None:
        return bout_id
    db_bout = Bout(
        event_id=event_id,
        winner_id=None,
        method=None,
        round=None,
        ending_time_seconds=None,
        status=BoutStatus.SCHEDULED,
        source=SOURCE,
    )
    session.add(db_bout)
    session.flush()  # materializa o id para as FKs de bout_fighters
    return db_bout.id


def resolve_card_fighters(
    session: Session, fighters_by_slug: Mapping[str, CitoFighter]
) -> tuple[dict[str, int], tuple[str, ...], int]:
    """``slug -> fighter_id`` dos cantos do card, os slugs ambíguos e quantos foram criados.

    **Não** é ``resolve_gap_fighters``: aquele paga uma chamada de perfil para desempatar
    ambiguidade, e aqui isso é proibido (a execução é de teto 0) e seria inútil (o ``profile``
    embutido não traz DOB). Ambiguidade entra na lista, a luta daquele lutador é pulada, e o
    desempate é decisão humana.

    O índice de persistidos é materializado **uma** vez e reusado em todas as resoluções (mesmo
    padrão de ``resolve_event_fighters``), o que também mantém coerente o lutador recém-criado
    para os cantos seguintes do mesmo card.
    """
    existing = load_existing_fighters(session)
    antes = len(existing)
    resolved: dict[str, int] = {}
    ambiguous: list[str] = []
    for slug in sorted(fighters_by_slug):
        try:
            resolved[slug] = resolve_or_create_fighter(session, fighters_by_slug[slug], existing)
        except AmbiguousFighterMatchError as exc:
            logger.warning(
                "Lutador %r (slug %r) ambíguo pelo nome; a luta dele é pulada e o desempate é "
                "decisão humana (zero quota aqui, por teto 0): %s",
                fighters_by_slug[slug].name,
                slug,
                exc,
            )
            ambiguous.append(slug)
    return resolved, tuple(ambiguous), len(existing) - antes


@dataclass(frozen=True)
class ScheduledEventResult:
    """O que a ingestão de **um** evento agendado produziu (base do resumo do lote)."""

    event_inserted: int
    without_card: bool
    bouts: TableDelta
    bout_fighters_inserted: int
    fighters_created: int
    skipped: tuple[tuple[str, ScheduledSkipReason], ...]
    repeated_fighters: tuple[str, ...]
    corner_origins: Mapping[CornerOrigin, int]


def ingest_scheduled_event(session: Session, item: CitoCatalogItem) -> ScheduledEventResult:
    """Ingere o card agendado de **um** item de catálogo; nenhuma chamada de rede.

    Encadeia: ``upsert_scheduled_event`` -> ``map_bout_blocks`` (que já descarta cancelada e
    luta sem dois cantos distintos) -> ``corner_fighters_by_slug`` -> ``resolve_card_fighters``
    (zero quota) -> por luta: ``reject_half_card`` **antes de qualquer escrita** ->
    ``assign_corners`` -> ``upsert_scheduled_bout`` -> ``fill_bout_context`` ->
    ``upsert_bout_fighter_totals`` com ``lines_by_slug`` **vazio** (a luta não aconteceu, então
    toda stat é nula).

    Sem SAVEPOINT por luta, de propósito: a decisão de persistir precede a escrita, então não
    existe estado parcial a reverter -- mesma disciplina de ``sync_catalog``. O commit é do
    chamador.
    """
    event, inserted = upsert_scheduled_event(session, item)
    blocks = list(item.bouts)
    bouts = map_bout_blocks(blocks)
    descartadas = len([block for block in blocks if not block.is_cancelled]) - len(bouts)

    fighters_by_slug = corner_fighters_by_slug(bouts, blocks)
    fighter_ids, ambiguos, fighters_created = resolve_card_fighters(session, fighters_by_slug)
    persisted = persisted_bout_corners(session, event.id)
    blocks_by_id = {block.id: block for block in blocks}
    sort_keys = corner_sort_keys(
        {slug: fighters_by_slug[slug] for slug in fighter_ids}, fighter_ids
    )

    skipped: list[tuple[str, ScheduledSkipReason]] = [
        (block.id, ScheduledSkipReason.CANCELLED) for block in blocks if block.is_cancelled
    ]
    skipped += [
        (block.id, ScheduledSkipReason.MISSING_CORNERS)
        for block in blocks
        if not block.is_cancelled and block.id not in {bout.bout_id for bout in bouts}
    ]
    bouts_inserted = 0
    bouts_reused = 0
    bf_inserted = 0
    corner_origins: Counter[CornerOrigin] = Counter()
    for bout in bouts:
        motivo = reject_half_card(bout, fighter_ids, persisted)
        if motivo is not None:
            skipped.append((bout.bout_id, motivo))
            continue
        bout, corner_origin = assign_corners(bout, blocks_by_id[bout.bout_id], sort_keys)
        corner_origins[corner_origin] += 1
        red, blue = bout.corners
        chave = frozenset({fighter_ids[red.slug], fighter_ids[blue.slug]})
        existente = persisted.get(chave)
        bout_db_id = upsert_scheduled_bout(
            session, event.id, existente[0] if existente is not None else None
        )
        if existente is None:
            persisted[chave] = (bout_db_id, frozenset({Corner.RED, Corner.BLUE}))
            bouts_inserted += 1
        else:
            bouts_reused += 1
        fill_bout_context(session, bout_db_id, blocks_by_id[bout.bout_id])
        # Os dois cantos, SEM linha de boutStats: a luta não aconteceu, então toda stat é NULA.
        _bf_ids, inserted_corners = upsert_bout_fighter_totals(
            session,
            bout_db_id,
            ((red.slug, Corner.RED), (blue.slug, Corner.BLUE)),
            {},
            fighter_ids,
        )
        bf_inserted += inserted_corners

    repetidos = _fighters_in_multiple_bouts(bouts, fighter_ids)
    if descartadas:
        logger.warning(
            "Evento %r: %d luta(s) sem exatamente dois cantos distintos, descartada(s).",
            item.slug,
            descartadas,
        )
    return ScheduledEventResult(
        event_inserted=1 if inserted else 0,
        without_card=not blocks,
        bouts=TableDelta(inserted=bouts_inserted, updated=bouts_reused),
        bout_fighters_inserted=bf_inserted,
        fighters_created=fighters_created,
        skipped=tuple(skipped)
        + tuple((slug, ScheduledSkipReason.AMBIGUOUS_FIGHTER) for slug in ambiguos),
        repeated_fighters=repetidos,
        corner_origins=dict(corner_origins),
    )


def _fighters_in_multiple_bouts(
    bouts: Sequence[CitoBout], fighter_ids: Mapping[str, int]
) -> tuple[str, ...]:
    """Lutadores escalados em mais de uma luta do mesmo card, pelo slug (ordem estável).

    **Não** é descarte: é dado possivelmente legítimo (lutador reserva aparece duas vezes num
    card provisório), mas é a segunda via pela qual o card perde palpite --
    ``apps.predictions.services._repeated_fighter_ids`` tira essas lutas do lote. Por isso é
    reportado, nunca silenciado.
    """
    ocorrencias: Counter[str] = Counter(
        corner.slug for bout in bouts for corner in bout.corners if corner.slug in fighter_ids
    )
    return tuple(sorted(slug for slug, total in ocorrencias.items() if total > 1))


# --------------------------------------------------------------------------- #
# Execução em lote: uma leitura do catálogo -> evento a evento -> relatório observável.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScheduledSyncReport:
    """O que a execução fez, com cada contagem separada por natureza.

    Nada de total mudo: um total que se move sem ninguém notar é o defeito que esta SPEC
    encontrou quatro vezes. Inserido e reusado são contados **à parte** porque é a diferença
    entre eles que PROVA a idempotência; os descartes vêm **item a item** porque um número
    sozinho não diz qual luta o card perdeu; e ``events_without_card`` é lista de slug, não
    contagem, pelo mesmo motivo.

    ``events_in_the_future`` é **medido**, nunca assumido: não há corte por data (Decisão 5), e
    um agendado de data já passada é exatamente o caso que a CA-29 manda a tela tratar.
    """

    catalog_items: int
    scheduled_items: int
    events_in_the_future: int
    events_without_card: tuple[str, ...]
    events: TableDelta
    bouts: TableDelta
    bout_fighters_inserted: int
    fighters_created: int
    skipped_bouts: tuple[tuple[str, str, ScheduledSkipReason], ...]
    fighters_in_multiple_bouts: tuple[tuple[str, str], ...]
    corner_origins: Mapping[CornerOrigin, int]
    cito_calls_used: int


def ingest_scheduled_events(
    session: Session,
    items: Sequence[CitoCatalogItem],
    *,
    today: date,
    catalog_items: int | None = None,
    cito_calls_used: int = 0,
) -> ScheduledSyncReport:
    """Ingere os itens agendados **já selecionados** e devolve o relatório agregado.

    Separada de ``run_scheduled_sync`` porque é a parte que não conhece cliente nem cache: é
    sobre ela que os testes de relatório e de guarda operam, com itens recortados à mão.

    ``today`` entra por parâmetro (determinismo; ``date.today()`` é proibido pela DTZ011) e é
    usada **só** para medir quantos eventos são futuros -- nunca para filtrar.
    """
    events_inserted = 0
    events_reused = 0
    bouts_inserted = 0
    bouts_reused = 0
    bf_inserted = 0
    fighters_created = 0
    without_card: list[str] = []
    skipped: list[tuple[str, str, ScheduledSkipReason]] = []
    repetidos: list[tuple[str, str]] = []
    corner_origins: Counter[CornerOrigin] = Counter()
    for item in items:
        resultado = ingest_scheduled_event(session, item)
        events_inserted += resultado.event_inserted
        events_reused += 1 - resultado.event_inserted
        bouts_inserted += resultado.bouts.inserted
        bouts_reused += resultado.bouts.updated
        bf_inserted += resultado.bout_fighters_inserted
        fighters_created += resultado.fighters_created
        if resultado.without_card:
            without_card.append(item.slug)
        skipped += [(item.slug, bout_id, motivo) for bout_id, motivo in resultado.skipped]
        repetidos += [(item.slug, slug) for slug in resultado.repeated_fighters]
        corner_origins.update(resultado.corner_origins)

    report = ScheduledSyncReport(
        catalog_items=catalog_items if catalog_items is not None else len(items),
        scheduled_items=len(items),
        events_in_the_future=sum(1 for item in items if item.local_date > today),
        events_without_card=tuple(without_card),
        events=TableDelta(inserted=events_inserted, updated=events_reused),
        bouts=TableDelta(inserted=bouts_inserted, updated=bouts_reused),
        bout_fighters_inserted=bf_inserted,
        fighters_created=fighters_created,
        skipped_bouts=tuple(skipped),
        fighters_in_multiple_bouts=tuple(repetidos),
        corner_origins=dict(corner_origins),
        cito_calls_used=cito_calls_used,
    )
    _log_report(report)
    return report


def run_scheduled_sync(
    session: Session,
    client: CitoClient,
    budget: CallBudget,
    *,
    today: date,
    date_from: date = CATALOG_WINDOW_START,
    page_limit: int = CATALOG_PAGE_LIMIT,
    cache: CatalogPageCache | None = None,
) -> ScheduledSyncReport:
    """Lê o catálogo **uma vez** (paginado, com card) e ingere todo agendado UFC dele.

    O recorte default (``date_from``/``page_limit``/``include_bouts``, **sem** ``date_to``) é
    exatamente o do cache já pago -- ver ``CATALOG_WINDOW_START``. Com um ``cache``, página já
    baixada vem do disco sem cobrar o ``CallBudget``, o que é o custo zero da CA-08.

    Opera sobre a ``Session`` recebida; o commit é do chamador.
    """
    catalog = client.fetch_event_catalog(
        limit=page_limit, date_from=date_from, date_to=None, cache=cache, include_bouts=True
    )
    selected = select_scheduled_events(catalog)
    return ingest_scheduled_events(
        session,
        selected,
        today=today,
        catalog_items=len(catalog),
        cito_calls_used=budget.used,
    )


def _log_report(report: ScheduledSyncReport) -> None:
    """Emite o relatório via ``logging`` (``print`` é proibido -- regra T20)."""
    logger.info(
        "Resumo da ingestão de agendados (source=%s): itens de catálogo=%d; agendados UFC=%d "
        "(futuros=%d); eventos sem card=%d; eventos inseridos=%d / reusados=%d; "
        "lutas inseridas=%d / reusadas=%d; cantos inseridos=%d; lutadores criados=%d; "
        "canto real (arte)=%d; canto atribuído (sem arte)=%d; canto atribuído (arte em "
        "conflito)=%d; lutas descartadas=%d; lutadores em mais de uma luta=%d; "
        "chamadas Cito=%d",
        SOURCE,
        report.catalog_items,
        report.scheduled_items,
        report.events_in_the_future,
        len(report.events_without_card),
        report.events.inserted,
        report.events.updated,
        report.bouts.inserted,
        report.bouts.updated,
        report.bout_fighters_inserted,
        report.fighters_created,
        report.corner_origins.get(CornerOrigin.ART, 0),
        report.corner_origins.get(CornerOrigin.ASSIGNED_NO_ART, 0),
        report.corner_origins.get(CornerOrigin.ASSIGNED_CONFLICTING_ART, 0),
        len(report.skipped_bouts),
        len(report.fighters_in_multiple_bouts),
        report.cito_calls_used,
    )
    for slug in report.events_without_card:
        logger.warning("Evento agendado %r não traz card (bouts vazio); ausência legítima.", slug)
    for event_slug, bout_id, motivo in report.skipped_bouts:
        logger.warning(
            "Luta %r do evento %r NÃO persistida: %s. A luta não foi gravada pela metade.",
            bout_id,
            event_slug,
            motivo.value,
        )
    for event_slug, fighter_slug in report.fighters_in_multiple_bouts:
        logger.warning(
            "Lutador %r aparece em mais de uma luta do card %r: a predição em lote tira essas "
            "lutas do card (apps.predictions.services._repeated_fighter_ids).",
            fighter_slug,
            event_slug,
        )


# --------------------------------------------------------------------------- #
# CLI: teto -> gate humano -> cliente + cache -> execução -> commit só no sucesso.
# --------------------------------------------------------------------------- #

# Conjunto de fixtures da demonstração (``--fixture``): recorte dos itens reais da captura de
# 2026-09-01, nunca dado inventado.
_DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "tests"
    / "ingestion"
    / "fixtures"
    / "catalog_agendados_derivado"
)

# Diretório default do cache em disco resumível (padrão do M5).
_DEFAULT_CACHE_DIR = Path(".cache") / "cito"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando da ingestão de agendados.

    **Sem ``--to``**, deliberadamente: o cache já pago foi gravado com ``to`` ausente (sufixo
    ``tall`` na chave), então oferecer a flag convidaria a transformar a execução inteira em
    cache miss e cobrar quota nova.
    """
    parser = argparse.ArgumentParser(
        description="Ingere os eventos e cards AGENDADOS do catálogo Cito (upsert idempotente).",
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Usa o modo fixture do cliente (lê JSON local, sem consumir a quota da Cito).",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=_DEFAULT_FIXTURE_DIR,
        help="Diretório das fixtures de catálogo (usado apenas com --fixture).",
    )
    parser.add_argument(
        "--somente-cache",
        action="store_true",
        help=(
            "Roda só com o que já está no cache em disco: resolve o teto em 0, então uma página "
            "ausente ABORTA em vez de gastar quota. Dispensa --confirmar-gasto-de-quota."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=_DEFAULT_CACHE_DIR,
        help="Diretório do cache em disco resumível das páginas do catálogo.",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        type=date.fromisoformat,
        default=CATALOG_WINDOW_START,
        help=(
            "Início da janela do catálogo (AAAA-MM-DD; default: "
            f"{CATALOG_WINDOW_START.isoformat()}, o recorte que o cache em disco já pagou). "
            "Mudar isto transforma a execução em cache miss e cobra quota nova."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=CATALOG_PAGE_LIMIT,
        help=f"Itens por página do catálogo (default: {CATALOG_PAGE_LIMIT}, o máximo da Cito).",
    )
    parser.add_argument(
        "--call-budget",
        type=int,
        default=None,
        help=(
            "Teto de chamadas à Cito nesta execução "
            "(CLI vence CITO_CALL_BUDGET, que vence o default de 500)."
        ),
    )
    parser.add_argument(
        "--confirmar-gasto-de-quota",
        action="store_true",
        help="Confirmação humana explícita para gastar quota real da Cito (gate da rede real).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint ``python -m ingestion.cito.sync_scheduled`` (ingestão dos cards agendados).

    Resolve o teto, aplica o **gate humano**, e só então instancia o cliente, abre a sessão e
    executa -- **commitando** só no sucesso. Estourar o teto (``QuotaExceededError``) é
    capturado: como nenhuma escrita precede a leitura completa do catálogo, o comando encerra
    com ``sys.exit(1)`` sem alcançar o commit.

    A data corrente vem de ``datetime.now(UTC).date()``: ``date.today()`` é proibido (DTZ011).
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)

    # O teto é resolvido ANTES do gate -- invertendo a ordem dos demais comandos do pacote --
    # porque é ele que diz se a execução PODE alcançar a rede. O gate protege a QUOTA, não o
    # modo: com teto 0 o CallBudget levanta QuotaExceededError antes do primeiro fetch, então
    # rodar contra .cache/cito/ (a CA-08) não exige confirmação. Rede real com teto > 0 sem
    # --confirmar-gasto-de-quota continua abortando antes da primeira chamada (CA-09).
    try:
        limit = 0 if args.somente_cache else resolve_call_budget(args.call_budget, os.environ)
    except ValueError as exc:
        logger.error("Configuração inválida: %s", exc)
        sys.exit(2)

    offline = args.fixture or limit == 0
    try:
        enforce_human_gate(fixture=offline, confirmed=args.confirmar_gasto_de_quota)
    except HumanGateNotConfirmedError as exc:
        logger.error("Gate humano não confirmado: %s", exc)
        sys.exit(1)

    budget = CallBudget(limit=limit)
    client = build_cito_client(fixture=args.fixture, fixture_dir=args.fixture_dir, budget=budget)
    cache = CatalogPageCache(args.cache_dir)

    with SessionLocal() as session:
        try:
            run_scheduled_sync(
                session,
                client,
                budget,
                today=datetime.now(UTC).date(),
                date_from=args.date_from,
                page_limit=args.limit,
                cache=cache,
            )
        except QuotaExceededError as exc:
            logger.error("Ingestão de agendados interrompida sem escrita: %s", exc)
            sys.exit(1)
        session.commit()


if __name__ == "__main__":
    main()
