"""Sincronização do catálogo Cito -> ``events.cito_slug``/``cito_event_id`` (M6, Slice 03).

Resolve, para cada evento **já persistido**, qual é o seu identificador na Cito, lendo o
**catálogo real paginado** (``GET /api/v1/ufc/events``) em vez de derivar o slug por regra.
É o que destrava os Fight Nights: o slug deles usa a data **local** do evento e diverge em um
dia da data UTC de ``startsAt`` (2026-08-23 UTC -> 'ufc-fight-night-august-22-2026'), então
nenhuma regex sobre o nome persistido produziria o identificador com segurança (SPEC 007,
RF-04; a ADR 0005 da Slice 04 registra a premissa da ADR 0004 que caiu).

Invariantes
-----------
- **Nenhum slug é derivado.** O slug e o id vêm do item de catálogo; o casamento é por data
  local (janela de +/-1 dia) + nome normalizado (``ingestion.cito.matching``).
- **Ambiguidade falha alto, mas não derruba o run** (RF-05): ``AmbiguousEventMatchError`` é
  capturada **por evento**; o evento entra em ``ambiguous``, **nada é escrito para ele** e o
  loop segue -- mesmo padrão do skip contado do backfill round-a-round.
- **Idempotência**: um campo só é atribuído quando o valor muda, então reexecutar não produz
  UPDATE, não altera contagem e não altera valores.
- **Ausência explícita**: evento sem correspondência permanece com os dois campos nulos.
- **Uma leitura do catálogo por execução**, paginada: uma unidade de ``CallBudget`` por
  página (~17 para os 811 eventos com ``limit=50``). ``--from``/``--to`` reduzem o custo.
- **Gate humano antes da rede real** (``ingestion.cito.gate``): sem
  ``--confirmar-gasto-de-quota``, aborta antes da primeira unidade de quota.
- **Promoções fora do escopo** (DWCS, Road to UFC) são descartadas do catálogo antes do
  casamento, por filtro de exclusão -- nunca por prefixo 'ufc-' no slug.

A lógica testável opera sobre a ``Session`` recebida (transacional nos testes); ``main`` é
fino: gate -> teto -> cliente -> sessão real -> **commit só no sucesso**. Toda a suíte roda em
modo fixture -- zero rede real.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.events.models import Event
from ingestion.cito.cache import CatalogPageCache
from ingestion.cito.client import (
    CATALOG_PAGE_LIMIT,
    CallBudget,
    CitoClient,
    QuotaExceededError,
    build_cito_client,
)
from ingestion.cito.dto import CitoCatalogItem
from ingestion.cito.gate import HumanGateNotConfirmedError, enforce_human_gate
from ingestion.cito.matching import (
    AmbiguousEventMatchError,
    is_ufc_catalog_item,
    resolve_event_match,
)
from ingestion.incremental import resolve_call_budget
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

# Conjunto de fixtures usado pela execução de demonstração (``--fixture``), sem consumir quota.
# Aponta para o conjunto **derivado** de duas páginas (itens reais da captura de 2026-08-31, com
# o ``meta`` ajustado para encerrar): a captura real de ``events_catalog_page_1.json`` é a
# página 1 de 17 e, sozinha, não permite percorrer o catálogo até o fim offline.
_DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "tests"
    / "ingestion"
    / "fixtures"
    / "catalog_paginado_derivado"
)

# Diretório default do cache em disco resumável das páginas do catálogo (padrão do M5).
_DEFAULT_CACHE_DIR = Path(".cache") / "cito"


@dataclass(frozen=True)
class CatalogSyncReport:
    """Cobertura da sincronização: o que casou, **como** casou, e o que não casou.

    ``matched_by_date_only`` é reportado à parte de propósito: são os casamentos que a data
    sustentou sozinha, sem confirmação de nome. É a lista que o humano inspeciona antes de a
    Slice 05 gastar quota em cima desses slugs.
    """

    catalog_items: int
    matched_by_name: int
    matched_by_date_only: tuple[tuple[int, str], ...]
    unmatched: tuple[tuple[int, str], ...]
    ambiguous: tuple[tuple[int, str], ...]
    cito_calls_used: int

    @property
    def matched(self) -> int:
        """Eventos que ganharam identificador (por nome ou só por data)."""
        return self.matched_by_name + len(self.matched_by_date_only)

    @property
    def total(self) -> int:
        """Eventos persistidos considerados na execução."""
        return self.matched + len(self.unmatched) + len(self.ambiguous)

    @property
    def coverage(self) -> float:
        """Fração de eventos casados; base sem evento -> ``0.0`` (sem divisão por zero)."""
        return self.matched / self.total if self.total else 0.0


def _select_events(session: Session, date_from: date | None, date_to: date | None) -> list[Event]:
    """Eventos persistidos da janela, em ordem cronológica estável (``(date, id)``).

    A janela é opcional nas duas pontas e é a **mesma** enviada à Cito, para que os dois lados
    enxerguem o mesmo recorte.
    """
    statement = select(Event).order_by(Event.date, Event.id)
    if date_from is not None:
        statement = statement.where(Event.date >= date_from)
    if date_to is not None:
        statement = statement.where(Event.date <= date_to)
    return list(session.scalars(statement))


def _assign_if_changed(event: Event, item: CitoCatalogItem) -> None:
    """Escreve os identificadores só quando o valor muda -- rerun não produz UPDATE."""
    if event.cito_slug != item.slug:
        event.cito_slug = item.slug
    if event.cito_event_id != item.id:
        event.cito_event_id = item.id


def _log_sync_summary(report: CatalogSyncReport, limit: int) -> None:
    """Emite o relatório de cobertura via ``logging`` (``print`` é proibido)."""
    logger.info(
        "Resumo da sincronização do catálogo Cito: itens de catálogo (UFC)=%d; eventos=%d; "
        "cobertura %d/%d (%.0f%%); casados por nome=%d; casados só por data=%d; não casados=%d; "
        "ambíguos=%d; chamadas Cito=%d/%d",
        report.catalog_items,
        report.total,
        report.matched,
        report.total,
        report.coverage * 100,
        report.matched_by_name,
        len(report.matched_by_date_only),
        len(report.unmatched),
        len(report.ambiguous),
        report.cito_calls_used,
        limit,
    )
    for event_id, name in report.matched_by_date_only:
        logger.warning(
            "Evento %r (id %d) casado SÓ POR DATA (sem concordância de nome): inspecionar antes "
            "de gastar quota no slug resolvido.",
            name,
            event_id,
        )
    for event_id, name in report.ambiguous:
        logger.warning(
            "Evento %r (id %d) pulado por ambiguidade: mais de um item de catálogo na janela e "
            "nenhum desempate por nome; nada foi escrito para ele.",
            name,
            event_id,
        )


def sync_event_catalog(
    session: Session,
    client: CitoClient,
    budget: CallBudget,
    *,
    limit: int = CATALOG_PAGE_LIMIT,
    date_from: date | None = None,
    date_to: date | None = None,
    cache: CatalogPageCache | None = None,
) -> CatalogSyncReport:
    """Resolve ``cito_slug``/``cito_event_id`` dos eventos persistidos a partir do catálogo.

    Uma única leitura do catálogo (paginada) alimenta o casamento de **todos** os eventos;
    ``date_from``/``date_to`` filtram os dois lados (a chamada à Cito e o ``select`` de
    ``events``). Itens de promoções fora do escopo (DWCS, Road to UFC) são descartados antes
    do casamento. Com um ``cache``, as páginas já baixadas vêm do disco -- sem rede e sem
    cobrar quota --, o que torna a leitura do catálogo resumível entre execuções e reaproveitável
    pelas Slices 05 e 06.

    ``AmbiguousEventMatchError`` é capturada **por evento**: o evento entra em ``ambiguous`` e
    o loop segue, sem escrever nada para ele (RF-05 -- a ambiguidade falha alto na função de
    casamento, e aqui é contada e pulada). Não há SAVEPOINT por evento, diferente do backfill:
    a decisão de casar **precede** qualquer escrita, então não existe estado parcial a
    reverter. O commit é do chamador.
    """
    catalog = [
        item
        for item in client.fetch_event_catalog(
            limit=limit, date_from=date_from, date_to=date_to, cache=cache
        )
        if is_ufc_catalog_item(item)
    ]

    matched_by_name = 0
    matched_by_date_only: list[tuple[int, str]] = []
    unmatched: list[tuple[int, str]] = []
    ambiguous: list[tuple[int, str]] = []

    for event in _select_events(session, date_from, date_to):
        try:
            match = resolve_event_match(event, catalog)
        except AmbiguousEventMatchError:
            ambiguous.append((event.id, event.name))
            continue
        if match is None:
            unmatched.append((event.id, event.name))
            continue
        _assign_if_changed(event, match.item)
        if match.matched_by_name:
            matched_by_name += 1
        else:
            matched_by_date_only.append((event.id, event.name))

    report = CatalogSyncReport(
        catalog_items=len(catalog),
        matched_by_name=matched_by_name,
        matched_by_date_only=tuple(matched_by_date_only),
        unmatched=tuple(unmatched),
        ambiguous=tuple(ambiguous),
        cito_calls_used=budget.used,
    )
    _log_sync_summary(report, budget.limit)
    return report


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando da sincronização de catálogo."""
    parser = argparse.ArgumentParser(
        description="Sincroniza o catálogo Cito nos identificadores dos eventos persistidos.",
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
        "--from",
        dest="date_from",
        type=date.fromisoformat,
        default=None,
        help="Início da janela (AAAA-MM-DD); filtra a chamada à Cito e o select de events.",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        type=date.fromisoformat,
        default=None,
        help="Fim da janela (AAAA-MM-DD); filtra a chamada à Cito e o select de events.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=CATALOG_PAGE_LIMIT,
        help=f"Itens por página do catálogo (default: {CATALOG_PAGE_LIMIT}, o máximo da Cito).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=_DEFAULT_CACHE_DIR,
        help="Diretório do cache em disco resumável das páginas do catálogo.",
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
    """Entrypoint ``python -m ingestion.cito.sync_catalog`` (sincronização do catálogo).

    Aplica o **gate humano** antes de tudo: em rede real sem ``--confirmar-gasto-de-quota``,
    aborta com ``logger.error`` + ``sys.exit(1)`` sem instanciar o cliente nem tocar a rede.
    Confirmado (ou em modo fixture), resolve o teto, abre a sessão real, sincroniza e
    **commita** só no sucesso. Estourar o teto (``QuotaExceededError``) é capturado: como
    nenhuma escrita precede a leitura completa do catálogo, o comando encerra com ``sys.exit(1)``
    sem alcançar o commit.
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
    cache = CatalogPageCache(args.cache_dir)

    with SessionLocal() as session:
        try:
            sync_event_catalog(
                session,
                client,
                budget,
                limit=args.limit,
                date_from=args.date_from,
                date_to=args.date_to,
                cache=cache,
            )
        except QuotaExceededError as exc:
            logger.error("Sincronização interrompida sem escrita: %s", exc)
            sys.exit(1)
        session.commit()


if __name__ == "__main__":
    main()
