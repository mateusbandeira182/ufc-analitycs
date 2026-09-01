"""Backfill round-a-round da Cito para ``bout_fighter_rounds`` (M5, Slice 05).

Popula a granularidade por round (``BoutFighterRound``) a partir da Cito, para os eventos
**já persistidos** (seed do Kaggle) da janela do piloto **2023-2025**. É persisted-driven
(mesma âncora da Slice 04): para cada evento da janela, lê o identificador Cito persistido
(``Event.cito_slug``, resolvido contra o catálogo real pela Sprint 007-03), obtém as stats via
cache resumível (``EventStatsCache.get_or_fetch`` sobre ``CitoClient.fetch_event_stats``),
resolve os ``bout_fighter_id`` (``resolve_bout_fighter_ids``, Slice 04) e grava uma linha por
``(bout_fighter_id, round)`` com ``source="cito"``.

Invariantes (reuso dos padrões do M1, ``ingestion.incremental``)
----------------------------------------------------------------
- **Idempotência por chave natural** ``(bout_fighter_id, round)``: os rounds já presentes são
  pulados; rerun devolve 0 inseridos e não altera contagem/conteúdo. Ausência de um split no
  payload vira ``None`` -- nunca zero inventado.
- **SAVEPOINT por evento** (``session.begin_nested``): uma falha no meio de um evento (ambiguidade
  de matching, estouro de quota) reverte só aquele evento, sem parcial -- retry idempotente.
- **Skip do evento sem identificador**: eventos sem ``cito_slug`` persistido (o catálogo não os
  casou) são pulados com ``logger.warning`` e contados em ``events_skipped``, sem abortar o run e
  sem inventar um slug. Ambiguidade e estouro de quota, ao contrário, seguem falhando alto.
- **``CallBudget`` cobrado por fetch não-cacheado**: o cliente cobra a cada ``fetch_event_stats``;
  um cache hit não chama o cliente, logo não cobra. Estourar o teto levanta ``QuotaExceededError``
  antes de gastar.
- **Rate-limit entre eventos não-cacheados**: um sleeper injetável (``0`` nos testes) separa fetches
  sucessivos, poupando a API; cache hit não dorme.
- **Gate humano antes da rede real**: rodar contra a Cito real sem ``--confirmar-gasto-de-quota``
  aborta **antes** de qualquer chamada (``ingestion.cito.gate.enforce_human_gate``, promovido a
  módulo próprio no M6 ao ganhar um segundo callsite); o modo fixture não exige gate.

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from ingestion.cito.cache import EventStatsCache
from ingestion.cito.client import CallBudget, CitoClient, QuotaExceededError
from ingestion.cito.dto import CitoBoutBlock, CitoBoutStatLine, CitoRoundStatLine
from ingestion.cito.gate import HumanGateNotConfirmedError, enforce_human_gate
from ingestion.cito.matching import resolve_bout_fighter_ids
from ingestion.incremental import resolve_call_budget
from mma_analytics.db import SessionLocal
from mma_analytics.settings import settings

logger = logging.getLogger(__name__)

SOURCE = "cito"

# Janela do backfill: 2023-2025, fronteiras inclusivas (115 eventos persistidos, todos com
# ``cito_slug``). Encolheu de 2019-2025 quando o lote virou **piloto medido**: a Sprint 007-01
# mediu o ganho das features de striking do M5 e o bootstrap pareado devolveu IC 95%
# [-0,02301, +0,00377] -- cruza zero, ganho sugestivo e não demonstrado. A regra pré-comprometida
# do PRD disparou, e o round-a-round passa a ser piloto antes do lote.
#
# A janela é **contígua e recente** de propósito: as features de round são médias móveis de 3
# lutas, então só sob cobertura densa um lutador acumula 3 lutas cobertas e a feature deixa de
# ser nula. Uma amostra espalhada mediria disponibilidade de dado, não ganho de modelo.
WINDOW_START = date(2023, 1, 1)
WINDOW_END = date(2025, 12, 31)

# Diretório de fixtures da execução de demonstração (``--fixture``), sem consumir quota.
_DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "tests" / "ingestion" / "fixtures"
)

# Diretório default do cache em disco resumável (relativo ao diretório de execução).
_DEFAULT_CACHE_DIR = Path(".cache") / "cito"


def _select_events_in_window(session: Session) -> list[Event]:
    """Eventos persistidos com ``date`` em [2023-01-01, 2025-12-31], em ordem cronológica estável.

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

    Os nove splits chegam como tuplas ``(landed, attempted)`` já parseadas na borda; um split
    ausente no payload permanece ``None`` (nunca zero inventado). ``totalStrikes`` está entre eles:
    o payload real de 2026-08-31 o traz por round, ao contrário do que o M5 supunha (ADR 0005).
    """
    sig_landed, sig_attempted = line.sig_strikes
    total_landed, total_attempted = line.total_strikes
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


def fill_bout_fighter_totals(session: Session, bout_fighter_id: int, line: CitoBoutStatLine) -> int:
    """Preenche só o que está **nulo** no canto (``reversals``, tempo de controle); devolve quantos.

    Nunca sobrescreve valor já presente -- o seed do Kaggle é a fonte da verdade do que já gravou
    -- e nunca grava ``None`` por cima de valor: ausência no payload permanece ausente, jamais
    zero inventado. Rerun é no-op (devolve 0), o que sustenta a idempotência do backfill.

    ``bout_fighters.source`` permanece ``"kaggle"``: ``source`` é a origem da **linha** (quem a
    criou), não de cada campo. A procedência do enriquecimento fica rastreável nas linhas de
    ``bout_fighter_rounds`` do mesmo canto, que gravam ``source="cito"``. Compor o valor
    (``"kaggle+cito"``) quebraria o backfill de peso da Sprint 007-02, que filtra por
    ``source="kaggle"``.
    """
    bout_fighter = session.get(BoutFighter, bout_fighter_id)
    if bout_fighter is None:
        return 0

    filled = 0
    if bout_fighter.reversals is None and line.reversals is not None:
        bout_fighter.reversals = line.reversals
        filled += 1
    if bout_fighter.control_time_seconds is None and line.control_time_seconds is not None:
        bout_fighter.control_time_seconds = line.control_time_seconds
        filled += 1
    return filled


def _bout_ids_by_cito_bout(
    session: Session, bf_ids: Mapping[tuple[str, str], int]
) -> dict[str, int]:
    """Mapeia ``cito_bout_id -> bouts.id`` a partir dos ``bout_fighter`` já casados (1 select).

    Reaproveita o matching de ``resolve_bout_fighter_ids`` -- sem N+1 e sem um segundo critério
    de casamento, que poderia discordar do primeiro.

    Quando os dois cantos de uma mesma luta da Cito apontam para ``bouts`` **diferentes** (card
    corrigido entre as fontes), a luta é descartada com aviso em vez de escolher um lado: gravar
    contexto na luta errada seria corrupção silenciosa, e ausência é sempre preferível a chute.
    """
    bout_ids_por_cito: dict[str, set[int]] = {}
    bout_id_por_bf: dict[int, int] = dict(
        session.execute(
            select(BoutFighter.id, BoutFighter.bout_id).where(
                BoutFighter.id.in_(set(bf_ids.values()))
            )
        )
        .tuples()
        .all()
    )
    for (cito_bout_id, _fighter_slug), bout_fighter_id in bf_ids.items():
        bout_id = bout_id_por_bf.get(bout_fighter_id)
        if bout_id is not None:
            bout_ids_por_cito.setdefault(cito_bout_id, set()).add(bout_id)

    resolved: dict[str, int] = {}
    for cito_bout_id, candidatos in bout_ids_por_cito.items():
        if len(candidatos) > 1:
            logger.warning(
                "Luta %r da Cito aponta para %d bouts persistidos distintos (%s); contexto de "
                "card não gravado -- nunca escolher um lado em silêncio.",
                cito_bout_id,
                len(candidatos),
                sorted(candidatos),
            )
            continue
        resolved[cito_bout_id] = next(iter(candidatos))
    return resolved


def fill_bout_context(session: Session, bout_id: int, line: CitoBoutBlock) -> int:
    """Preenche só o que está **nulo** em ``bouts`` (weight_class, card_section, bout_order).

    ``method`` e ``winner_id`` **não** são tocados: o desfecho é do seed, e trocar a fonte da
    verdade do resultado no meio de um backfill de enriquecimento mudaria em silêncio o rótulo
    que o modelo treina. Mesma política das demais colunas -- nunca sobrescrever valor presente,
    nunca gravar ``None`` por cima de valor, ausência permanece ausente. Rerun é no-op.

    ``card_section`` recebe o rótulo **cru** da Cito ('Main Card'/'Prelims'); ``bout_order`` é
    ordenável mas não sequencial nem denso (1001 na principal do card sondado).

    **``bout_order`` é o porteiro dos dois campos de card**: se ele vier nulo no payload, nem ele
    nem ``card_section`` são gravados. A sondagem de 2026-09-01 mediu por quê -- nos eventos de
    2023 a Cito devolve ``cardSection: "Main Card"`` para **todas** as lutas (as 15 de UFC 283
    inclusive, que obviamente não são todas card principal), enquanto ``boutOrder`` vem nulo. O
    rótulo ali é um **default não-discriminante**, não um dado: persisti-lo verbatim gravaria
    dado **falso**, pior que ausência, porque uma feature ou tela construída depois herdaria o
    erro com aparência de sinal. Os dois campos viajam juntos no enriquecimento da Cito, então a
    presença de ``boutOrder`` é o sinal confiável de que o enriquecimento existe para o evento.

    ``weight_class`` não passa por esse porteiro: é dado real do card, independente do
    enriquecimento, e veio correta em toda a janela sondada.
    """
    bout = session.get(Bout, bout_id)
    if bout is None:
        return 0

    filled = 0
    if bout.weight_class is None and line.weight_class is not None:
        bout.weight_class = line.weight_class
        filled += 1
    if line.bout_order is None:
        return filled
    if bout.card_section is None and line.card_section is not None:
        bout.card_section = line.card_section
        filled += 1
    if bout.bout_order is None:
        bout.bout_order = line.bout_order
        filled += 1
    return filled


@dataclass(frozen=True)
class BackfillRoundsSummary:
    """Resumo observável do backfill: eventos, rounds, quota gasta e **cobertura** round-a-round.

    A cobertura (``bouts_total``/``bouts_with_rounds``) é medida **sobre o payload**, não sobre o
    que foi gravado: ela responde a pergunta do piloto -- a Cito expõe round-a-round para estes
    eventos? --, independente de quantos cantos casaram com a base persistida. Os não-casados são
    contados à parte (``unmatched_stat_lines``).
    """

    events_processed: int
    events_skipped: int
    events_without_round_stats: int  # eventos processados cujo ``roundStats`` veio vazio
    rounds_inserted: int
    cache_hits: int
    cito_calls_used: int
    bouts_total: int  # lutas distintas vistas em ``boutStats`` (denominador da cobertura)
    bouts_with_rounds: int  # lutas distintas presentes em ``roundStats``
    unmatched_stat_lines: int  # cantos do payload sem ``bout_fighter`` persistido
    bout_fighter_fields_filled: int  # ``reversals``/tempo de controle preenchidos onde eram nulos
    bout_context_fields_filled: int  # peso/seção/posição de card preenchidos onde eram nulos
    source: str

    @property
    def round_coverage(self) -> float:
        """Fração de lutas com round-a-round; sem lutas -> ``0.0`` (sem divisão por zero)."""
        return self.bouts_with_rounds / self.bouts_total if self.bouts_total else 0.0


def _log_backfill_summary(summary: BackfillRoundsSummary, limit: int) -> None:
    """Emite o resumo do backfill via ``logging`` (``print`` é proibido)."""
    logger.info(
        "Resumo do backfill round-a-round (source=%s): eventos processados=%d; eventos pulados=%d; "
        "eventos sem roundStats=%d; rounds inseridos=%d; cache hits=%d; chamadas Cito=%d/%d; "
        "cobertura round-a-round agregada=%d/%d (%.0f%%); cantos não-casados=%d; "
        "campos de canto preenchidos=%d; campos de contexto de card preenchidos=%d",
        summary.source,
        summary.events_processed,
        summary.events_skipped,
        summary.events_without_round_stats,
        summary.rounds_inserted,
        summary.cache_hits,
        summary.cito_calls_used,
        limit,
        summary.bouts_with_rounds,
        summary.bouts_total,
        summary.round_coverage * 100,
        summary.unmatched_stat_lines,
        summary.bout_fighter_fields_filled,
        summary.bout_context_fields_filled,
    )


def _log_event_progress(
    *,
    event: Event,
    slug: str,
    bouts_total: int,
    bouts_with_rounds: int,
    rounds_inserted: int,
    unmatched: int,
    cache_hit: bool,
) -> None:
    """Emite uma linha de log por evento processado, com a cobertura observada naquele evento.

    É o que torna o piloto auditável evento a evento: o resumo agregado esconde qual card veio
    sem round-a-round, e a decisão de seguir (ou parar) depende dessa distribuição.
    """
    logger.info(
        "Evento %r (id %d, slug %r): cobertura round-a-round %d/%d; rounds inseridos=%d; "
        "cantos não-casados=%d; cache hit=%s",
        event.name,
        event.id,
        slug,
        bouts_with_rounds,
        bouts_total,
        rounds_inserted,
        unmatched,
        cache_hit,
    )


def run_backfill_rounds(
    session: Session,
    client: CitoClient,
    budget: CallBudget,
    cache: EventStatsCache,
    *,
    max_events: int | None = None,
    min_interval_seconds: float = 0.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> BackfillRoundsSummary:
    """Popula ``bout_fighter_rounds`` para os eventos da janela 2023-2025; devolve o resumo.

    Para cada evento da janela, dentro de um **SAVEPOINT** (``session.begin_nested``): obtém as
    stats via ``cache.get_or_fetch`` (cache hit = 0 quota), resolve os ``bout_fighter_id`` (Slice
    04) e, com o payload de **uma única chamada**, grava três coisas -- os rounds
    (``upsert_bout_fighter_rounds``), os totais faltantes do canto (``fill_bout_fighter_totals``)
    e o contexto de card da luta (``fill_bout_context``). Uma falha no meio de um evento reverte
    só aquele evento e propaga (retry idempotente, sem parcial). Entre eventos **não-cacheados**
    aplica o rate-limit (``sleeper``). Opera sobre a ``Session`` recebida; o commit é do chamador.

    ``max_events`` limita a execução aos N eventos **mais antigos** da janela; com o mesmo
    diretório de cache, uma execução posterior sem o teto reaproveita esses N por cache hit e não
    re-gasta quota -- é o que faz a sondagem contar dentro do orçamento do lote.

    Eventos sem ``cito_slug`` persistido (o catálogo não os casou) são **pulados** com
    ``logger.warning`` e contados em ``events_skipped``, **antes** do SAVEPOINT: o loop segue para
    o próximo evento, sem inventar identificador nem abortar o run. Uma ambiguidade de matching ou
    estouro de quota, ao contrário, continua **falhando alto** (invariante de entity resolution /
    gate de quota).
    """
    events = _select_events_in_window(session)
    if max_events is not None:
        # Teto por execução: fatia os N eventos MAIS ANTIGOS da janela (a ordem já é cronológica).
        # É a trava da sondagem -- um erro de laço interrompe cedo em vez de consumir o orçamento
        # inteiro do piloto. O cache resumível faz as chamadas da sondagem contarem dentro do lote.
        events = events[:max_events]
    last_index = len(events) - 1
    rounds_inserted = 0
    cache_hits = 0
    events_skipped = 0
    events_without_round_stats = 0
    bouts_total = 0
    bouts_with_rounds = 0
    unmatched_stat_lines = 0
    bout_fighter_fields_filled = 0
    bout_context_fields_filled = 0
    for index, event in enumerate(events):
        slug = event.cito_slug
        if slug is None:
            logger.warning(
                "Evento %r (id %d) pulado: sem cito_slug persistido; rode a sincronização de "
                "catálogo antes do backfill.",
                event.name,
                event.id,
            )
            events_skipped += 1
            continue

        event_rounds_inserted = 0
        event_unmatched = 0
        with session.begin_nested():
            stats, cache_hit = cache.get_or_fetch(slug, client.fetch_event_stats)
            # Cobertura medida sobre o PAYLOAD: lutas com box-score (denominador) contra lutas
            # com round-a-round (numerador). Não depende do matching -- é o achado do piloto
            # sobre o que a Cito expõe, e a ausência é resultado legítimo, nunca fabricação.
            event_bouts_total = len({line.bout_id for line in stats.bout_stats})
            event_bouts_with_rounds = len({line.bout_id for line in stats.round_stats})
            bf_ids = resolve_bout_fighter_ids(session, event, stats)

            # Totais por canto: preenche só ``reversals``/tempo de controle nulos. Um canto sem
            # ``bout_fighter`` persistido é contado como lacuna e nunca fabricado (CA-08); a
            # ambiguidade, essa sim, já falhou alto dentro de ``resolve_bout_fighter_ids``.
            for stat_line in stats.bout_stats:
                bout_fighter_id = bf_ids.get((stat_line.bout_id, stat_line.fighter_slug))
                if bout_fighter_id is None:
                    event_unmatched += 1
                    continue
                bout_fighter_fields_filled += fill_bout_fighter_totals(
                    session, bout_fighter_id, stat_line
                )

            # Contexto de card: uma vez por luta casada, a partir do mesmo payload já pago.
            bout_id_por_cito = _bout_ids_by_cito_bout(session, bf_ids)
            for bout_block in stats.bouts:
                bout_id = bout_id_por_cito.get(bout_block.id)
                if bout_id is not None:
                    bout_context_fields_filled += fill_bout_context(session, bout_id, bout_block)

            lines_by_bf: dict[int, list[CitoRoundStatLine]] = {}
            for line in stats.round_stats:
                bout_fighter_id = bf_ids.get((line.bout_id, line.fighter_slug))
                if bout_fighter_id is None:
                    # Canto sem correspondência persistida: reportado por quem casa, nunca grava.
                    continue
                lines_by_bf.setdefault(bout_fighter_id, []).append(line)
            for bout_fighter_id, lines in lines_by_bf.items():
                event_rounds_inserted += upsert_bout_fighter_rounds(session, bout_fighter_id, lines)

        rounds_inserted += event_rounds_inserted
        unmatched_stat_lines += event_unmatched
        bouts_total += event_bouts_total
        bouts_with_rounds += event_bouts_with_rounds
        if not event_bouts_with_rounds:
            events_without_round_stats += 1
        _log_event_progress(
            event=event,
            slug=slug,
            bouts_total=event_bouts_total,
            bouts_with_rounds=event_bouts_with_rounds,
            rounds_inserted=event_rounds_inserted,
            unmatched=event_unmatched,
            cache_hit=cache_hit,
        )

        if cache_hit:
            cache_hits += 1
        elif index < last_index:
            # Rate-limit só entre fetches reais (não após cache hit nem após o último evento).
            sleeper(min_interval_seconds)

    summary = BackfillRoundsSummary(
        events_processed=len(events) - events_skipped,
        events_skipped=events_skipped,
        events_without_round_stats=events_without_round_stats,
        rounds_inserted=rounds_inserted,
        cache_hits=cache_hits,
        cito_calls_used=budget.used,
        bouts_total=bouts_total,
        bouts_with_rounds=bouts_with_rounds,
        unmatched_stat_lines=unmatched_stat_lines,
        bout_fighter_fields_filled=bout_fighter_fields_filled,
        bout_context_fields_filled=bout_context_fields_filled,
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
        description="Backfill round-a-round da Cito para bout_fighter_rounds (janela 2023-2025).",
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
        "--max-events",
        type=int,
        default=None,
        help=(
            "Teto de eventos processados nesta execução (os N mais antigos da janela). "
            "Usado pela sondagem; o cache resumível faz essas chamadas contarem no lote."
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
    client = _build_client(fixture=args.fixture, fixture_dir=args.fixture_dir, budget=budget)
    cache = EventStatsCache(args.cache_dir)

    with SessionLocal() as session:
        try:
            run_backfill_rounds(
                session,
                client,
                budget,
                cache,
                max_events=args.max_events,
                min_interval_seconds=args.min_interval,
            )
        except QuotaExceededError as exc:
            logger.error("Backfill interrompido sem escrita parcial: %s", exc)
            sys.exit(1)
        session.commit()


if __name__ == "__main__":
    main()
