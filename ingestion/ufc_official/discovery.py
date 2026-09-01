"""Varredura do catálogo oficial e mapeamento de ``events.ufc_event_id`` (SPEC 008, Slice 02).

Resolve, para cada evento **já persistido** da janela (2010-03-21 em diante), qual é o seu
``EventId`` na API oficial da UFC, e emite o relatório de cobertura com a ambiguidade contada e
os eventos problemáticos **nomeados**.

Estratégia de descoberta incremental (decisão em aberto nº 2 da SPEC 008)
=========================================================================

**O problema.** Os ``eventId`` da fonte são **estáveis** (reconferidos idênticos após uma hora)
mas **não são ordenados por data do evento**: a varredura devolveu eventos de 1993 a 2026 fora
de ordem cronológica. Logo, a descoberta incremental **não pode** assumir "buscar ids acima do
último evento processado por data", nem inferir "id alto implica evento recente".

**A decisão.** Catálogo local materializado por varredura de id, com cache em disco por
``eventId`` e **fronteira crescente**:

1. **O catálogo é a varredura.** A fonte não expõe endpoint de listagem; o catálogo é construído
   buscando ``/api/v3/event/live/{eventId}.json`` id a id.
2. **Cada sucesso é gravado cru em disco**, um arquivo por id
   (``.cache/ufc_official/event_live_{id}.json``). A varredura completa acontece **uma vez**;
   execuções seguintes leem o cache.
3. **A execução incremental busca na rede apenas** (a) os ids **ausentes do cache** dentro do
   intervalo já conhecido (os buracos) e (b) a **fronteira**: ids acima do maior id conhecido,
   avançando até acumular ``FRONTIER_MISS_STREAK`` ausências consecutivas. É o que satisfaz o
   RF-09 -- um evento novo custa dezenas de requisições, não ~1.300.
4. **Por que a fronteira por id funciona apesar da não-ordenação.** A não-ordenação é entre
   ``eventId`` e **data do evento** (o histórico foi carregado em bloco e eventos futuros são
   cadastrados fora de ordem cronológica). A alocação do id, porém, é monotônica no **momento de
   criação do registro**, então um evento recém-anunciado recebe id acima do máximo conhecido. A
   fronteira é ancorada no **maior id já visto**, nunca na data do último evento ingerido -- que
   é exatamente a inferência que a não-ordenação proíbe.
5. **Ausência não é cacheada.** Um id que hoje não existe pode existir amanhã; gravar a ausência
   congelaria o buraco. Re-sondar buracos é irrelevante em custo (fonte gratuita, sem quota).
6. **A premissa da fronteira é falsificável e o alarme é o relatório.** Se a alocação de id
   deixar de ser monotônica na criação, a contagem de eventos da janela sem ``ufc_event_id``
   sobe entre execuções. A revarredura completa não precisa de flag: basta apagar o diretório de
   cache (``rm -rf .cache/ufc_official``).
7. **Sem gate humano de quota.** A fonte é gratuita, sem autenticação e sem rate limit
   observado -- o gate ``--confirmar-gasto-de-quota`` da Cito **não** se aplica aqui, e a sua
   ausência é deliberada, não esquecimento. A varredura é **sequencial**, sem paralelismo, para
   não parecer abuso da origem.

**Alternativas descartadas.** (a) "Buscar acima do último id" ancorado no último evento
*ingerido*: quebra pela não-ordenação por data. (b) Revarredura completa a cada execução: viola
o RF-09 e desperdiça ~1.300 requisições por um punhado de eventos novos. (c) Persistir o
catálogo inteiro no banco: criaria tabela para dado que só serve de índice intermediário, sem
consumidor a jusante.

Ausência na fonte é 200 com envelope vazio
==========================================
A fonte **não** responde 404 para id inexistente: devolve **HTTP 200** com
``{"LiveEventDetail": {}}`` (medido nos ids 0, 1344, 1345, 1350, 1400, 2000, 5000 e 99999).
Por isso a varredura consulta ``is_absent_event_payload`` **antes** de validar: sem essa
distinção, o primeiro id acima da fronteira derrubaria a execução como se a fonte tivesse
quebrado o contrato. Quebra de contrato de verdade (``UfcOfficialContractError``) continua
falhando alto e não é capturada aqui (RF-10).
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import count
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.events.models import Event
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.cache import OfficialEventCache
from ingestion.ufc_official.client import UfcOfficialClient
from ingestion.ufc_official.dto import UfcOfficialError, is_absent_event_payload, parse_event
from ingestion.ufc_official.matching import (
    AmbiguousOfficialEventMatchError,
    OfficialCatalogItem,
    official_event_candidates,
    resolve_official_event_match,
)
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

# Ausências consecutivas que encerram a fronteira. A fonte responde 200 com envelope vazio para
# QUALQUER id (inclusive 99999), então sem um critério de parada a varredura não terminaria.
# Vinte e cinco é folga sobre os buracos observados no intervalo real e custa 25 requisições por
# execução incremental -- ordens de grandeza abaixo das ~1.300 de uma revarredura.
FRONTIER_MISS_STREAK = 25

# ``OrganizationId`` do UFC no payload oficial. A fonte cobre PRIDE (2), WEC (3), Strikeforce
# (4), DREAM (8), K-1 (9), Gladiator FC (33), DWCS (67) e Road to UFC (68) -- 42% de uma amostra
# de 85 eventos sondada em 2026-09-01 não era do UFC. O escopo do projeto é só UFC, e as fases de
# promoção (travadas em 2026-08-31) mantêm DWCS e Road to UFC para depois.
#
# O filtro é EXATO porque a promoção é campo de primeira classe do payload -- diferente do filtro
# da Cito (``is_ufc_catalog_item``), que precisa excluir marcadores no slug por não ter esse
# campo. E ele acontece ANTES do cálculo da data local, o que não é detalhe: o id 380
# (``Gladiator FC - Day 2``, de 2004) vem com ``TimeZone`` nulo, e calcular a data antes de
# filtrar derrubava a varredura inteira num evento que nem nos interessa.
UFC_ORGANIZATION_ID = 1

# Diretório default do cache em disco da varredura (padrão do ``.cache/`` do M5/M6).
_DEFAULT_CACHE_DIR = Path(".cache") / "ufc_official"


@dataclass(frozen=True)
class CatalogScan:
    """Resultado da varredura: os itens do catálogo e o custo que eles tiveram.

    ``highest_id`` é a âncora da fronteira da **próxima** execução e, no relatório, o alarme de
    uma varredura fria truncada: se ele ficar muito abaixo do intervalo conhecido da fonte, o
    intervalo tem um buraco maior que ``FRONTIER_MISS_STREAK``.

    ``undatable`` são os eventos **do UFC** que a fonte não sabe datar (``TimeZone`` nulo): não
    entram no catálogo e são nomeados, nunca datados por palpite.
    """

    items: tuple[OfficialCatalogItem, ...]
    fetched: int
    cache_hits: int
    misses: int
    highest_id: int
    discarded_other_promotions: int
    undatable: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class DiscoveryReport:
    """Cobertura do mapeamento: o que casou, o que não casou e **por quê**.

    As três listas guardam ``(event_id, name)`` porque uma contagem sozinha não é acionável --
    quem lê o relatório precisa saber *quais* eventos inspecionar.

    ``needs_review`` e ``unmatched`` são diagnósticos diferentes de propósito: o primeiro é
    divergência de grafia (havia candidato na janela de data, nenhum corroborou por nome) e o
    segundo é ausência na fonte (nenhum candidato). Colapsá-los esconderia qual dos dois
    problemas a cobertura tem.

    ``undatable`` vem da varredura e não é uma quinta categoria dos **nossos** eventos: são
    eventos do UFC que a **fonte** não sabe datar. Aparece aqui porque, sem ele, o evento
    persistido correspondente sairia como "não casado" -- diagnóstico errado, já que a fonte
    tem o evento, só não tem a data dele.
    """

    catalog_items: int
    discarded_other_promotions: int
    fetched: int
    cache_hits: int
    matched_by_name: int
    needs_review: tuple[tuple[int, str], ...]
    unmatched: tuple[tuple[int, str], ...]
    ambiguous: tuple[tuple[int, str], ...]
    undatable: tuple[tuple[int, str], ...]

    @property
    def matched(self) -> int:
        """Eventos que ganharam identificador."""
        return self.matched_by_name

    @property
    def total(self) -> int:
        """Eventos persistidos **da janela** considerados na execução."""
        return self.matched + len(self.needs_review) + len(self.unmatched) + len(self.ambiguous)

    @property
    def coverage(self) -> float:
        """Fração de eventos casados; janela sem evento -> ``0.0`` (sem divisão por zero)."""
        return self.matched / self.total if self.total else 0.0


def select_window_events(session: Session) -> list[Event]:
    """Eventos persistidos de ``OFFICIAL_WINDOW_START`` em diante, em ordem ``(date, id)``.

    Guarda **única** da janela da RF-03: nada a jusante repete o filtro. Duas guardas para a
    mesma regra escondem qual delas de fato protege -- e a que sobra, quando uma é removida por
    engano, costuma ser a que não estava protegendo.
    """
    statement = (
        select(Event).where(Event.date >= OFFICIAL_WINDOW_START).order_by(Event.date, Event.id)
    )
    return list(session.scalars(statement))


def _fetch_payload(client: UfcOfficialClient | None, event_id: int) -> object | None:
    """Busca o payload cru de um id; ``None`` quando o id não existe ou a busca falha.

    É **aqui** que a ausência vira ``None``, antes do cache: a fonte responde 200 com envelope
    vazio para id inexistente, e deixar esse corpo chegar ao disco congelaria o buraco -- o id
    passaria a ser "conhecido" e nunca mais seria re-sondado.

    ``client=None`` é o **modo offline**: nada é buscado e o catálogo passa a ser exatamente o
    que já está no cache. ``UfcOfficialError`` (HTTP não-200, rede, corpo não-JSON) também vira
    ausência: a varredura passa por mais de mil ids e não pode morrer no primeiro soluço da
    origem. Quebra de contrato **não** passa por aqui -- ela nasce na validação, depois, e
    falha alto (RF-10).
    """
    if client is None:
        return None
    try:
        payload = client.fetch_event_payload(event_id)
    except UfcOfficialError as exc:
        logger.warning("Id %d pulado (falha transitória na fonte): %s", event_id, exc)
        return None
    return None if is_absent_event_payload(payload) else payload


def discover_official_catalog(
    client: UfcOfficialClient | None, cache: OfficialEventCache
) -> CatalogScan:
    """Varre os ids: buracos do intervalo conhecido + fronteira até a sequência de ausências.

    Sequencial e sem paralelismo, por educação com a origem. O cache decide o que custa rede;
    esta função decide **quais ids** consultar (ver a estratégia na docstring do módulo).
    """
    known = cache.known_ids()
    highest_known = max(known, default=0)

    items: list[OfficialCatalogItem] = []
    undatable: list[tuple[int, str]] = []
    fetched = 0
    cache_hits = 0
    misses = 0
    discarded = 0
    highest_id = 0
    miss_streak = 0

    for event_id in count(1):
        if event_id > highest_known and miss_streak >= FRONTIER_MISS_STREAK:
            break

        payload, cache_hit = cache.get_or_fetch(
            event_id, lambda id_alvo: _fetch_payload(client, id_alvo)
        )
        if payload is None:
            misses += 1
            miss_streak += 1
            continue

        if cache_hit:
            cache_hits += 1
        else:
            fetched += 1
        miss_streak = 0
        highest_id = event_id

        # Único ponto da varredura que conhece a forma do payload. A ordem das duas guardas é
        # load-bearing: a promoção é checada ANTES da data local, porque há evento fora do
        # escopo sem ``TimeZone`` na fonte (id 380) e calcular a data dele levantaria por um
        # evento que nem nos interessa.
        event = parse_event(payload, event_id=event_id)
        if event.organization.organization_id != UFC_ORGANIZATION_ID:
            discarded += 1
            continue
        if event.time_zone is None:
            undatable.append((event_id, event.name))
            continue
        items.append(
            OfficialCatalogItem(
                event_id=str(event.event_id),
                name=event.name,
                local_date=event.local_date,
            )
        )

    logger.info(
        "Varredura do catálogo oficial: %d eventos do UFC (%d do cache, %d baixados), "
        "%d descartados de outras promoções, %d ids ausentes, maior id visto=%d",
        len(items),
        cache_hits,
        fetched,
        discarded,
        misses,
        highest_id,
    )
    for event_id, name in undatable:
        logger.warning(
            "Evento %r (eventId %d) do UFC ficou FORA do catálogo: a fonte não traz 'TimeZone' "
            "para ele, então não há data de calendário confiável -- e assumir a UTC deslocaria "
            "o evento em um dia. Medido em 1.067 payloads: um caso em 603 eventos do UFC.",
            name,
            event_id,
        )
    return CatalogScan(
        items=tuple(items),
        fetched=fetched,
        cache_hits=cache_hits,
        misses=misses,
        highest_id=highest_id,
        discarded_other_promotions=discarded,
        undatable=tuple(undatable),
    )


def _assign_if_changed(event: Event, item: OfficialCatalogItem) -> None:
    """Escreve o identificador só quando o valor muda -- rerun não produz UPDATE (CA-05)."""
    if event.ufc_event_id != item.event_id:
        event.ufc_event_id = item.event_id


def _log_coverage(report: DiscoveryReport) -> None:
    """Emite o relatório de cobertura via ``logging`` (``print`` é proibido, regra T20)."""
    logger.info(
        "Resumo do mapeamento da fonte oficial: itens de catálogo (UFC)=%d; descartados de "
        "outras promoções=%d; eventos da janela=%d; cobertura %d/%d (%.1f%%); casados por "
        "nome=%d; em revisão=%d; não casados=%d; ambíguos=%d; ids baixados=%d; cache hits=%d",
        report.catalog_items,
        report.discarded_other_promotions,
        report.total,
        report.matched,
        report.total,
        report.coverage * 100,
        report.matched_by_name,
        len(report.needs_review),
        len(report.unmatched),
        len(report.ambiguous),
        report.fetched,
        report.cache_hits,
    )
    for event_id, name in report.ambiguous:
        logger.warning(
            "Evento %r (id %d) pulado por AMBIGUIDADE: mais de um evento da fonte concorda por "
            "nome na janela de data; nada foi escrito para ele.",
            name,
            event_id,
        )
    for event_id, name in report.needs_review:
        logger.warning(
            "Evento %r (id %d) EM REVISÃO: há candidato na janela de data, mas nenhum concorda "
            "por nome; nada foi escrito (id errado é pior que id ausente).",
            name,
            event_id,
        )
    for event_id, name in report.undatable:
        logger.warning(
            "Evento %r (eventId %d) do UFC ficou fora do catálogo por falta de 'TimeZone' na "
            "fonte: se ele existe na nossa base, aparece acima como NÃO CASADO.",
            name,
            event_id,
        )
    for event_id, name in report.unmatched:
        logger.info(
            "Evento %r (id %d) NÃO CASADO: nenhum candidato na janela de data da fonte oficial.",
            name,
            event_id,
        )


def map_official_event_ids(session: Session, scan: CatalogScan) -> DiscoveryReport:
    """Escreve ``ufc_event_id`` nos eventos da janela que a fonte corrobora, e reporta o resto.

    O catálogo que chega aqui já é só do UFC (a
        descoberta filtra na projeção).

        A ambiguidade é capturada **por evento**: o evento entra em ``ambiguous``, nada é escrito
        para ele e o laço segue. Não há ``SAVEPOINT``, diferente do backfill do M5 -- a decisão de
        casar **precede** qualquer escrita, então não existe estado parcial a reverter (mesmo padrão
        de ``ingestion.cito.sync_catalog``). O commit é do chamador.
    """
    catalog = scan.items

    matched_by_name = 0
    needs_review: list[tuple[int, str]] = []
    unmatched: list[tuple[int, str]] = []
    ambiguous: list[tuple[int, str]] = []

    for event in select_window_events(session):
        candidates = official_event_candidates(event, catalog)
        try:
            match = resolve_official_event_match(event, candidates)
        except AmbiguousOfficialEventMatchError:
            ambiguous.append((event.id, event.name))
            continue
        if match is None:
            destino = needs_review if candidates else unmatched
            destino.append((event.id, event.name))
            continue
        _assign_if_changed(event, match.item)
        matched_by_name += 1

    report = DiscoveryReport(
        catalog_items=len(catalog),
        discarded_other_promotions=scan.discarded_other_promotions,
        fetched=scan.fetched,
        cache_hits=scan.cache_hits,
        matched_by_name=matched_by_name,
        needs_review=tuple(needs_review),
        unmatched=tuple(unmatched),
        ambiguous=tuple(ambiguous),
        undatable=scan.undatable,
    )
    _log_coverage(report)
    return report


def run_discovery(
    session: Session, client: UfcOfficialClient | None, cache: OfficialEventCache
) -> DiscoveryReport:
    """Varre o catálogo e mapeia os eventos da janela -- o entrypoint testável do comando."""
    return map_official_event_ids(session, discover_official_catalog(client, cache))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando da descoberta."""
    parser = argparse.ArgumentParser(
        description=(
            "Descobre o eventId da API oficial da UFC de cada evento persistido da janela "
            "(2010-03-21 em diante) e grava events.ufc_event_id."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=_DEFAULT_CACHE_DIR,
        help=(
            "Diretório do cache em disco da varredura (payload cru por eventId). "
            "Apague-o para revarrer o catálogo inteiro."
        ),
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=None,
        help=(
            "Modo OFFLINE: lê este diretório como cache e não toca a rede "
            "(demonstração sobre o conjunto de fixtures)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """``python -m ingestion.ufc_official.discovery [--cache-dir DIR] [--fixture-dir DIR]``.

    **Sem gate de quota**: a fonte é gratuita e não autenticada, ao contrário da Cito -- a
    ausência do ``--confirmar-gasto-de-quota`` é deliberada. Para revarrer o catálogo inteiro,
    apague o diretório de cache. Commita só no sucesso.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)

    offline = args.fixture_dir is not None
    cache = OfficialEventCache(args.fixture_dir if offline else args.cache_dir)
    client = None if offline else UfcOfficialClient()

    with SessionLocal() as session:
        run_discovery(session, client, cache)
        session.commit()


if __name__ == "__main__":
    main()
