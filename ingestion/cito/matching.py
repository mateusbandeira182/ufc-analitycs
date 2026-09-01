"""Matching de evento Cito <-> ``bout_fighter`` persistido (M5, Slice 04; revisto pela ADR 0005).

Dado um evento **já persistido** (semeado do Kaggle no M0), este módulo resolve, para cada
linha do ``boutStats`` da Cito (``CitoEventStats``), o ``bout_fighter_id`` correspondente já no
banco, casando por **nome normalizado** (``ingestion.normalize.normalize_name``) escopado ao
evento. A chave de saída é ``(cito_bout_id, fighter_slug)`` -- o contrato que o backfill
round-a-round consome para saber em qual ``bout_fighter`` gravar cada round.

Âncora persistida, identificador vindo do catálogo
--------------------------------------------------
O evento **já persistido** é a âncora (a sua ``date`` e o seu roster vêm do seed), e a Cito é
consultada **uma única vez por evento** (``fetch_event_stats``, nunca por-luta). O identificador
Cito desse evento vem de ``Event.cito_slug``, resolvido contra o **catálogo real** pela Sprint
007-03 -- nunca derivado do ``name`` por regra.

A decisão anterior (registrada como "ADR 0004 -- matching persisted-driven") derivava o slug do
nome, restrita ao formato numerado, sobre a premissa de que o endpoint de stats não expunha a
data nem o nome do evento. A sondagem de 2026-08-31 falsificou a premissa (o payload traz
``data.event`` com ``eventDate``, ``title``, ``slug``) e mediu o custo da heurística: ela
acertava o slug de **2 dos 745** eventos persistidos. Ver ADR 0005.

O ``bout_fighter_id`` é resolvido por **nome normalizado**, nunca por canto: as fontes divergem
no rótulo R/B e a API real sequer traz ``corner`` na linha de stat (o canto vive em
``bouts[].fighters[]``). Ambiguidade (um nome casando com >1 ``bout_fighter`` do evento) **falha
alto** com ``AmbiguousBoutFighterMatchError`` (espelha a entity resolution do M1); um
``fighter_slug`` sem correspondência é apenas reportado como não-casado (não levanta).

Esta slice é **leitura pura**: nenhuma escrita (nada em ``bout_fighter_rounds`` -- isso é a
Slice 05). O dry-run (``run_match_dry_run`` / ``main``) roda em modo fixture, sem quota real, e
loga a cobertura via ``logging`` (``print`` é proibido).
"""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito.client import DEFAULT_CALL_BUDGET, CallBudget, CitoClient
from ingestion.cito.dto import CitoCatalogItem, CitoEventStats
from ingestion.normalize import normalize_name
from mma_analytics.db import SessionLocal
from mma_analytics.settings import settings

logger = logging.getLogger(__name__)

# Diretório de fixtures usado pela execução de demonstração (``--fixture``), sem consumir quota.
_DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "tests" / "ingestion" / "fixtures"
)


class BoutFighterMatchError(Exception):
    """Falha ao reconciliar um ``fighter_slug`` da Cito contra os ``bout_fighters`` do evento."""


class AmbiguousBoutFighterMatchError(BoutFighterMatchError):
    """Nome casa com >1 ``bout_fighter`` do evento -- nunca duplicar/mesclar em silêncio."""


@dataclass(frozen=True)
class MatchReport:
    """Relatório de cobertura do matching de um evento (casados vs. não-casados)."""

    event_id: int
    matched: int
    unmatched_slugs: tuple[str, ...]

    @property
    def total(self) -> int:
        """Total de linhas de ``boutStats`` consideradas (casadas + não-casadas)."""
        return self.matched + len(self.unmatched_slugs)

    @property
    def coverage(self) -> float:
        """Fração de linhas casadas; evento sem linhas -> ``0.0`` (sem divisão por zero)."""
        return self.matched / self.total if self.total else 0.0


def _slug_to_normalized_name(fighter_slug: str) -> str:
    """Converte o ``fighter_slug`` da Cito ('dricus-du-plessis') na chave de nome normalizado.

    Reusa a ``normalize_name`` do M0/M1 (mesma chave da entity resolution): os hífens do slug
    viram espaços e o resultado passa pela normalização determinística (acentos/caixa/sufixos).
    """
    return normalize_name(fighter_slug.replace("-", " "))


def _bout_fighter_ids_by_name(session: Session, event_id: int) -> dict[str, list[int]]:
    """Multimap ``name_normalized -> [bout_fighter_id, ...]`` dos cantos do evento (só leitura).

    Um único ``select`` join ``BoutFighter -> Bout -> Fighter`` escopado ao evento. A lista por
    nome permite detectar a ambiguidade (mais de um ``bout_fighter`` com o mesmo nome normalizado
    no evento) em vez de escolher arbitrariamente.
    """
    rows = session.execute(
        select(BoutFighter.id, Fighter.name_normalized)
        .join(Bout, Bout.id == BoutFighter.bout_id)
        .join(Fighter, Fighter.id == BoutFighter.fighter_id)
        .where(Bout.event_id == event_id)
    ).all()

    by_name: dict[str, list[int]] = {}
    for bout_fighter_id, name_normalized in rows:
        by_name.setdefault(name_normalized, []).append(bout_fighter_id)
    return by_name


def resolve_bout_fighter_ids(
    session: Session, event: Event, event_stats: CitoEventStats
) -> dict[tuple[str, str], int]:
    """Casa cada linha de ``event_stats.bout_stats`` ao ``bout_fighter_id`` persistido do evento.

    Persisted-driven: o ``event`` é a âncora; cada ``fighter_slug`` da Cito é normalizado
    (``_slug_to_normalized_name``) e casado ao ``bout_fighter`` do evento por nome. A saída é
    ``{(cito_bout_id, fighter_slug): bout_fighter_id}`` -- o contrato que o backfill consome.

    A chave usa o **slug**, não o canto: a API real não traz ``corner`` na linha de stat (o rótulo
    vive em ``bouts[].fighters[]``), então uma chave com canto colidiria nos dois cantos da mesma
    luta e gravaria o round no ``bout_fighter`` errado, em silêncio. O canto nunca foi critério de
    matching -- o critério sempre foi o nome normalizado. Ver ADR 0005.

    Nome que casa com >1 ``bout_fighter`` do evento -> ``AmbiguousBoutFighterMatchError`` (falha
    alto). Nome sem correspondência -> ignorado (reportado como não-casado por quem chama, nunca
    levanta). Leitura pura: nenhuma escrita.
    """
    by_name = _bout_fighter_ids_by_name(session, event.id)

    resolved: dict[tuple[str, str], int] = {}
    for line in event_stats.bout_stats:
        name = _slug_to_normalized_name(line.fighter_slug)
        candidates = by_name.get(name, [])
        if len(candidates) > 1:
            raise AmbiguousBoutFighterMatchError(
                f"O nome {name!r} (slug {line.fighter_slug!r}) casa com {len(candidates)} "
                f"bout_fighters do evento {event.id}; nunca casar em silêncio."
            )
        if not candidates:
            continue
        resolved[(line.bout_id, line.fighter_slug)] = candidates[0]
    return resolved


def run_match_dry_run(session: Session, event: Event, client: CitoClient) -> MatchReport:
    """Casa o evento (fixture) contra os ``bout_fighters`` persistidos e loga a cobertura.

    Encadeia ``event.cito_slug`` -> ``client.fetch_event_stats`` (1 chamada, cobrada no
    ``CallBudget``) -> ``resolve_bout_fighter_ids``, monta o ``MatchReport`` e loga a cobertura
    via ``logging``. Leitura pura: nenhuma escrita, nenhuma chamada por-luta.

    Um evento sem ``cito_slug`` **falha alto** antes de gastar quota: aqui o alvo é um evento
    específico, escolhido pelo humano, então não há o que pular -- ao contrário do backfill em
    lote, que segue para o próximo. Nenhum slug é derivado do nome (ADR 0005).
    """
    slug = event.cito_slug
    if slug is None:
        raise BoutFighterMatchError(
            f"O evento {event.name!r} (id {event.id}) não tem cito_slug persistido; "
            "rode a sincronização de catálogo (python -m ingestion.cito.sync_catalog) antes."
        )
    stats = client.fetch_event_stats(slug)
    resolved = resolve_bout_fighter_ids(session, event, stats)

    matched_keys = set(resolved.keys())
    unmatched = tuple(
        line.fighter_slug
        for line in stats.bout_stats
        if (line.bout_id, line.fighter_slug) not in matched_keys
    )
    report = MatchReport(event_id=event.id, matched=len(resolved), unmatched_slugs=unmatched)
    logger.info(
        "Matching do evento %r (id %d): cobertura %d/%d (%.0f%%)",
        event.name,
        event.id,
        report.matched,
        report.total,
        report.coverage * 100,
    )
    return report


def _find_event_by_cito_slug(session: Session, event_slug: str) -> Event:
    """Localiza o ``Event`` persistido cujo ``cito_slug`` é ``event_slug`` (consulta direta).

    O identificador é o que a sincronização de catálogo gravou (Sprint 007-03); nada é derivado
    do ``name``. Nenhum candidato -> ``BoutFighterMatchError`` apontando o pré-requisito.
    """
    event = session.scalars(select(Event).where(Event.cito_slug == event_slug)).one_or_none()
    if event is None:
        raise BoutFighterMatchError(
            f"Nenhum evento persistido tem cito_slug {event_slug!r}; rode a sincronização de "
            "catálogo (python -m ingestion.cito.sync_catalog) antes."
        )
    return event


def _build_client(*, fixture: bool, fixture_dir: Path, budget: CallBudget) -> CitoClient:
    """Constrói o ``CitoClient`` do dry-run: modo fixture (0 quota real) ou HTTP autenticado."""
    if fixture:
        return CitoClient(
            token=settings.cito_api_token,
            base_url=settings.cito_base_url,
            fixture_dir=fixture_dir,
            budget=budget,
        )
    return CitoClient(token=settings.cito_api_token, base_url=settings.cito_base_url, budget=budget)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do dry-run."""
    parser = argparse.ArgumentParser(
        description="Dry-run do matching de um evento Cito contra os bouts persistidos.",
    )
    parser.add_argument(
        "--event-slug", required=True, help="Slug Cito do evento a casar (ex.: 'ufc-319')."
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
        "--call-budget",
        type=int,
        default=DEFAULT_CALL_BUDGET,
        help="Teto de chamadas à Cito nesta execução (default: free tier 500).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint ``python -m ingestion.cito.matching --event-slug <slug> [--fixture]``.

    Localiza o evento persistido cujo slug derivado bate com ``--event-slug``, casa (dry-run) e
    loga a cobertura. Leitura pura: não escreve nem comita nada (o dry-run só reporta).
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    budget = CallBudget(limit=args.call_budget)
    client = _build_client(fixture=args.fixture, fixture_dir=args.fixture_dir, budget=budget)

    with SessionLocal() as session:
        event = _find_event_by_cito_slug(session, args.event_slug)
        run_match_dry_run(session, event, client)


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Casamento evento persistido <-> item do catálogo Cito (M6, SPEC 007, Slice 03).
#
# Aditivo: o matching persisted-driven acima (Slice 04 do M5) permanece intocado. Aqui a
# âncora continua sendo o evento **já persistido**, mas o identificador da Cito passa a vir
# do **catálogo real** (RF-04) em vez de ser derivado do nome -- é o que destrava os Fight
# Nights, cujo slug usa a data local e não tem convenção derivável com segurança.
# ---------------------------------------------------------------------------

# Pontuação e qualquer caractere não-alfanumérico remanescente viram separador de token.
_NON_ALNUM = re.compile(r"[^0-9a-z]+")

# Promoções fora do escopo desta fase (decisão do humano, SPEC 007): DWCS e Road to UFC.
# O filtro é por EXCLUSÃO destes marcadores, nunca por allowlist de prefixo 'ufc-' no slug:
# 'cryptocom-ufc-331' e 'ufc-freedom-250' são UFC e um prefixo os descartaria, enquanto
# 'road-to-ufc-season-5-semifinals' contém 'ufc' no meio e NÃO é UFC.
_NON_UFC_MARKERS = ("dwcs", "dana white", "contender series", "road to ufc")

# A data persistida em ``events.date`` é a data LOCAL do evento (seed do Kaggle/ufcstats) e a
# do catálogo é ``CitoCatalogItem.local_date`` (``eventDate`` quando exposto, senão a data UTC
# de ``startsAt``). Uma janela simétrica de um dia absorve tanto a divergência residual entre
# as fontes quanto o caso em que o catálogo não expõe ``eventDate`` e a data cai para a UTC
# (card noturno nos EUA vira o dia seguinte; fusos a leste deslocam no sentido oposto).
# NÃO é heurística de slug: a data é dado das duas pontas.
_EVENT_DATE_TOLERANCE = timedelta(days=1)


class EventMatchError(Exception):
    """Falha ao resolver o item de catálogo Cito de um evento persistido."""


class AmbiguousEventMatchError(EventMatchError):
    """Mais de um item de catálogo casa e o nome não desempata -- nunca casar em silêncio.

    Espelha ``AmbiguousBoutFighterMatchError`` (M5) e a entity resolution do M1: a
    ambiguidade **falha alto**; quem chama decide se aborta ou conta e segue (RF-05).
    """


def normalize_event_name(name: str) -> str:
    """'UFC 319: Du Plessis vs. Chimaev' -> 'ufc 319 du plessis vs chimaev'.

    Aplica ``normalize_name`` PRIMEIRO (NFKD -> ASCII, caixa, espaços) e só então troca a
    pontuação restante por espaço. A ordem importa: remover não-alfanuméricos antes do NFKD
    comeria letras acentuadas ('Šarić' -> 'ari').
    """
    return " ".join(_NON_ALNUM.sub(" ", normalize_name(name)).split())


def is_ufc_catalog_item(item: CitoCatalogItem) -> bool:
    """Mantém só eventos do UFC; descarta DWCS e Road to UFC (fora do escopo desta fase).

    Filtro por **exclusão** de marcadores, nunca por prefixo 'ufc-' no slug -- ver o
    comentário de ``_NON_UFC_MARKERS`` para os casos reais que justificam a escolha.
    """
    haystack = f"{item.slug.replace('-', ' ')} {normalize_event_name(item.title)}"
    return not any(marker in haystack for marker in _NON_UFC_MARKERS)


@dataclass(frozen=True)
class EventMatch:
    """Item de catálogo casado a um evento persistido, com o critério que o resolveu.

    ``matched_by_name`` distingue os dois graus de confiança: ``True`` quando o nome
    normalizado concordou (casamento forte), ``False`` quando só a data sustentou o
    casamento (candidato único na janela) -- este último é reportado à parte para inspeção
    humana antes de a Slice 05 gastar quota em cima do slug.
    """

    item: CitoCatalogItem
    matched_by_name: bool


def resolve_event_match(event: Event, catalog: Sequence[CitoCatalogItem]) -> EventMatch | None:
    """Casa ``event`` com um item do catálogo por data local (+/-1 dia) + nome normalizado.

    Fase 1 -- **candidatos**: itens cuja ``local_date`` está a no máximo um dia da
    ``event.date`` persistida. Fase 2 -- **desempate**: entre os candidatos, os de
    ``normalize_event_name`` igual ao do evento vencem; exatamente um -> casa
    (``matched_by_name=True``); mais de um -> ``AmbiguousEventMatchError``. Sem nenhum
    concordante, um único candidato na janela é aceito (``matched_by_name=False``,
    reportado à parte); mais de um -> ``AmbiguousEventMatchError``; nenhum -> ``None``
    (não casado).

    Função pura: não toca o banco e não escreve. O slug **nunca** é derivado -- ele vem do
    item de catálogo (RF-04).
    """
    candidates = [
        item for item in catalog if abs(item.local_date - event.date) <= _EVENT_DATE_TOLERANCE
    ]
    if not candidates:
        return None

    event_key = normalize_event_name(event.name)
    by_name = [item for item in candidates if normalize_event_name(item.title) == event_key]
    if len(by_name) == 1:
        return EventMatch(item=by_name[0], matched_by_name=True)
    if len(by_name) > 1:
        raise AmbiguousEventMatchError(
            f"O evento {event.name!r} ({event.date}) casa por nome com {len(by_name)} itens do "
            f"catálogo Cito ({', '.join(item.slug for item in by_name)}); nunca casar em silêncio."
        )

    if len(candidates) > 1:
        raise AmbiguousEventMatchError(
            f"O evento {event.name!r} ({event.date}) tem {len(candidates)} candidatos na janela de "
            f"+/-1 dia ({', '.join(item.slug for item in candidates)}) e nenhum concorda por nome; "
            "nunca escolher arbitrariamente."
        )
    return EventMatch(item=candidates[0], matched_by_name=False)
