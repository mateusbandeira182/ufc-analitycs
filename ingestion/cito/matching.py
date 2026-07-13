"""Matching persisted-driven de evento Cito <-> ``bout_fighter`` persistido (M5, Slice 04).

Dado um evento **já persistido** (semeado do Kaggle no M0), este módulo resolve, para cada
linha do ``boutStats`` da Cito (``CitoEventStats``), o ``bout_fighter_id`` correspondente já no
banco, casando por **nome normalizado** (``ingestion.normalize.normalize_name``) escopado ao
evento. A chave de saída é ``(cito_bout_id, corner)`` -- o contrato estável que a Slice 05
(backfill round-a-round) consome para saber em qual ``bout_fighter`` gravar cada round.

Desvio consciente do RF-07 (decisão do humano, 2026-07-13)
----------------------------------------------------------
O RF-07 da SPEC descrevia um matching **Cito-driven** por janela-de-data +/-3 dias + prefixo de
slug + nome normalizado. Esta implementação é **persisted-driven**: o evento **já persistido** é
a âncora (a sua ``date`` e o seu roster vêm do seed), e a Cito é consultada **uma única vez por
evento** (``fetch_event_stats``). Duas consequências:

- **Sem janela de data contra a Cito.** O endpoint ``fetch_event_stats`` **não** entrega a data
  do evento, então não há como comparar datas Cito x seed; a data da âncora é a persistida. A
  janela +/-3 dias do RF-07 fica, portanto, sem objeto e é deliberadamente omitida.
- **1 chamada por evento.** O slug Cito é derivado do ``name`` persistido (``event_cito_slug``),
  usado numa só chamada; nunca há fetch por-luta.

O ``bout_fighter_id`` é resolvido por **nome normalizado**, nunca por canto (fontes divergem no
rótulo R/B): o ``corner`` compõe apenas a chave de saída. Ambiguidade (um nome casando com >1
``bout_fighter`` do evento) **falha alto** com ``AmbiguousBoutFighterMatchError`` (espelha a
entity resolution do M1); um ``fighter_slug`` sem correspondência é apenas reportado como
não-casado (não levanta).

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
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.enums import Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito.client import DEFAULT_CALL_BUDGET, CallBudget, CitoClient
from ingestion.cito.dto import CitoEventStats
from ingestion.normalize import normalize_name
from mma_analytics.db import SessionLocal
from mma_analytics.settings import settings

logger = logging.getLogger(__name__)

# Diretório de fixtures usado pela execução de demonstração (``--fixture``), sem consumir quota.
_DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "tests" / "ingestion" / "fixtures"
)

# Identificador numerado de um evento no ``name`` persistido (ex.: 'UFC 319: ...' -> 'ufc-319').
# A chave natural de ``events`` é ``(name, date)`` -- não há coluna ``slug``; o slug Cito é
# derivado do nome. A única convenção de slug Cito confirmada por dado real (fixtures
# ``event_stats_ufc-<n>.json``) é 'ufc-<n>', derivada do prefixo numerado. Formatos não-numerados
# ('UFC Fight Night: ...', 'UFC on ESPN/ABC: ...') NÃO têm convenção derivável com segurança sem
# dado da Cito -- ``event_cito_slug`` levanta ``UnsupportedEventSlugError`` e o backfill (Slice 05)
# pula o evento com aviso, sem heurística silenciosa (nunca chutar e casar o evento errado).
_EVENT_SLUG_PATTERN = re.compile(r"^\s*UFC\s+(\d+)\b", re.IGNORECASE)


class BoutFighterMatchError(Exception):
    """Falha ao reconciliar um ``fighter_slug`` da Cito contra os ``bout_fighters`` do evento."""


class AmbiguousBoutFighterMatchError(BoutFighterMatchError):
    """Nome casa com >1 ``bout_fighter`` do evento -- nunca duplicar/mesclar em silêncio."""


class UnsupportedEventSlugError(ValueError):
    """O ``name`` do evento não permite derivar um slug Cito com segurança (sem heurística).

    Subclasse de ``ValueError`` para preservar o contrato anterior de ``event_cito_slug`` (que já
    levantava ``ValueError`` para nomes fora do formato numerado) -- quem captura ``ValueError``
    continua funcionando. É levantada para os formatos **não-numerados** ('UFC Fight Night: ...',
    'UFC on ESPN: ...', 'UFC on ABC: ...' etc.): a única convenção de slug Cito confirmada por dado
    real (fixtures ``event_stats_ufc-<n>.json``) é 'ufc-<n>', derivada do prefixo numerado; não há
    dado da Cito que confirme como esses formatos são slugificados, então derivar um seria chutar e
    arriscar casar o evento errado. O backfill round-a-round (Slice 05) captura este erro tipado e
    **pula** o evento com aviso -- nunca casa em silêncio nem aborta o run inteiro.
    """


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


def event_cito_slug(event: Event) -> str:
    """Deriva o slug Cito a partir do ``name`` do evento persistido ('UFC 319: ...' -> 'ufc-319').

    Usado numa única chamada ``fetch_event_stats`` por evento (persisted-driven). Só o formato
    **numerado** ('UFC <n>') tem convenção de slug confirmada por dado real ('ufc-<n>'). Um ``name``
    fora desse formato (Fight Night, 'UFC on ESPN/ABC', etc.) não deriva slug em silêncio -- levanta
    ``UnsupportedEventSlugError`` (subtipo de ``ValueError``), pois chutar a slugificação sem dado
    da Cito arriscaria casar o evento errado. Quem chama trata o erro (o backfill pula o evento).
    """
    match = _EVENT_SLUG_PATTERN.match(event.name)
    if match is None:
        raise UnsupportedEventSlugError(
            f"Nome de evento {event.name!r} não segue o formato numerado 'UFC <n>'; "
            "a derivação do slug Cito só cobre eventos numerados (a única convenção confirmada por "
            "dado real da Cito). Formatos não-numerados são pulados pelo backfill, sem chutar."
        )
    return f"ufc-{match.group(1)}"


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
) -> dict[tuple[str, Corner], int]:
    """Casa cada linha de ``event_stats.bout_stats`` ao ``bout_fighter_id`` persistido do evento.

    Persisted-driven: o ``event`` é a âncora; cada ``fighter_slug`` da Cito é normalizado
    (``_slug_to_normalized_name``) e casado ao ``bout_fighter`` do evento por nome. A saída é
    ``{(cito_bout_id, corner): bout_fighter_id}`` -- o contrato que a Slice 05 consome (o
    ``corner`` é só parte da chave, não o critério de matching).

    Nome que casa com >1 ``bout_fighter`` do evento -> ``AmbiguousBoutFighterMatchError`` (falha
    alto). Nome sem correspondência -> ignorado (reportado como não-casado por quem chama, nunca
    levanta). Leitura pura: nenhuma escrita.
    """
    by_name = _bout_fighter_ids_by_name(session, event.id)

    resolved: dict[tuple[str, Corner], int] = {}
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
        resolved[(line.bout_id, line.corner)] = candidates[0]
    return resolved


def run_match_dry_run(session: Session, event: Event, client: CitoClient) -> MatchReport:
    """Casa o evento (fixture) contra os ``bout_fighters`` persistidos e loga a cobertura.

    Encadeia ``event_cito_slug`` -> ``client.fetch_event_stats`` (1 chamada, cobrada no
    ``CallBudget``) -> ``resolve_bout_fighter_ids``, monta o ``MatchReport`` e loga a cobertura
    via ``logging``. Leitura pura: nenhuma escrita, nenhuma chamada por-luta.
    """
    stats = client.fetch_event_stats(event_cito_slug(event))
    resolved = resolve_bout_fighter_ids(session, event, stats)

    matched_keys = set(resolved.keys())
    unmatched = tuple(
        line.fighter_slug
        for line in stats.bout_stats
        if (line.bout_id, line.corner) not in matched_keys
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
    """Localiza o ``Event`` persistido cujo ``event_cito_slug`` bate com ``event_slug``.

    Percorre os eventos e compara o slug derivado do ``name``; eventos com nome fora do formato
    numerado (``UnsupportedEventSlugError``) são ignorados (não são candidatos a um slug 'ufc-<n>').
    Nenhum candidato -> ``BoutFighterMatchError`` claro.
    """
    for event in session.scalars(select(Event)):
        try:
            candidate_slug = event_cito_slug(event)
        except UnsupportedEventSlugError:
            continue
        if candidate_slug == event_slug:
            return event
    raise BoutFighterMatchError(f"Nenhum evento persistido deriva o slug Cito {event_slug!r}.")


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
