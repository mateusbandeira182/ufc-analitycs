"""Fechamento do gap de 12 meses: eventos novos do catálogo Cito -> base (M6, Slice 06).

O CSV do Kaggle está congelado em setembro de 2025 (ADR 0002): tudo posterior só existe na
Cito. Este módulo é o outro sentido do que a Sprint 007-03 fez -- lá o evento **persistido** era
a âncora e o catálogo dava o identificador; aqui o **catálogo** é a âncora e a base é o destino.
Para cada evento UFC realizado que a base ainda não tem, uma **única** chamada de stats
(``GET /api/v1/ufc/events/{slug}/stats``) traz o card, o resultado, os totais por canto e o
round-a-round, e tudo é persistido numa transação só, com ``source="cito"``.

Invariantes
-----------
- **Descoberta antes do fetch**: o evento já persistido é descartado em ``select_gap_events``,
  antes de qualquer chamada -- é o que torna o rerun gratuito em quota (CA-04).
- **Uma chamada por evento**: os quatro blocos vêm juntos; a chamada de perfil
  (``GET /fighters/{slug}``) é gasta **apenas** para desempatar ambiguidade de entity
  resolution (RF-13), nunca em lote.
- **SAVEPOINT por evento** (``session.begin_nested``): falha no meio -- ambiguidade
  irresolvível, estouro de quota -- reverte só aquele evento; os anteriores permanecem.
- **Idempotência por chave natural** em todos os níveis, reusando o M1 (``upsert_event``,
  ``upsert_bout``) e o M5 (``upsert_bout_fighter_rounds``).
- **Entity resolution do M1** (``resolve_or_create_fighter``): nome normalizado + DOB como
  desempate, ambiguidade **falha alto** antes de qualquer insert -- nunca duplica em silêncio.
- **Filtro de promoção por exclusão** (``is_ufc_catalog_item``, Sprint 007-03), nunca por
  prefixo de slug: 'cryptocom-ufc-331' é UFC e 'road-to-ufc-...' não é.
- **Gate humano antes da rede real** (``ingestion.cito.gate``) e ``CallBudget`` com teto.

A lógica testável opera sobre a ``Session`` recebida (transacional nos testes); ``main`` é fino:
gate -> teto -> cliente + cache -> ``run_gap_sync`` -> commit só no sucesso. Toda a suíte roda em
modo fixture -- zero rede real.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito.backfill_rounds import fill_bout_context, upsert_bout_fighter_rounds
from ingestion.cito.cache import CatalogPageCache, EventStatsCache
from ingestion.cito.client import (
    CATALOG_PAGE_LIMIT,
    CallBudget,
    CitoClient,
    QuotaExceededError,
    build_cito_client,
)
from ingestion.cito.dto import (
    CitoBout,
    CitoBoutBlock,
    CitoBoutFighterRef,
    CitoBoutStatLine,
    CitoCatalogItem,
    CitoCorner,
    CitoEvent,
    CitoEventStats,
    CitoFighter,
    CitoRoundStatLine,
)
from ingestion.cito.gate import HumanGateNotConfirmedError, enforce_human_gate
from ingestion.cito.matching import is_ufc_catalog_item, normalize_event_name
from ingestion.entity_resolution import AmbiguousFighterMatchError, match_fighter_id_by_age
from ingestion.incremental import (
    TableDelta,
    is_known_method_token,
    load_existing_fighters,
    map_bout_core,
    resolve_call_budget,
    resolve_or_create_fighter,
    upsert_bout,
    upsert_event,
)
from ingestion.normalize import normalize_name
from mma_analytics.db import SessionLocal

# Linhas de stat compartilham a forma (``CitoRoundStatLine`` estende ``CitoBoutStatLine``): o
# agrupamento por luta é o mesmo para totais e rounds, e o TypeVar preserva o tipo concreto.
CitoBoutStatLineT = TypeVar("CitoBoutStatLineT", bound=CitoBoutStatLine)

logger = logging.getLogger(__name__)

SOURCE = "cito"


# POR QUE O CANTO NÃO VEM DA CITO -- não "conserte" isto de volta para o campo da fonte.
#
# O `corner` de ``bouts[].fighters[]`` é o **desfecho**, não o canto de caminhada. Medido em
# 2026-09-01 sobre 157 payloads de stats: entre 1.888 lutas decididas o vencedor está em `red`
# em **1.882 (99,7%)**; os `outcome` confirmam (`red/win` 1.884 contra `red/loss` 4). A nossa
# própria base do Kaggle mede **64,6%**, que é a taxa real do UFC -- 99,7% é impossível.
#
# Persistir aquele rótulo era gravar dado falso numa coluna com significado no mundo real, e
# pior: o alvo do modelo É o canto (``analysis.dataset`` treina ``target_winner_corner`` e
# ``ingestion.features.matchup.pivot_corners`` monta o lado A pelo vermelho). Com o rótulo da
# Cito, o alvo ficava fixo em `red` para toda luta vinda dela, e qualquer feature que
# distinguisse vencedor de perdedor virava perfeitamente preditiva -- vazamento de rótulo.
#
# Decisão do humano (2026-09-01): o canto de luta ingerida da Cito é **atribuído**, pela ordem
# lexicográfica do **nome normalizado** do par (desempate por ``fighter_id``). O vencedor cai no
# vermelho em ~50% dos casos: vira ruído, não viés. A procedência já é rastreável por
# ``source="cito"`` -- não há coluna nova.
#
# A LIÇÃO, que custou uma iteração: **ser independente do resultado não basta; o critério tem de
# ser independente da FONTE.** A primeira versão ordenava por ``fighter_id`` e era, sim,
# independente do resultado (46,8% de vitórias do vermelho, medido). Mas os ids do seed vão até
# 2.611 e os criados pela ingestão começam em 2.640, então todo lutador **inédito** perdia a
# comparação e caía no azul -- 133 de 133 no lote real. Isso não era canto neutro: era um
# marcador de "estreante na base" disfarçado de canto, e enviesava as features (o
# ``layoff_days_diff`` médio dava +57,8 nas lutas da Cito contra -11,7 nas do Kaggle). O nome
# normalizado não sabe nem quem venceu nem quem é novo.


class GapSyncError(Exception):
    """Falha no fechamento do gap que não pode degradar em silêncio (payload inconsistente)."""


class GapAmbiguityError(GapSyncError, AmbiguousFighterMatchError):
    """Ambiguidade de entity resolution num evento do gap, carregando a quota já gasta nele.

    Herda de ``AmbiguousFighterMatchError`` de propósito: quem só quer saber que a resolução
    ficou ambígua continua capturando a exceção do M1, sem conhecer este módulo. O que ela
    acrescenta é o **custo**: um evento pulado ainda consumiu a chamada de stats e as de
    desempate, e um resumo que as descartasse reportaria consumo menor que o real -- o oposto
    do que um teto de quota precisa.
    """

    def __init__(self, message: str, *, stats_calls_used: int, profile_calls_used: int) -> None:
        super().__init__(message)
        self.stats_calls_used = stats_calls_used
        self.profile_calls_used = profile_calls_used


# Status do catálogo que marca um evento já realizado. Um evento 'scheduled' não tem resultado
# nem stats: buscá-lo gastaria quota para gravar um card sem desfecho.
_COMPLETED = "completed"


def _persisted_event_keys(session: Session) -> tuple[set[str], set[tuple[str, date]]]:
    """Fotografa o que a base já tem: os ``cito_slug`` e as chaves naturais (nome, data).

    Duas chaves porque nenhuma sozinha basta. O ``cito_slug`` é o identificador exato e casa
    mesmo quando o nome persistido do seed diverge do título do catálogo; a chave natural
    ``(normalize_event_name(name), date)`` cobre o evento persistido **sem** identificador
    (ingerido antes da sincronização de catálogo), que só o nome desmascara. Reusa a
    normalização de nome de evento da Sprint 007-03, que também neutraliza a pontuação
    ('UFC 319: Du Plessis vs. Chimaev' -> 'ufc 319 du plessis vs chimaev').
    """
    slugs: set[str] = set()
    keys: set[tuple[str, date]] = set()
    for name, event_date, cito_slug in session.execute(
        select(Event.name, Event.date, Event.cito_slug)
    ):
        keys.add((normalize_event_name(name), event_date))
        if cito_slug is not None:
            slugs.add(cito_slug)
    return slugs, keys


def latest_persisted_event_date(session: Session) -> date | None:
    """Data do evento mais recente já persistido; base vazia -> ``None`` (sem corte inferior)."""
    return session.scalar(select(Event.date).order_by(Event.date.desc()).limit(1))


def gap_window_events(
    catalog: Sequence[CitoCatalogItem],
    *,
    cutoff: date | None,
    today: date,
) -> list[CitoCatalogItem]:
    """Eventos UFC **realizados** do catálogo na janela ``[cutoff, today]``, em ordem estável.

    Descarta promoção fora do escopo (``is_ufc_catalog_item`` -- DWCS e Road to UFC, filtro por
    exclusão de marcadores da Sprint 007-03), evento ainda não realizado (status diferente de
    'completed' ou data futura -- não tem resultado nem stats a buscar) e evento anterior ao
    corte (preenchê-lo seria backfill histórico, não fechamento de gap). O corte é **inclusive**
    para não perder um card do mesmo dia do último persistido.

    Função pura: não toca o banco. É o **denominador** do resumo -- o que a base deveria ter na
    janela --, do qual o já persistido é descontado em ``select_gap_events``.
    """
    window = [
        item
        for item in catalog
        if is_ufc_catalog_item(item)
        and item.status == _COMPLETED
        and (cutoff is None or item.local_date >= cutoff)
        and item.local_date <= today
    ]
    window.sort(key=lambda item: (item.local_date, item.slug))
    return window


def select_gap_events(
    session: Session,
    catalog: Sequence[CitoCatalogItem],
    *,
    today: date,
    cutoff: date | None = None,
) -> list[CitoCatalogItem]:
    """Eventos da janela do gap que a base **ainda não tem**, em ordem cronológica estável.

    Sobre ``gap_window_events``, descarta o que já está persistido -- pelo ``cito_slug`` do
    catálogo ou pela chave natural ``(nome normalizado, data)``. É esse descarte, acontecendo
    **antes** de qualquer fetch, que torna o rerun gratuito em quota (CA-04).

    O ``cutoff`` default é o último evento persistido; informá-lo reabre a janela para trás, o
    que é o único jeito de reencontrar um evento **pulado** (ao fechar o gap à frente dele, ele
    fica atrás do corte). Reabrir não reingere nada: o descarte do já persistido continua valendo.

    A ``today`` entra por parâmetro (determinismo no teste; ``date.today()`` é proibido pela
    regra DTZ011 do ruff). Leitura pura: não escreve nada.
    """
    if cutoff is None:
        cutoff = latest_persisted_event_date(session)
    persisted_slugs, persisted_keys = _persisted_event_keys(session)

    selected = [
        item
        for item in gap_window_events(catalog, cutoff=cutoff, today=today)
        if item.slug not in persisted_slugs
        and (normalize_event_name(item.title), item.local_date) not in persisted_keys
    ]
    logger.info(
        "Descoberta do gap: %d eventos UFC ausentes entre %s e %s (catálogo com %d itens).",
        len(selected),
        cutoff,
        today,
        len(catalog),
    )
    return selected


# --------------------------------------------------------------------------- #
# Mapeadores puros do payload de stats -> DTOs do M1.
#
# São a ÚNICA fronteira deste módulo que conhece a forma do payload estendido (ADR 0005): o
# resto fala apenas os DTOs do M1, o que permite reusar ``map_bout_core``, ``upsert_bout`` e
# ``resolve_or_create_fighter`` sem adaptação. Divergiu o contrato, corrige-se só aqui.
# --------------------------------------------------------------------------- #


def _corners_by_side(bout: CitoBoutBlock) -> tuple[CitoBoutFighterRef, CitoBoutFighterRef] | None:
    """Os dois lados da luta, em ordem determinística por slug; card incompleto -> ``None``.

    A ordem é por slug, **não** pelo rótulo ``corner`` do payload, e é apenas um posicionamento
    estável até que os ``fighter_id`` existam: o canto de verdade é atribuído depois, por
    ``assign_deterministic_corners``. O porquê está no comentário "POR QUE O CANTO NÃO VEM DA
    CITO", no topo deste módulo.

    Luta sem exatamente dois cantos distintos é descartada por quem chama -- inventar um
    adversário seria fabricar luta.
    """
    por_slug = {fighter.fighter_slug: fighter for fighter in bout.fighters}
    if len(por_slug) != 2:
        return None
    primeiro, segundo = (por_slug[slug] for slug in sorted(por_slug))
    return primeiro, segundo


def map_stats_to_bouts(stats: CitoEventStats) -> list[CitoBout]:
    """Traduz o bloco ``bouts`` do payload de stats nos ``CitoBout`` do M1 (função pura).

    ``corners[0]`` é o vermelho e ``corners[1]`` o azul -- a convenção do DTO do M1, que
    ``map_bout_core`` e ``upsert_bout`` assumem. Campo de resultado ausente permanece ``None``
    (``map_bout_core`` degrada método nulo para ``NO_CONTEST``); luta cancelada ou sem os dois
    cantos é descartada com aviso, nunca completada por chute.
    """
    bouts: list[CitoBout] = []
    for bout in stats.bouts:
        if bout.is_cancelled:
            logger.info("Luta %r cancelada no card; descartada da ingestão.", bout.id)
            continue
        corners = _corners_by_side(bout)
        if corners is None:
            logger.warning(
                "Luta %r sem exatamente um canto vermelho e um azul (%d cantos); descartada.",
                bout.id,
                len(bout.fighters),
            )
            continue
        red, blue = corners
        bouts.append(
            CitoBout(
                bout_id=bout.id,
                corners=(
                    CitoCorner(slug=red.fighter_slug, name=red.fighter_name or red.fighter_slug),
                    CitoCorner(slug=blue.fighter_slug, name=blue.fighter_name or blue.fighter_slug),
                ),
                method=bout.method,
                finish_round=bout.result_round,
                finish_time_seconds=bout.result_time_seconds,
                weight_class=bout.weight_class,
                winner_slug=bout.winner_fighter_slug,
            )
        )
    return bouts


def corner_sort_keys(
    fighters_by_slug: Mapping[str, CitoFighter], fighter_ids: Mapping[str, int]
) -> dict[str, tuple[str, int]]:
    """Chave de ordenação do canto: ``(nome normalizado, fighter_id)`` por slug.

    O nome normalizado é o critério; o ``fighter_id`` só desempata o caso (inexistente na
    prática) de dois homônimos exatos na mesma luta, mantendo a ordem **total** e reprodutível em
    SQL -- é o que permite corrigir linhas já persistidas por UPDATE, sem reingestão.
    """
    return {
        slug: (normalize_name(fighter.name), fighter_ids[slug])
        for slug, fighter in fighters_by_slug.items()
    }


def assign_deterministic_corners(
    bout: CitoBout, sort_keys: Mapping[str, tuple[str, int]]
) -> CitoBout:
    """Reordena os cantos pela chave de ordenação -- o menor nome normalizado vira vermelho.

    É aqui que o canto persistido nasce, e ele **não** vem do payload -- o porquê, e a razão de o
    critério ser o nome e não o ``fighter_id``, estão no comentário "POR QUE O CANTO NÃO VEM DA
    CITO", no topo deste módulo. Como ``corners[0]`` é o
    vermelho por convenção do DTO do
    M1, reordenar o par é tudo que a atribuição precisa -- ``map_bout_core`` e
    ``upsert_bout_fighter_totals`` a seguem sem saber de nada disso.

    O ``winner_slug`` não é tocado, então quem venceu continua saindo do **slug**: reatribuir o
    canto muda em qual lado o vencedor cai, nunca quem ele é.
    """
    primeiro, segundo = bout.corners
    if sort_keys[primeiro.slug] <= sort_keys[segundo.slug]:
        return bout
    return bout.model_copy(update={"corners": (segundo, primeiro)})


def map_stats_to_event(stats: CitoEventStats) -> CitoEvent:
    """Monta o ``CitoEvent`` do M1 a partir dos blocos ``event`` e ``bouts`` do payload.

    A data é a ``event_date`` **local** do bloco de evento -- a mesma grandeza que o seed
    persistiu em ``events.date`` e a que o slug usa (ADR 0005); a data UTC de ``startsAt``
    divergiria em um dia nos cards noturnos dos EUA.
    """
    return CitoEvent(
        event_id=stats.event.id,
        name=stats.event.title,
        date=stats.event.event_date,
        bouts=map_stats_to_bouts(stats),
    )


def corner_profile_to_fighter(fighter: CitoBoutFighterRef) -> CitoFighter:
    """Monta o ``CitoFighter`` do M1 a partir do canto do card (RF-13, zero quota).

    Nome, slug, apelido e cartel vêm do ``profile`` embutido -- é isso que permite criar um
    lutador novo sem gastar uma chamada de perfil. ``date_of_birth``, ``stance``, ``height_cm``
    e ``reach_cm`` ficam ``None``: ausência explícita, não zero inventado; a antropometria não
    entra em lote e a chamada ``GET /fighters/{slug}`` é reservada ao desempate de ambiguidade
    (``resolve_gap_fighters``).

    Sem ``profile``, degrada para o nome do card (``fighter_name``). Sem nome nenhum, levanta
    ``GapSyncError``: derivar o nome do slug ('st-pierre' -> 'St Pierre') colocaria um nome
    inventado na chave de entity resolution, criando a duplicata que ela existe para impedir.
    """
    profile = fighter.profile
    name = profile.name if profile is not None else fighter.fighter_name
    if not name:
        raise GapSyncError(
            f"Canto {fighter.fighter_slug!r} sem nome no payload (nem profile, nem fighterName); "
            "nunca derivar o nome do slug -- isso corromperia a entity resolution."
        )
    record = profile.record if profile is not None else None
    return CitoFighter(
        slug=fighter.fighter_slug,
        name=name,
        nickname=profile.nickname if profile is not None else None,
        date_of_birth=None,
        height_cm=None,
        reach_cm=None,
        stance=None,
        wins=record.wins if record is not None and record.wins is not None else 0,
        losses=record.losses if record is not None and record.losses is not None else 0,
        draws=record.draws if record is not None and record.draws is not None else 0,
    )


def corner_fighters_by_slug(
    bouts: Sequence[CitoBout], stats: CitoEventStats
) -> dict[str, CitoFighter]:
    """Mapa ``slug -> CitoFighter`` de todos os cantos do card (um por lutador, sem repetir).

    Percorre o bloco ``bouts`` do payload -- a única fonte do ``profile`` embutido -- e restringe
    aos slugs das lutas que ``map_stats_to_bouts`` manteve: um canto de luta cancelada não vira
    lutador novo. Primeira ocorrência vence (o mesmo lutador só aparece uma vez por card).
    """
    wanted = {corner.slug for bout in bouts for corner in bout.corners}
    fighters: dict[str, CitoFighter] = {}
    for bout in stats.bouts:
        for fighter in bout.fighters:
            if fighter.fighter_slug in wanted and fighter.fighter_slug not in fighters:
                fighters[fighter.fighter_slug] = corner_profile_to_fighter(fighter)
    return fighters


def resolve_gap_fighters(
    session: Session,
    fighters_by_slug: Mapping[str, CitoFighter],
    client: CitoClient,
    *,
    today: date,
) -> tuple[dict[str, int], int]:
    """Mapa ``slug -> fighter_id`` de todos os cantos + nº de chamadas de perfil gastas (RF-13).

    Por slug (em ordem estável): tenta ``resolve_or_create_fighter`` com o ``CitoFighter``
    montado do payload já pago -- **zero** quota. Só quando isso levanta
    ``AmbiguousFighterMatchError`` gasta **uma** chamada ``client.get_fighter(slug)`` e tenta de
    novo. O retry é seguro porque ``match_fighter_id`` falha **antes** de qualquer insert: não há
    parcial a limpar.

    O perfil real devolve ``birthDate`` **nulo** e publica ``age`` (medido em 2026-09-01), então
    a data de nascimento sozinha não fecha o desempate. Quando ela não resolve, a mesma chamada
    -- já paga -- é reaproveitada para o desempate por **idade**
    (``match_fighter_id_by_age``), que compara a idade informada com a calculada do
    ``date_of_birth`` persistido de cada homônimo. Sem unicidade sob essa comparação, propaga:
    nunca duplicar nem mesclar em silêncio.

    O índice de lutadores persistidos é materializado **uma vez** por evento e reusado em todas
    as resoluções (mesmo padrão de ``resolve_event_fighters``, do M1): evita N varreduras da
    tabela e mantém coerente o lutador recém-criado para os cantos seguintes do mesmo card.
    """
    existing = load_existing_fighters(session)
    resolved: dict[str, int] = {}
    profile_calls = 0
    for slug in sorted(fighters_by_slug):
        fighter = fighters_by_slug[slug]
        try:
            resolved[slug] = resolve_or_create_fighter(session, fighter, existing)
        except AmbiguousFighterMatchError:
            logger.warning(
                "Lutador %r (slug %r) ambíguo pelo nome; gastando 1 chamada de perfil para "
                "desempatar pela data de nascimento.",
                fighter.name,
                slug,
            )
            profile_calls += 1
            profile = client.get_fighter(slug)
            try:
                resolved[slug] = resolve_or_create_fighter(session, profile, existing)
            except AmbiguousFighterMatchError as retry_exc:
                # A DOB não fechou (o perfil real costuma trazê-la nula): a MESMA chamada já
                # paga é reaproveitada para o desempate por idade, sem gastar quota nova.
                by_age = match_fighter_id_by_age(profile.name, profile.age, existing, today=today)
                if by_age is not None:
                    resolved[slug] = by_age
                    continue
                # O desempate foi pago e não resolveu: a exceção leva o custo consigo para que o
                # resumo do lote não subestime a quota consumida por um evento pulado.
                raise GapAmbiguityError(
                    str(retry_exc), stats_calls_used=0, profile_calls_used=profile_calls
                ) from retry_exc
    return resolved, profile_calls


# --------------------------------------------------------------------------- #
# Persistência de um evento do gap.
# --------------------------------------------------------------------------- #


def _persisted_event(session: Session, event: CitoEvent) -> Event:
    """Localiza o ``Event`` recém-garantido por ``upsert_event``, pela mesma chave natural.

    Busca escopada pela ``date`` e comparada por ``normalize_name`` -- a chave que
    ``upsert_event`` usa, e que tolera a grafia divergente de um evento vindo de outra fonte.
    Ausência aqui significa que o upsert não fez o que promete: falha alto em vez de seguir com
    um id inventado.
    """
    key = normalize_name(event.name)
    for persisted in session.scalars(select(Event).where(Event.date == event.date)):
        if normalize_name(persisted.name) == key:
            return persisted
    raise GapSyncError(f"Evento {event.name!r} ({event.date}) não encontrado após o upsert.")


def _stamp_cito_identifiers(event: Event, item: CitoCatalogItem) -> None:
    """Carimba ``cito_slug``/``cito_event_id`` do item de catálogo, só quando o valor muda.

    Mesma disciplina de ``sync_catalog._assign_if_changed``: rerun não produz UPDATE.
    """
    if event.cito_slug != item.slug:
        event.cito_slug = item.slug
    if event.cito_event_id != item.id:
        event.cito_event_id = item.id


def _build_bout_fighter(
    bout_id: int, fighter_id: int, corner: Corner, line: CitoBoutStatLine | None
) -> BoutFighter:
    """Monta o canto (``bout_fighters``) com os totais do payload; sem linha, só a participação.

    Os nove splits chegam como tuplas ``(landed, attempted)`` já parseadas na borda; split
    ausente permanece ``None`` -- nunca zero inventado. Um canto **sem** linha de ``boutStats``
    ainda vira linha: a participação na luta é dado do card, e omiti-la deixaria a luta com um
    lado só, quebrando a chave natural order-independent de ``bouts``.
    """
    if line is None:
        return BoutFighter(bout_id=bout_id, fighter_id=fighter_id, corner=corner, source=SOURCE)

    sig_landed, sig_attempted = line.sig_strikes
    total_landed, total_attempted = line.total_strikes
    head_landed, head_attempted = line.head
    body_landed, body_attempted = line.body
    leg_landed, leg_attempted = line.leg
    distance_landed, distance_attempted = line.distance
    clinch_landed, clinch_attempted = line.clinch
    ground_landed, ground_attempted = line.ground
    takedowns_landed, takedowns_attempted = line.takedowns
    return BoutFighter(
        bout_id=bout_id,
        fighter_id=fighter_id,
        corner=corner,
        knockdowns=line.knockdowns,
        sig_strikes_landed=sig_landed,
        sig_strikes_attempted=sig_attempted,
        takedowns_landed=takedowns_landed,
        takedowns_attempted=takedowns_attempted,
        submission_attempts=line.submission_attempts,
        control_time_seconds=line.control_time_seconds,
        total_strikes_landed=total_landed,
        total_strikes_attempted=total_attempted,
        head_landed=head_landed,
        head_attempted=head_attempted,
        body_landed=body_landed,
        body_attempted=body_attempted,
        leg_landed=leg_landed,
        leg_attempted=leg_attempted,
        distance_landed=distance_landed,
        distance_attempted=distance_attempted,
        clinch_landed=clinch_landed,
        clinch_attempted=clinch_attempted,
        ground_landed=ground_landed,
        ground_attempted=ground_attempted,
        reversals=line.reversals,
        source=SOURCE,
    )


def upsert_bout_fighter_totals(
    session: Session,
    bout_id: int,
    corners: Sequence[tuple[str, Corner]],
    lines_by_slug: Mapping[str, CitoBoutStatLine],
    fighter_id_by_slug: Mapping[str, int],
) -> tuple[dict[str, int], int]:
    """Get-or-create long por ``(bout_id, fighter_id)``; devolve ``{slug: bout_fighter_id}``.

    O caminho do M1 (``upsert_bout_fighters``) consome ``CitoBoutStats``, que **não** tem
    ``reversals`` nem os oito splits de golpe -- usá-lo aqui descartaria dado que a mesma
    chamada já trouxe. O mapeamento espelha ``backfill_rounds._build_round`` (mesma forma de
    linha, outra tabela).

    Idempotente: o ``fighter_id`` já presente na luta é reusado, nunca duplicado (a unicidade
    ``uq_bout_fighter`` do M0 sustenta a chave no banco). Os cantos vêm do **card**, não das
    linhas de stat: um canto sem box-score ainda é participante da luta.
    """
    existing: dict[int, int] = dict(
        session.execute(
            select(BoutFighter.fighter_id, BoutFighter.id).where(BoutFighter.bout_id == bout_id)
        )
        .tuples()
        .all()
    )
    resolved: dict[str, int] = {}
    inserted = 0
    for slug, corner in corners:
        fighter_id = fighter_id_by_slug[slug]
        bout_fighter_id = existing.get(fighter_id)
        if bout_fighter_id is None:
            bout_fighter = _build_bout_fighter(bout_id, fighter_id, corner, lines_by_slug.get(slug))
            session.add(bout_fighter)
            session.flush()  # materializa o id para as FKs de bout_fighter_rounds
            bout_fighter_id = bout_fighter.id
            existing[fighter_id] = bout_fighter_id
            inserted += 1
        resolved[slug] = bout_fighter_id
    return resolved, inserted


@dataclass(frozen=True)
class GapEventResult:
    """O que a ingestão de um evento do gap produziu (base do resumo agregado)."""

    event_inserted: int
    bouts: TableDelta
    bout_fighters: TableDelta
    rounds_inserted: int
    fighters_created: int
    profile_calls_used: int
    unmatched_stat_lines: int
    cache_hit: bool


def _group_by_bout(
    lines: Sequence[CitoBoutStatLineT],
) -> dict[str, list[CitoBoutStatLineT]]:
    """Agrupa linhas de stat (totais ou rounds) pelo ``bout_id`` da Cito, preservando a ordem."""
    grouped: dict[str, list[CitoBoutStatLineT]] = {}
    for line in lines:
        grouped.setdefault(line.bout_id, []).append(line)
    return grouped


def _reject_unknown_methods(item: CitoCatalogItem, bouts: Sequence[CitoBout]) -> None:
    """Recusa o evento inteiro se algum token de método não tiver tradução conhecida.

    No enriquecimento (M1/M5) um token não previsto degrada para ``NO_CONTEST``, o que é
    conservador porque o desfecho já veio do seed. Aqui a luta está sendo **criada**: degradar
    gravaria o rótulo que o preditivo treina como 'no contest' quando foi uma finalização --
    dado falso, com aparência de dado, exatamente o que o porteiro de contexto de card da Slice
    05 existe para impedir.

    O SAVEPOINT reverte só este evento; acrescentar o token a ``_CITO_METHOD_BY_TOKEN`` e
    reexecutar não custa quota nenhuma, porque o payload já está no cache.
    """
    unknown = sorted(
        {
            bout.method
            for bout in bouts
            if bout.method is not None and not is_known_method_token(bout.method)
        }
    )
    if unknown:
        raise GapSyncError(
            f"Evento {item.slug!r} traz token(s) de método sem tradução conhecida: "
            f"{', '.join(repr(token) for token in unknown)}. Nada foi gravado -- acrescente-os a "
            "ingestion.incremental._CITO_METHOD_BY_TOKEN e reexecute (o cache torna o retry "
            "gratuito). Nunca degradar para NO_CONTEST na criação da luta: o método é o rótulo "
            "do preditivo."
        )


def ingest_gap_event(
    session: Session,
    item: CitoCatalogItem,
    client: CitoClient,
    cache: EventStatsCache,
    *,
    today: date,
) -> GapEventResult:
    """Ingere um evento do gap com UMA chamada de stats, atomicamente (SAVEPOINT).

    Encadeia, dentro de ``session.begin_nested()``: ``cache.get_or_fetch`` (hit = 0 quota) ->
    ``upsert_event`` (M1) -> carimbo dos identificadores do catálogo -> ``resolve_gap_fighters``
    (RF-13) -> por luta: ``map_bout_core`` + ``upsert_bout`` (M1) -> ``fill_bout_context``
    (o porteiro de card da Slice 05) -> ``upsert_bout_fighter_totals`` ->
    ``upsert_bout_fighter_rounds`` (Slice 05).

    Falha no meio -- ambiguidade irresolvível, estouro de quota, payload inconsistente --
    reverte **só este evento** e propaga; os eventos já processados permanecem persistidos, e o
    retry é idempotente. O commit é do chamador.
    """
    rounds_inserted = 0
    bouts_inserted = 0
    bouts_reused = 0
    bout_fighters_inserted = 0
    bout_fighters_reused = 0
    unmatched = 0
    with session.begin_nested():
        stats, cache_hit = cache.get_or_fetch(item.slug, client.fetch_event_stats)
        cito_event = map_stats_to_event(stats)
        _reject_unknown_methods(item, cito_event.bouts)
        fighters_before = session.scalar(select(func.count()).select_from(Fighter)) or 0

        event_inserted = upsert_event(session, cito_event)
        event = _persisted_event(session, cito_event)
        _stamp_cito_identifiers(event, item)

        fighters_by_slug = corner_fighters_by_slug(cito_event.bouts, stats)
        try:
            fighter_ids, profile_calls = resolve_gap_fighters(
                session, fighters_by_slug, client, today=today
            )
        except GapAmbiguityError as exc:
            # Acrescenta o custo do fetch de stats deste evento (que a resolução não conhece):
            # o total é o que o resumo do lote precisa para não subestimar a quota consumida.
            raise GapAmbiguityError(
                str(exc),
                stats_calls_used=0 if cache_hit else 1,
                profile_calls_used=exc.profile_calls_used,
            ) from exc
        fighters_after = session.scalar(select(func.count()).select_from(Fighter)) or 0

        known_bout_ids = set(session.scalars(select(Bout.id).where(Bout.event_id == event.id)))
        blocks_by_id = {block.id: block for block in stats.bouts}
        totals_by_bout = _group_by_bout(stats.bout_stats)
        rounds_by_bout = _group_by_bout(stats.round_stats)

        sort_keys = corner_sort_keys(fighters_by_slug, fighter_ids)
        for bout in cito_event.bouts:
            # O canto persistido é ATRIBUÍDO aqui, pelo nome normalizado -- nunca o do payload.
            bout = assign_deterministic_corners(bout, sort_keys)
            red, blue = bout.corners
            bout_db_id = upsert_bout(
                session,
                event.id,
                fighter_ids[red.slug],
                fighter_ids[blue.slug],
                map_bout_core(bout),
            )
            if bout_db_id in known_bout_ids:
                bouts_reused += 1
            else:
                known_bout_ids.add(bout_db_id)
                bouts_inserted += 1
            fill_bout_context(session, bout_db_id, blocks_by_id[bout.bout_id])

            lines = totals_by_bout.get(bout.bout_id, [])
            bf_ids, inserted = upsert_bout_fighter_totals(
                session,
                bout_db_id,
                ((red.slug, Corner.RED), (blue.slug, Corner.BLUE)),
                {line.fighter_slug: line for line in lines},
                fighter_ids,
            )
            bout_fighters_inserted += inserted
            bout_fighters_reused += len(bf_ids) - inserted
            unmatched += sum(1 for line in lines if line.fighter_slug not in bf_ids)

            rounds_by_corner: dict[int, list[CitoRoundStatLine]] = {}
            for line in rounds_by_bout.get(bout.bout_id, []):
                bout_fighter_id = bf_ids.get(line.fighter_slug)
                if bout_fighter_id is None:
                    # Linha de round de um lutador que o card não lista: nunca se fabrica canto.
                    unmatched += 1
                    continue
                rounds_by_corner.setdefault(bout_fighter_id, []).append(line)
            for bout_fighter_id, corner_rounds in rounds_by_corner.items():
                rounds_inserted += upsert_bout_fighter_rounds(
                    session, bout_fighter_id, corner_rounds
                )

    return GapEventResult(
        event_inserted=event_inserted,
        bouts=TableDelta(inserted=bouts_inserted, updated=bouts_reused),
        bout_fighters=TableDelta(inserted=bout_fighters_inserted, updated=bout_fighters_reused),
        rounds_inserted=rounds_inserted,
        fighters_created=fighters_after - fighters_before,
        profile_calls_used=profile_calls,
        unmatched_stat_lines=unmatched,
        cache_hit=cache_hit,
    )


# --------------------------------------------------------------------------- #
# Execução em lote: descoberta -> ingestão evento a evento -> resumo observável.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GapSyncSummary:
    """Resumo observável da execução: deltas, quota gasta e quão atrasada a base ficou.

    ``stats_calls_used`` é contado à parte de ``cito_calls_used`` porque respondem perguntas
    diferentes: o primeiro é o custo **por evento** do gap (zero num rerun, que é o CA-04); o
    segundo é o consumo total do orçamento, que inclui as páginas de catálogo.

    ``events_ambiguous`` lista os slugs que a entity resolution não conseguiu resolver -- é a
    lacuna **explícita** que o humano precisa ver para decidir o desempate. Nada foi gravado
    para eles.
    """

    events_ingested: int
    events_skipped: int
    events_ambiguous: tuple[str, ...]
    fighters_created: int
    bouts: TableDelta
    bout_fighters: TableDelta
    rounds_inserted: int
    unmatched_stat_lines: int
    profile_calls_used: int
    stats_calls_used: int
    cache_hits: int
    cito_calls_used: int
    latest_persisted_date: date | None
    latest_catalog_date: date | None
    source: str

    @property
    def days_behind(self) -> int | None:
        """Distância em dias entre o último evento persistido e o último realizado do catálogo.

        ``None`` quando falta uma das pontas (base vazia ou catálogo sem evento realizado):
        ausência é reportada como ausência, nunca como zero.
        """
        if self.latest_persisted_date is None or self.latest_catalog_date is None:
            return None
        return (self.latest_catalog_date - self.latest_persisted_date).days


def _log_gap_summary(summary: GapSyncSummary, limit: int) -> None:
    """Emite o resumo da execução via ``logging`` (``print`` é proibido)."""
    logger.info(
        "Resumo do fechamento do gap (source=%s): eventos ingeridos=%d; eventos já presentes=%d; "
        "eventos ambíguos (pulados)=%d; lutadores criados=%d; bouts inseridos=%d; "
        "bout_fighters inseridos=%d; rounds inseridos=%d; cantos não-casados=%d; "
        "chamadas de stats=%d; desempates de perfil=%d; "
        "cache hits=%d; chamadas Cito=%d/%d; último persistido=%s; último do catálogo=%s; "
        "defasagem=%s dias",
        summary.source,
        summary.events_ingested,
        summary.events_skipped,
        len(summary.events_ambiguous),
        summary.fighters_created,
        summary.bouts.inserted,
        summary.bout_fighters.inserted,
        summary.rounds_inserted,
        summary.unmatched_stat_lines,
        summary.stats_calls_used,
        summary.profile_calls_used,
        summary.cache_hits,
        summary.cito_calls_used,
        limit,
        summary.latest_persisted_date,
        summary.latest_catalog_date,
        summary.days_behind,
    )


def run_gap_sync(
    session: Session,
    client: CitoClient,
    budget: CallBudget,
    cache: EventStatsCache,
    *,
    today: date,
    date_from: date | None = None,
    catalog_cache: CatalogPageCache | None = None,
    page_limit: int = CATALOG_PAGE_LIMIT,
    max_events: int | None = None,
    min_interval_seconds: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> GapSyncSummary:
    """Fecha o gap: descobre os eventos UFC ausentes e ingere cada um com UMA chamada.

    O catálogo é lido **uma vez**, recortado pela janela do gap (``from``/``to``), o que reduz o
    custo de paginação ao mínimo. Cada evento descoberto é ingerido por ``ingest_gap_event``,
    dentro do seu próprio SAVEPOINT: uma falha propaga (o operador precisa vê-la), mas os
    eventos anteriores permanecem persistidos e o retry custa só o que faltou.

    ``date_from`` sobrepõe o início da janela (default: o último evento persistido). É o que
    permite recuperar um evento **pulado** numa execução anterior: fechado o gap à frente dele,
    ele fica atrás do corte e nenhuma reexecução o reencontraria.

    ``max_events`` limita a execução aos N eventos **mais antigos** do gap -- é a trava da
    sondagem, e o cache resumível faz essas chamadas contarem dentro do lote seguinte. Entre
    fetches **não-cacheados** aplica o rate-limit (``sleeper``, ``0`` nos testes). Opera sobre a
    ``Session`` recebida; o commit é do chamador.
    """
    cutoff = date_from if date_from is not None else latest_persisted_event_date(session)
    catalog = client.fetch_event_catalog(
        limit=page_limit, date_from=cutoff, date_to=today, cache=catalog_cache
    )
    window = gap_window_events(catalog, cutoff=cutoff, today=today)
    selected = select_gap_events(session, catalog, today=today, cutoff=cutoff)
    events_skipped = len(window) - len(selected)
    if max_events is not None:
        # Teto por execução: os N eventos MAIS ANTIGOS do gap (a ordem já é cronológica). É a
        # trava da sondagem -- um erro de laço interrompe cedo em vez de consumir o orçamento.
        selected = selected[:max_events]

    last_index = len(selected) - 1
    events_ingested = 0
    fighters_created = 0
    bouts_inserted = 0
    bouts_reused = 0
    bout_fighters_inserted = 0
    bout_fighters_reused = 0
    rounds_inserted = 0
    unmatched = 0
    profile_calls = 0
    stats_calls = 0
    cache_hits = 0
    ambiguous: list[str] = []
    for index, item in enumerate(selected):
        try:
            result = ingest_gap_event(session, item, client, cache, today=today)
        except GapAmbiguityError as exc:
            # Mesmo tratamento que a Sprint 007-03 deu ao evento ambíguo do catálogo (RF-05): a
            # ambiguidade falha alto DENTRO do evento (o SAVEPOINT já reverteu tudo dele) e aqui
            # é contada e pulada. Derrubar o lote inteiro jogaria fora quota já paga e travaria
            # o fechamento do gap para sempre -- a reexecução pararia no mesmo ponto.
            logger.warning(
                "Evento %r (slug %r) pulado por ambiguidade de entity resolution: %s. Nada foi "
                "gravado para ele; o desempate é decisão humana.",
                item.title,
                item.slug,
                exc,
            )
            ambiguous.append(item.slug)
            stats_calls += exc.stats_calls_used
            profile_calls += exc.profile_calls_used
            continue
        events_ingested += result.event_inserted
        fighters_created += result.fighters_created
        bouts_inserted += result.bouts.inserted
        bouts_reused += result.bouts.updated
        bout_fighters_inserted += result.bout_fighters.inserted
        bout_fighters_reused += result.bout_fighters.updated
        rounds_inserted += result.rounds_inserted
        unmatched += result.unmatched_stat_lines
        profile_calls += result.profile_calls_used
        logger.info(
            "Evento %r (slug %r, %s) ingerido: %d lutas, %d cantos, %d rounds, %d lutadores "
            "criados, %d desempates; cache hit=%s",
            item.title,
            item.slug,
            item.local_date,
            result.bouts.inserted,
            result.bout_fighters.inserted,
            result.rounds_inserted,
            result.fighters_created,
            result.profile_calls_used,
            result.cache_hit,
        )
        if result.cache_hit:
            cache_hits += 1
        else:
            stats_calls += 1
            if index < last_index:
                # Rate-limit só entre fetches reais (nunca após cache hit nem no último evento).
                sleeper(min_interval_seconds)

    realized = [item.local_date for item in catalog if item.status == _COMPLETED]
    summary = GapSyncSummary(
        events_ingested=events_ingested,
        events_skipped=events_skipped,
        events_ambiguous=tuple(ambiguous),
        fighters_created=fighters_created,
        bouts=TableDelta(inserted=bouts_inserted, updated=bouts_reused),
        bout_fighters=TableDelta(inserted=bout_fighters_inserted, updated=bout_fighters_reused),
        rounds_inserted=rounds_inserted,
        unmatched_stat_lines=unmatched,
        profile_calls_used=profile_calls,
        stats_calls_used=stats_calls,
        cache_hits=cache_hits,
        cito_calls_used=budget.used,
        latest_persisted_date=latest_persisted_event_date(session),
        latest_catalog_date=max(realized) if realized else None,
        source=SOURCE,
    )
    _log_gap_summary(summary, budget.limit)
    return summary


# --------------------------------------------------------------------------- #
# CLI: gate humano -> teto -> cliente + cache -> execução -> commit só no sucesso.
# --------------------------------------------------------------------------- #

# Conjunto de fixtures da demonstração (``--fixture``): catálogo e stats no MESMO diretório,
# porque o modo fixture do cliente resolve os dois pelo mesmo ``fixture_dir``. Ambos são
# recortes das capturas reais de 2026-08-31, nunca dado inventado.
_DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "tests" / "ingestion" / "fixtures" / "gap_sync"
)

# Diretório default do cache em disco resumível (padrão do M5).
_DEFAULT_CACHE_DIR = Path(".cache") / "cito"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do fechamento do gap."""
    parser = argparse.ArgumentParser(
        description="Fecha o gap de eventos ausentes ingerindo o catálogo Cito (1 chamada/evento).",
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
        help="Diretório das fixtures de catálogo e de stats (usado apenas com --fixture).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=_DEFAULT_CACHE_DIR,
        help="Diretório do cache em disco resumível das respostas da Cito.",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        type=date.fromisoformat,
        default=None,
        help=(
            "Início da janela do gap (AAAA-MM-DD; default: o último evento persistido). "
            "Serve para reabrir a janela e recuperar evento pulado numa execução anterior."
        ),
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=None,
        help=(
            "Teto de eventos ingeridos nesta execução (os N mais antigos do gap). Usado pela "
            "sondagem; o cache resumível faz essas chamadas contarem no lote seguinte."
        ),
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
        "--min-interval",
        type=float,
        default=0.0,
        help="Intervalo mínimo (segundos) de rate-limit entre eventos não-cacheados.",
    )
    parser.add_argument(
        "--confirmar-gasto-de-quota",
        action="store_true",
        help="Confirmação humana explícita para gastar quota real da Cito (gate da rede real).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint ``python -m ingestion.cito.gap_sync`` (fechamento do gap de eventos).

    Aplica o **gate humano** antes de tudo: em rede real sem ``--confirmar-gasto-de-quota``,
    aborta com ``logger.error`` + ``sys.exit(1)`` sem instanciar o cliente nem tocar a rede.
    Confirmado (ou em modo fixture), resolve o teto, abre a sessão real, roda o fechamento
    (SAVEPOINT por evento) e **commita** só no sucesso. Estourar o teto
    (``QuotaExceededError``) é capturado: o SAVEPOINT já reverteu o evento em curso, mas os
    anteriores desta execução ainda estão na transação -- por isso o commit acontece antes do
    ``sys.exit(1)``, para não jogar fora quota já paga (o cache também os preserva).

    A data corrente vem de ``datetime.now(UTC).date()``: ``date.today()`` é proibido (DTZ011).
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)

    try:
        enforce_human_gate(fixture=args.fixture, confirmed=args.confirmar_gasto_de_quota)
    except HumanGateNotConfirmedError as exc:
        logger.error("Gate humano não confirmado: %s", exc)
        sys.exit(1)

    try:
        limit = resolve_call_budget(args.call_budget, os.environ)
    except ValueError as exc:
        logger.error("Configuração inválida: %s", exc)
        sys.exit(2)

    budget = CallBudget(limit=limit)
    client = build_cito_client(fixture=args.fixture, fixture_dir=args.fixture_dir, budget=budget)
    cache = EventStatsCache(args.cache_dir)
    catalog_cache = CatalogPageCache(args.cache_dir)

    with SessionLocal() as session:
        try:
            run_gap_sync(
                session,
                client,
                budget,
                cache,
                today=datetime.now(UTC).date(),
                date_from=args.date_from,
                catalog_cache=catalog_cache,
                max_events=args.max_events,
                min_interval_seconds=args.min_interval,
            )
        except QuotaExceededError as exc:
            session.commit()  # preserva os eventos completos desta execução (quota já paga)
            logger.error("Fechamento do gap interrompido sem escrita parcial: %s", exc)
            sys.exit(1)
        session.commit()


if __name__ == "__main__":
    main()
