"""Backfill round-a-round da Cito para ``bout_fighter_rounds`` (M5, Slice 05).

Popula a granularidade por round (``BoutFighterRound``) a partir da Cito, para os eventos
**já persistidos** (seed do Kaggle) da janela fixa **2019-2025**. É persisted-driven (mesma âncora
da Slice 04): para cada evento da janela, deriva o slug Cito (``event_cito_slug``), obtém as stats
via cache resumível (``EventStatsCache.get_or_fetch`` sobre ``CitoClient.fetch_event_stats``),
resolve os ``bout_fighter_id`` (``resolve_bout_fighter_ids``, Slice 04) e grava uma linha por
``(bout_fighter_id, round)`` com ``source="cito"``.

Invariantes (reuso dos padrões do M1, ``ingestion.incremental``)
----------------------------------------------------------------
- **Idempotência por chave natural** ``(bout_fighter_id, round)``: os rounds já presentes são
  pulados; rerun devolve 0 inseridos e não altera contagem/conteúdo. Ausência de um split no
  payload vira ``None`` -- nunca zero inventado. A Cito não expõe total de golpes por round, então
  ``total_strikes_*`` fica ``None``.
- **SAVEPOINT por evento** (``session.begin_nested``): uma falha no meio de um evento (ambiguidade
  de matching, estouro de quota) reverte só aquele evento, sem parcial -- retry idempotente.
- **Skip do slug não-derivável**: eventos cujo ``name`` não deriva slug Cito
  (``UnsupportedEventSlugError`` -- formatos não-numerados como 'UFC Fight Night: ...') são pulados
  com ``logger.warning`` e contados em ``events_skipped``, sem abortar o run nem casar em silêncio
  (sem heurística silenciosa). Ambiguidade e estouro de quota, ao contrário, seguem falhando alto.
- **``CallBudget`` cobrado por fetch não-cacheado**: o cliente cobra a cada ``fetch_event_stats``;
  um cache hit não chama o cliente, logo não cobra. Estourar o teto levanta ``QuotaExceededError``
  antes de gastar.
- **Rate-limit entre eventos não-cacheados**: um sleeper injetável (``0`` nos testes) separa fetches
  sucessivos, poupando a API; cache hit não dorme.
- **Gate humano antes da rede real**: rodar contra a Cito real sem ``--confirmar-gasto-de-quota``
  aborta **antes** de qualquer chamada (``_enforce_human_gate``); o modo fixture não exige gate.

A lógica testável opera sobre a ``Session`` recebida (transacional nos testes); ``main`` é fino:
resolve o teto, aplica o gate, abre a sessão real e **commita** só no sucesso. Toda a suíte roda em
modo fixture -- zero rede real.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.models import BoutFighterRound
from apps.events.models import Event
from ingestion.cito.cache import EventStatsCache
from ingestion.cito.client import CallBudget, CitoClient, QuotaExceededError
from ingestion.cito.dto import CitoRoundStatLine
from ingestion.cito.matching import (
    UnsupportedEventSlugError,
    event_cito_slug,
    resolve_bout_fighter_ids,
)
from ingestion.incremental import resolve_call_budget
from mma_analytics.db import SessionLocal
from mma_analytics.settings import settings

logger = logging.getLogger(__name__)

SOURCE = "cito"

# Janela fixa do backfill (decisão #2 da SPEC): 2019-2025, fronteiras inclusivas, exclui 2026.
WINDOW_START = date(2019, 1, 1)
WINDOW_END = date(2025, 12, 31)

# Diretório de fixtures da execução de demonstração (``--fixture``), sem consumir quota.
_DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "tests" / "ingestion" / "fixtures"
)

# Diretório default do cache em disco resumável (relativo ao diretório de execução).
_DEFAULT_CACHE_DIR = Path(".cache") / "cito"


class HumanGateNotConfirmedError(RuntimeError):
    """Backfill contra a rede real exigido sem a confirmação humana explícita do gasto de quota."""


def _enforce_human_gate(*, fixture: bool, confirmed: bool) -> None:
    """Modo rede real sem ``--confirmar-gasto-de-quota`` aborta ANTES de qualquer fetch.

    O modo fixture (JSON local, 0 quota) não exige gate. Contra a Cito real, a confirmação
    explícita é obrigatória: sem ela, levanta ``HumanGateNotConfirmedError`` antes de instanciar o
    cliente ou tocar a rede.
    """
    if not fixture and not confirmed:
        raise HumanGateNotConfirmedError(
            "Backfill contra a Cito real exige --confirmar-gasto-de-quota; "
            "nenhuma chamada foi disparada."
        )


def _select_events_in_window(session: Session) -> list[Event]:
    """Eventos persistidos com ``date`` em [2019-01-01, 2025-12-31], em ordem cronológica estável.

    A ordem por ``(date, id)`` torna o backfill determinístico (processa os eventos em ordem de
    calendário), o que o rate-limit e o cache resumível assumem.
    """
    return list(
        session.scalars(
            select(Event)
            .where(Event.date.between(WINDOW_START, WINDOW_END))
            .order_by(Event.date, Event.id)
        )
    )


def _build_round(bout_fighter_id: int, line: CitoRoundStatLine) -> BoutFighterRound:
    """Mapeia uma linha ``roundStats`` da Cito no ``BoutFighterRound`` (1:1, ausência -> None).

    A Cito não expõe total de golpes por round -- ``total_strikes_*`` fica ``None`` (nunca zero
    inventado). Os oito splits chegam como tuplas ``(landed, attempted)`` já parseadas na borda.
    """
    sig_landed, sig_attempted = line.sig_strikes
    head_landed, head_attempted = line.head
    body_landed, body_attempted = line.body
    leg_landed, leg_attempted = line.leg
    distance_landed, distance_attempted = line.distance
    clinch_landed, clinch_attempted = line.clinch
    ground_landed, ground_attempted = line.ground
    takedowns_landed, takedowns_attempted = line.takedowns
    return BoutFighterRound(
        bout_fighter_id=bout_fighter_id,
        round=line.round,
        knockdowns=line.knockdowns,
        sig_strikes_landed=sig_landed,
        sig_strikes_attempted=sig_attempted,
        takedowns_landed=takedowns_landed,
        takedowns_attempted=takedowns_attempted,
        submission_attempts=line.submission_attempts,
        control_time_seconds=line.control_time_seconds,
        total_strikes_landed=None,
        total_strikes_attempted=None,
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


def upsert_bout_fighter_rounds(
    session: Session,
    bout_fighter_id: int,
    round_lines: Sequence[CitoRoundStatLine],
) -> int:
    """Get-or-create long por ``(bout_fighter_id, round)``; ``source="cito"``; devolve inseridos.

    Idempotente: os ``round`` já presentes para o ``bout_fighter_id`` são pulados (a unicidade
    ``uq_bout_fighter_round`` sustenta a chave no banco). Nunca pré-agrega -- cada round guarda os
    próprios números granulares. Espelha ``ingestion.incremental.upsert_bout_fighters`` (materializa
    o conjunto de chaves já presentes e pula as existentes); a rule of three ainda não foi atingida,
    então a materialização de chave não é extraída para helper compartilhado.
    """
    existing: set[int] = {
        existing_round
        for (existing_round,) in session.execute(
            select(BoutFighterRound.round).where(
                BoutFighterRound.bout_fighter_id == bout_fighter_id
            )
        )
    }
    inserted = 0
    for line in round_lines:
        if line.round in existing:
            continue
        session.add(_build_round(bout_fighter_id, line))
        existing.add(line.round)
        inserted += 1
    return inserted


@dataclass(frozen=True)
class BackfillRoundsSummary:
    """Resumo observável do backfill: eventos processados/pulados, rounds, hits e quota gasta."""

    events_processed: int
    events_skipped: int
    rounds_inserted: int
    cache_hits: int
    cito_calls_used: int
    source: str


def _log_backfill_summary(summary: BackfillRoundsSummary, limit: int) -> None:
    """Emite o resumo do backfill via ``logging`` (``print`` é proibido)."""
    logger.info(
        "Resumo do backfill round-a-round (source=%s): eventos processados=%d; eventos pulados=%d; "
        "rounds inseridos=%d; cache hits=%d; chamadas Cito=%d/%d",
        summary.source,
        summary.events_processed,
        summary.events_skipped,
        summary.rounds_inserted,
        summary.cache_hits,
        summary.cito_calls_used,
        limit,
    )


def run_backfill_rounds(
    session: Session,
    client: CitoClient,
    budget: CallBudget,
    cache: EventStatsCache,
    *,
    min_interval_seconds: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> BackfillRoundsSummary:
    """Popula ``bout_fighter_rounds`` para os eventos da janela 2019-2025; devolve o resumo.

    Para cada evento da janela, dentro de um **SAVEPOINT** (``session.begin_nested``): obtém as
    stats via ``cache.get_or_fetch`` (cache hit = 0 quota), resolve os ``bout_fighter_id`` (Slice
    04) e grava os rounds via ``upsert_bout_fighter_rounds``. Uma falha no meio de um evento reverte
    só aquele evento e propaga (retry idempotente, sem parcial). Entre eventos **não-cacheados**
    aplica o rate-limit (``sleeper``). Opera sobre a ``Session`` recebida; o commit é do chamador.

    Eventos cujo ``name`` não deriva slug Cito (``UnsupportedEventSlugError`` -- formatos
    não-numerados, ex.: 'UFC Fight Night: ...') são **pulados** com ``logger.warning`` e contados
    em ``events_skipped``, **antes** do SAVEPOINT: o loop segue para o próximo evento, sem casar em
    silêncio nem abortar o run. Uma ambiguidade de matching ou estouro de quota, ao contrário,
    continua **falhando alto** (invariante de entity resolution / gate de quota).
    """
    events = _select_events_in_window(session)
    last_index = len(events) - 1
    rounds_inserted = 0
    cache_hits = 0
    events_skipped = 0
    for index, event in enumerate(events):
        try:
            slug = event_cito_slug(event)
        except UnsupportedEventSlugError:
            logger.warning(
                "Evento %r (id %d) pulado: slug Cito não derivável do nome (formato não-numerado); "
                "sem heurística silenciosa, o backfill segue para o próximo evento.",
                event.name,
                event.id,
            )
            events_skipped += 1
            continue

        with session.begin_nested():
            stats, cache_hit = cache.get_or_fetch(slug, client.fetch_event_stats)
            bf_ids = resolve_bout_fighter_ids(session, event, stats)
            lines_by_bf: dict[int, list[CitoRoundStatLine]] = {}
            for line in stats.round_stats:
                bout_fighter_id = bf_ids.get((line.bout_id, line.corner))
                if bout_fighter_id is None:
                    # Canto sem correspondência persistida: reportado por quem casa, nunca grava.
                    continue
                lines_by_bf.setdefault(bout_fighter_id, []).append(line)
            for bout_fighter_id, lines in lines_by_bf.items():
                rounds_inserted += upsert_bout_fighter_rounds(session, bout_fighter_id, lines)

        if cache_hit:
            cache_hits += 1
        elif index < last_index:
            # Rate-limit só entre fetches reais (não após cache hit nem após o último evento).
            sleeper(min_interval_seconds)

    summary = BackfillRoundsSummary(
        events_processed=len(events) - events_skipped,
        events_skipped=events_skipped,
        rounds_inserted=rounds_inserted,
        cache_hits=cache_hits,
        cito_calls_used=budget.used,
        source=SOURCE,
    )
    _log_backfill_summary(summary, budget.limit)
    return summary


def _build_client(*, fixture: bool, fixture_dir: Path, budget: CallBudget) -> CitoClient:
    """Constrói o ``CitoClient`` do backfill: modo fixture (0 quota real) ou HTTP autenticado."""
    if fixture:
        return CitoClient(
            token=settings.cito_api_token,
            base_url=settings.cito_base_url,
            fixture_dir=fixture_dir,
            budget=budget,
        )
    return CitoClient(token=settings.cito_api_token, base_url=settings.cito_base_url, budget=budget)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do backfill."""
    parser = argparse.ArgumentParser(
        description="Backfill round-a-round da Cito para bout_fighter_rounds (janela 2019-2025).",
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
        help="Diretório das fixtures de stats de evento (usado apenas com --fixture).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=_DEFAULT_CACHE_DIR,
        help="Diretório do cache em disco resumável das respostas da Cito.",
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
    """Entrypoint ``python -m ingestion.cito.backfill_rounds`` (backfill round-a-round da Cito).

    Aplica o **gate humano** antes de tudo: em rede real sem ``--confirmar-gasto-de-quota``, aborta
    com ``logger.error`` + ``sys.exit(1)`` sem instanciar o cliente nem tocar a rede. Confirmado (ou
    em modo fixture), resolve o teto, abre a sessão real, roda o backfill (SAVEPOINT por evento) e
    **commita** só no sucesso. Estourar o teto (``QuotaExceededError``) é capturado: o SAVEPOINT já
    reverteu o evento (zero parcial), então ``main`` emite a mensagem clara e encerra com
    ``sys.exit(1)``, sem alcançar o commit.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)

    try:
        _enforce_human_gate(fixture=args.fixture, confirmed=args.confirmar_gasto_de_quota)
    except HumanGateNotConfirmedError as exc:
        logger.error("Gate humano não confirmado: %s", exc)
        sys.exit(1)

    try:
        limit = resolve_call_budget(args.call_budget, os.environ)
    except ValueError as exc:
        logger.error("Configuração inválida: %s", exc)
        sys.exit(2)

    budget = CallBudget(limit=limit)
    client = _build_client(fixture=args.fixture, fixture_dir=args.fixture_dir, budget=budget)
    cache = EventStatsCache(args.cache_dir)

    with SessionLocal() as session:
        try:
            run_backfill_rounds(
                session, client, budget, cache, min_interval_seconds=args.min_interval
            )
        except QuotaExceededError as exc:
            logger.error("Backfill interrompido sem escrita parcial: %s", exc)
            sys.exit(1)
        session.commit()


if __name__ == "__main__":
    main()
