"""Imagens do lutador vindas da Cito -> ``fighters.headshot_url``/``body_image_url``.

M7 (SPEC 008), Slice 06. A fonte é a **Cito**, não a API oficial da UFC: uma varredura de 50
eventos da fonte oficial por ``image``/``photo``/``headshot``/``thumbnail``/``.png``/``.jpg``
não achou nenhuma imagem (só ``UFCLink``, que é link de página). A Cito, ao contrário, embute
no card duas variantes por atleta, e o catálogo com ``includeBouts`` entrega 25 eventos por
chamada -- contra uma chamada por lutador no endpoint de perfil.

As duas variantes, e a que fica de fora
---------------------------------------
- ``profile.headshotUrl``  -> estilo ``event_results_athlete_headshot`` (retrato).
- ``profile.bodyImageUrl`` -> estilo ``athlete_bio_full_body`` (corpo inteiro).
- ``fighters[].imageUrl``  -> estilo ``event_fight_card_upper_body_of_standing_athlete``: a
  **arte do card**. Fica de fora, e não por preciosismo: ela é por luta, muda a cada evento, e
  é do sufixo ``_L_``/``_R_`` do nome do arquivo dela que o M6 recupera o canto REAL
  (``ingestion.cito.gap_sync.art_side``). O ``athlete_bio_full_body`` **também** carrega esse
  sufixo -- e carrega ``_L_`` nos **dois** cantos da mesma luta (medido no payload real), então
  quem tentasse "ganhar cobertura" apontando ``art_side`` para ele corromperia o canto, que é o
  alvo do modelo, em silêncio.

Regras de escrita
-----------------
- **Nunca INSERT**: lutador do payload sem correspondência na base é pulado e contado. Criar
  lutador é responsabilidade do ``gap_sync``, não desta slice.
- **``None`` nunca apaga**: ausência no payload jamais sobrescreve URL já persistida.
- **Idempotente**: só escreve o que muda, então reexecutar não produz UPDATE nem gasta quota
  (as páginas de catálogo vêm do cache em disco).
- **Janela** (RF-03/CA-08 da SPEC): nada anterior a ``OFFICIAL_WINDOW_START`` (2010-03-21) é
  tocado, em nenhuma tabela. A constante é **importada** de ``ingestion.ufc_official``, nunca
  redeclarada: duas definições da mesma fronteira são o começo de duas fronteiras.
- **``source`` não muda**: ``source`` é a origem da **linha** (CLAUDE.md), então um backfill da
  Cito sobre uma linha semeada do Kaggle mantém ``source="kaggle"``. Não compor valores.
- **Gate humano de quota**: execução contra a Cito real sem ``--confirmar-gasto-de-quota``
  aborta antes da primeira chamada (``ingestion.cito.gate``).

A resolução ``fighter_slug -> fighters.id`` é **persisted-driven escopada ao evento** (roster do
evento + nome normalizado), o mecanismo firmado pela ADR 0005. O ``ufc_fighter_id`` da Slice 03
**não** serve aqui: o payload da Cito não traz o ``FighterId`` da fonte oficial -- traz
``fighterSlug``, e o perfil expõe ``ufcStatsId``, que é identificador do ufcstats.com, grandeza
diferente.
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

from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
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
from ingestion.cito.matching import _slug_to_normalized_name, is_ufc_catalog_item
from ingestion.incremental import resolve_call_budget
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

# Conjunto de fixtures da execução de demonstração (``--fixture``), sem consumir quota: a
# página derivada com card, montada a partir da captura crua já paga.
_DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "tests"
    / "ingestion"
    / "fixtures"
    / "catalog_imagens_derivado"
)

# Diretório default do cache em disco resumável das páginas do catálogo (padrão do M5/M6).
_DEFAULT_CACHE_DIR = Path(".cache") / "cito"


@dataclass(frozen=True)
class FighterImages:
    """As duas variantes de imagem de um lutador; ausência é ``None``, nunca string vazia."""

    headshot_url: str | None
    body_image_url: str | None

    @property
    def is_empty(self) -> bool:
        """``True`` quando o payload não trouxe imagem nenhuma -- nada a gravar."""
        return self.headshot_url is None and self.body_image_url is None


# Estilo da **arte do card** no ufc.com. Não é atributo de lutador (a SPEC 008 a coloca
# explicitamente fora do escopo da Slice 06): é por luta, muda a cada evento, e é dela que o M6
# recupera o canto REAL.
#
# O filtro existe porque ler o campo certo NÃO basta: medido na passada 1 do backfill
# (2026-09-01, custo zero), de 600 lutadores com imagem, **18** trazem essa arte dentro do
# ``profile.bodyImageUrl`` -- o campo que promete ``athlete_bio_full_body``. Sem o filtro,
# gravaríamos em ``fighters`` justamente o que a SPEC excluiu, e ainda por cima uma URL que
# carrega o sufixo ``_L_``/``_R_`` de uma luta específica.
#
# A rejeição é só desta arte, não de "todo estilo inesperado": a mesma medição achou um retrato
# publicado no estilo ``teaser``, que é imagem do atleta e entra normalmente.
_ARTE_DE_CARD = "event_fight_card_upper_body_of_standing_athlete"


def _sem_arte_de_card(url: str | None) -> str | None:
    """Devolve a URL, ou ``None`` quando ela é arte do card (nunca atributo de lutador)."""
    if url is not None and _ARTE_DE_CARD in url:
        return None
    return url


def extract_fighter_images(item: CitoCatalogItem) -> dict[str, FighterImages]:
    """``{fighter_slug: FighterImages}`` do card de um item de catálogo (``includeBouts``).

    Lê EXCLUSIVAMENTE ``bouts[].fighters[].profile.headshot_url`` e ``.body_image_url``.
    NUNCA ``fighters[].image_url`` (arte do card -- é dela que o M6 tira o canto real) e
    NUNCA ``profile.image_url`` (rejeitado no M6, e sem contrato). Slug repetido em mais de
    uma luta do mesmo item: a primeira ocorrência não-vazia vence; nada é composto.

    Além de ler só os campos certos, **verifica o conteúdo**: uma arte de card publicada dentro
    do perfil é descartada (``_sem_arte_de_card``), porque o campo certo às vezes carrega o
    conteúdo errado -- medido em 18 dos 600 lutadores da passada 1.

    Função pura: sem I/O, sem sessão. Canto sem ``profile``, ou com ``profile`` sem nenhuma das
    duas URLs aproveitáveis, não gera entrada -- uma entrada só de nulos induziria o chamador a
    "atualizar" com ausência.
    """
    imagens: dict[str, FighterImages] = {}
    for bout in item.bouts:
        for canto in bout.fighters:
            if canto.profile is None or canto.fighter_slug in imagens:
                continue
            imagem = FighterImages(
                headshot_url=_sem_arte_de_card(canto.profile.headshot_url),
                body_image_url=_sem_arte_de_card(canto.profile.body_image_url),
            )
            if not imagem.is_empty:
                imagens[canto.fighter_slug] = imagem
    return imagens


@dataclass(frozen=True)
class ImageBackfillReport:
    """Cobertura do backfill: o que foi preenchido, o que foi pulado, e o que custou.

    ``unmatched_slugs`` e ``ambiguous_slugs`` são reportados à parte de propósito: são os casos
    em que o backfill se recusou a casar no escuro, e é a lista que o humano inspeciona.
    """

    catalog_items: int
    items_out_of_window: int
    events_without_match: tuple[str, ...]
    fighters_updated: int
    fighters_unchanged: int
    unmatched_slugs: tuple[str, ...]
    ambiguous_slugs: tuple[str, ...]
    headshot_filled: int
    body_image_filled: int
    cito_calls_used: int

    @property
    def total(self) -> int:
        """Lutadores do payload considerados (casados + não casados + ambíguos)."""
        return (
            self.fighters_updated
            + self.fighters_unchanged
            + len(self.unmatched_slugs)
            + len(self.ambiguous_slugs)
        )

    @property
    def coverage(self) -> float:
        """Fração de lutadores do payload que casaram; sem lutador -> ``0.0``."""
        casados = self.fighters_updated + self.fighters_unchanged
        return casados / self.total if self.total else 0.0


def _events_by_cito_slug(session: Session, slugs: set[str]) -> dict[str, Event]:
    """``cito_slug -> Event`` dos eventos persistidos que o catálogo desta execução alcança.

    Uma única consulta para todos os slugs: o item de catálogo já traz o identificador, e é ele
    que liga o card ao evento persistido (nunca uma regra sobre o nome -- ADR 0005).
    """
    if not slugs:
        return {}
    eventos = session.scalars(select(Event).where(Event.cito_slug.in_(slugs)))
    return {evento.cito_slug: evento for evento in eventos if evento.cito_slug is not None}


def _fighter_ids_by_name(session: Session, event_id: int) -> dict[str, list[int]]:
    """Multimap ``name_normalized -> [fighter_id, ...]`` dos lutadores do evento (só leitura).

    O roster do evento é o universo de casamento -- muito mais estreito e seguro que um lookup
    global por nome normalizado. A lista por nome permite detectar a ambiguidade em vez de
    escolher arbitrariamente.
    """
    rows = session.execute(
        select(BoutFighter.fighter_id, Fighter.name_normalized)
        .join(Bout, Bout.id == BoutFighter.bout_id)
        .join(Fighter, Fighter.id == BoutFighter.fighter_id)
        .where(Bout.event_id == event_id)
    ).all()

    by_name: dict[str, list[int]] = {}
    for fighter_id, name_normalized in rows:
        by_name.setdefault(name_normalized, []).append(fighter_id)
    return by_name


def _assign_if_changed(fighter: Fighter, images: FighterImages) -> tuple[bool, bool, bool]:
    """Escreve só o que muda; ``None`` NUNCA sobrescreve valor já persistido.

    Devolve ``(mudou, retrato_preenchido, corpo_preenchido)`` -- o primeiro alimenta
    ``fighters_updated`` (rerun -> 0) e os outros dois contam a cobertura por variante.
    """
    retrato = images.headshot_url is not None and fighter.headshot_url != images.headshot_url
    corpo = images.body_image_url is not None and fighter.body_image_url != images.body_image_url
    if retrato:
        fighter.headshot_url = images.headshot_url
    if corpo:
        fighter.body_image_url = images.body_image_url
    return (retrato or corpo, retrato, corpo)


def _log_backfill_summary(report: ImageBackfillReport, limit: int) -> None:
    """Emite o relatório de cobertura via ``logging`` (``print`` é proibido)."""
    logger.info(
        "Resumo do backfill de imagens da Cito: itens de catálogo=%d (fora da janela=%d); "
        "lutadores %d/%d casados (%.0f%%); atualizados=%d; já em dia=%d; não casados=%d; "
        "ambíguos=%d; retratos preenchidos=%d; corpos preenchidos=%d; chamadas Cito=%d/%d",
        report.catalog_items,
        report.items_out_of_window,
        report.fighters_updated + report.fighters_unchanged,
        report.total,
        report.coverage * 100,
        report.fighters_updated,
        report.fighters_unchanged,
        len(report.unmatched_slugs),
        len(report.ambiguous_slugs),
        report.headshot_filled,
        report.body_image_filled,
        report.cito_calls_used,
        limit,
    )
    for slug in report.events_without_match:
        logger.warning(
            "Item de catálogo %r não tem evento persistido com esse cito_slug; nenhuma imagem "
            "foi gravada para o card dele. Rode a sincronização de catálogo antes.",
            slug,
        )
    for slug in report.ambiguous_slugs:
        logger.warning(
            "Lutador %r pulado por ambiguidade: o nome normalizado casa com mais de um lutador "
            "do roster do evento; nada foi escrito para ele.",
            slug,
        )


def run_image_backfill(
    session: Session,
    client: CitoClient,
    budget: CallBudget,
    *,
    limit: int = CATALOG_PAGE_LIMIT,
    date_from: date | None = None,
    date_to: date | None = None,
    cache: CatalogPageCache | None = None,
) -> ImageBackfillReport:
    """Preenche ``headshot_url``/``body_image_url`` a partir do catálogo com ``includeBouts``.

    Uma única leitura paginada do catálogo alimenta todos os eventos (25 itens por chamada
    nesse recorte). Para cada item dentro da janela, o evento persistido é achado pelo
    ``cito_slug`` e o roster dele é o universo de casamento por nome normalizado.

    Nunca faz INSERT: ``fighter_slug`` sem correspondência entra em ``unmatched_slugs`` e um
    nome que casa com mais de um lutador do roster entra em ``ambiguous_slugs`` -- os dois com
    zero escrita. O commit é do chamador.
    """
    catalog = [
        item
        for item in client.fetch_event_catalog(
            limit=limit, date_from=date_from, date_to=date_to, cache=cache, include_bouts=True
        )
        if is_ufc_catalog_item(item)
    ]

    na_janela = [item for item in catalog if item.local_date >= OFFICIAL_WINDOW_START]
    eventos = _events_by_cito_slug(session, {item.slug for item in na_janela})

    events_without_match: list[str] = []
    unmatched: list[str] = []
    ambiguous: list[str] = []
    updated = 0
    unchanged = 0
    headshot_filled = 0
    body_filled = 0

    for item in na_janela:
        evento = eventos.get(item.slug)
        imagens = extract_fighter_images(item)
        if evento is None:
            if imagens:
                events_without_match.append(item.slug)
            continue

        por_nome = _fighter_ids_by_name(session, evento.id)
        for fighter_slug, imagem in imagens.items():
            candidatos = por_nome.get(_slug_to_normalized_name(fighter_slug), [])
            if len(candidatos) > 1:
                ambiguous.append(fighter_slug)
                continue
            if not candidatos:
                unmatched.append(fighter_slug)
                continue
            fighter = session.get(Fighter, candidatos[0])
            if fighter is None:  # pragma: no cover - o id vem de um join com fighters
                unmatched.append(fighter_slug)
                continue
            mudou, retrato, corpo = _assign_if_changed(fighter, imagem)
            if mudou:
                updated += 1
            else:
                unchanged += 1
            headshot_filled += int(retrato)
            body_filled += int(corpo)

    report = ImageBackfillReport(
        catalog_items=len(catalog),
        items_out_of_window=len(catalog) - len(na_janela),
        events_without_match=tuple(events_without_match),
        fighters_updated=updated,
        fighters_unchanged=unchanged,
        unmatched_slugs=tuple(unmatched),
        ambiguous_slugs=tuple(ambiguous),
        headshot_filled=headshot_filled,
        body_image_filled=body_filled,
        cito_calls_used=budget.used,
    )
    _log_backfill_summary(report, budget.limit)
    return report


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do backfill de imagens."""
    parser = argparse.ArgumentParser(
        description="Preenche as URLs de imagem dos lutadores a partir do catálogo da Cito.",
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
        default=OFFICIAL_WINDOW_START,
        help=(
            "Início da janela (AAAA-MM-DD); filtra a chamada à Cito. "
            f"Default: {OFFICIAL_WINDOW_START.isoformat()} (a janela da SPEC)."
        ),
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        type=date.fromisoformat,
        default=None,
        help="Fim da janela (AAAA-MM-DD); filtra a chamada à Cito.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=CATALOG_PAGE_LIMIT,
        help=(
            f"Itens por página do catálogo (default: {CATALOG_PAGE_LIMIT}); "
            "com includeBouts a Cito clampa em 25."
        ),
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
    """Entrypoint ``python -m ingestion.cito.images`` (backfill das URLs de imagem).

    Aplica o **gate humano** antes de tudo: em rede real sem ``--confirmar-gasto-de-quota``,
    aborta com ``logger.error`` + ``sys.exit(1)`` sem instanciar o cliente nem tocar a rede.
    Confirmado (ou em modo fixture), resolve o teto, abre a sessão real, roda o backfill e
    **commita** só no sucesso.
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
            run_image_backfill(
                session,
                client,
                budget,
                limit=args.limit,
                date_from=args.date_from,
                date_to=args.date_to,
                cache=cache,
            )
        except QuotaExceededError as exc:
            logger.error("Backfill interrompido sem escrita: %s", exc)
            sys.exit(1)
        session.commit()


if __name__ == "__main__":
    main()
