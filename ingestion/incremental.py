"""Ingestão incremental pós-evento via Cito API (M1, Slices 01-03).

Dado um evento novo do UFC, busca-o na Cito (``CitoClient``) e faz o **upsert
idempotente** de ``events`` por chave natural, gravando ``source="cito"``. A chave de
dedup é o nome **normalizado** (mesma ``normalize_name`` do seed M0) mais a ``date``:
um evento já presente na base -- inclusive semeado do Kaggle com grafia divergente em
caixa/acento/espaço -- **não** é duplicado, e a linha existente não é tocada (o
``source`` original é preservado). Rodar de novo o mesmo evento é no-op.

A slice 02 estende o orquestrador com a **entity resolution cross-source** dos lutadores:
para cada canto do evento, busca o perfil na Cito (``get_fighter``) e reconcilia ao
``fighter_id`` já existente (nome normalizado + DOB como desempate) ou insere um novo com
``source="cito"`` -- nunca duplicando nem mesclando em silêncio (``resolve_or_create_fighter``
propaga ``AmbiguousFighterMatchError``). ``resolve_event_fighters`` devolve o mapa
``slug -> fighter_id``, que a slice 03 consome no upsert de bouts.

A slice 03 fecha o encadeamento com o **upsert de ``bouts`` + ``bout_fighters`` (long)**: para
cada luta do evento, mapeia o core (``map_bout_core``; mapa PRÓPRIO Cito -> ``BoutMethod``,
token não previsto degrada para ``NO_CONTEST``), faz o get-or-create da luta por chave natural
**order-independent** (``upsert_bout``; par não-ordenado de ``fighter_id`` via ``sorted``, sem
constraint no banco -- ADR 0001), busca as stats granulares por canto (``fetch_bout_stats``,
``GET /bouts/{boutId}/stats``) e explode em uma linha por lutador-por-luta
(``upsert_bout_fighters``). Trocar os cantos R/B não duplica a luta; as stats ficam granulares;
``source="cito"`` em toda escrita. Sem migration -- ``uq_bout_fighter`` do M0 já cobre a chave long.

A slice 04 endurece o job operacionalmente: um ``CallBudget`` (teto configurável via
``resolve_call_budget`` -- CLI ``--call-budget`` > env ``CITO_CALL_BUDGET`` > default 500) é
cobrado pelo cliente a cada fetch, e o processamento do evento roda dentro de um **SAVEPOINT**
(``session.begin_nested``): uma falha no meio -- inclusive o estouro do teto
(``QuotaExceededError``) -- reverte todas as escritas daquele evento e propaga, sem parcial
inconsistente. Ao final, ``run_incremental`` devolve e emite (via ``logging``) um
``IncrementalSummary`` com inseridos/atualizados por tabela, ``source`` e chamadas gastas.

A lógica testável (``upsert_event``, ``resolve_or_create_fighter``, ``resolve_event_fighters``,
``map_bout_core``, ``upsert_bout``, ``upsert_bout_fighters``, ``resolve_call_budget``,
``run_incremental``) opera sobre a ``Session`` recebida (transacional nos testes); ``main`` é
fino: abre a sessão real, chama ``run_incremental`` e **commita** (mesmo desenho de
``ingestion/seed.py::main``). O modo fixture do cliente permite a execução de demonstração sem
consumir a quota do free tier.

Decisão em aberto #2 da SPEC resolvida pelo default: chave por nome normalizado + data,
**sem tocar o schema** (sem ``cito_event_id``, sem migration nesta slice). A unique
``uq_event_name_date`` do M0 permanece como rede de segurança no banco.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.cito.client import (
    DEFAULT_CALL_BUDGET,
    CallBudget,
    CitoClient,
    QuotaExceededError,
)
from ingestion.cito.dto import CitoBout, CitoBoutStats, CitoEvent, CitoFighter
from ingestion.entity_resolution import ExistingFighter, FighterCandidate, match_fighter_id
from ingestion.normalize import normalize_name
from mma_analytics.db import SessionLocal
from mma_analytics.settings import settings

logger = logging.getLogger(__name__)

SOURCE = "cito"

# Variável de ambiente que sobrepõe o teto default de chamadas à Cito (default vence se ausente).
_ENV_CALL_BUDGET = "CITO_CALL_BUDGET"

# Diretório de fixtures usado pela execução de demonstração (``--fixture``), sem consumir quota.
_DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "ingestion" / "fixtures"


def resolve_call_budget(cli_value: int | None, env: Mapping[str, str]) -> int:
    """Resolve o teto de chamadas à Cito: CLI vence ``CITO_CALL_BUDGET``, que vence o default (500).

    Espelha ``resolve_dataset_dir`` do seed: trocar o teto não exige editar código. O valor
    resolvido instancia o ``CallBudget`` da execução.
    """
    if cli_value is not None:
        return cli_value
    raw = env.get(_ENV_CALL_BUDGET)
    return int(raw) if raw else DEFAULT_CALL_BUDGET


# Mapa PRÓPRIO da Cito -> ``BoutMethod``. Deliberadamente **não** reusa o ``_METHOD_BY_TOKEN`` do
# ``seed_bouts`` (Kaggle): os tokens da Cito são de outra fonte e podem divergir. Um token não
# previsto degrada conservadoramente para ``NO_CONTEST`` com log (mesma política do M0). Ajustar
# quando o payload real da Cito revelar tokens fora deste conjunto (ver relatório da Slice 03).
_CITO_METHOD_BY_TOKEN: dict[str, BoutMethod] = {
    "KO/TKO": BoutMethod.KO_TKO,
    "TKO": BoutMethod.KO_TKO,
    "Submission": BoutMethod.SUBMISSION,
    "Decision - Unanimous": BoutMethod.DECISION,
    "Decision - Split": BoutMethod.DECISION,
    "Decision - Majority": BoutMethod.DECISION,
    "DQ": BoutMethod.DQ,
    "No Contest": BoutMethod.NO_CONTEST,
    "Overturned": BoutMethod.NO_CONTEST,
}


def upsert_event(session: Session, event: CitoEvent) -> int:
    """Upsert idempotente de ``events`` por chave natural ``(normalize_name(name), date)``.

    Grava ``source="cito"`` na linha nova. Retorna quantos eventos foram inseridos nesta
    execução (0 ou 1). Se um evento com a mesma chave normalizada já existe (ex.: semeado
    do Kaggle com grafia divergente), nada é inserido e a linha existente **não** é tocada.
    """
    existing_keys: set[tuple[str, date]] = {
        (normalize_name(name), event_date)
        for name, event_date in session.execute(select(Event.name, Event.date))
    }

    key = (normalize_name(event.name), event.date)
    if key in existing_keys:
        logger.info("Evento %r (%s) já presente; upsert é no-op", event.name, event.date)
        return 0

    session.add(Event(name=event.name, date=event.date, location=None, source=SOURCE))
    session.flush()
    logger.info("Evento %r (%s) inserido com source=%s", event.name, event.date, SOURCE)
    return 1


def _load_existing_fighters(session: Session) -> list[ExistingFighter]:
    """Materializa a chave de matching de todos os fighters persistidos (id + nome + DOB)."""
    return [
        ExistingFighter(id=fighter_id, name_normalized=name_normalized, date_of_birth=dob)
        for fighter_id, name_normalized, dob in session.execute(
            select(Fighter.id, Fighter.name_normalized, Fighter.date_of_birth)
        )
    ]


def resolve_or_create_fighter(session: Session, fighter: CitoFighter) -> int:
    """Reusa o ``fighter_id`` existente ou insere um novo com ``source="cito"``.

    Reconcilia o perfil da Cito contra os fighters já persistidos via ``match_fighter_id``
    (nome normalizado + DOB como desempate). No match, retorna o id existente **sem** tocar
    o registro (o ``source`` do seed não é sobrescrito). Sem match, insere um ``Fighter`` novo
    com ``source="cito"``, mapeando bio e cartel (``wins``/``losses``/``draws`` são NOT NULL;
    o DTO já garante default ``0``). Idempotente: na reexecução, o fighter inserido antes casa
    por ``(name_normalized, DOB)`` e é reusado -- zero inserts. Propaga
    ``AmbiguousFighterMatchError`` (nunca duplica nem mescla em silêncio).
    """
    existing = _load_existing_fighters(session)
    candidate = FighterCandidate(name=fighter.name, date_of_birth=fighter.date_of_birth)
    matched_id = match_fighter_id(candidate, existing)
    if matched_id is not None:
        logger.info("Fighter %r reconciliado ao id existente %d", fighter.name, matched_id)
        return matched_id

    model = Fighter(
        name=fighter.name,
        name_normalized=normalize_name(fighter.name),
        nickname=fighter.nickname,
        date_of_birth=fighter.date_of_birth,
        height_cm=fighter.height_cm,
        reach_cm=fighter.reach_cm,
        stance=fighter.stance,
        wins=fighter.wins,
        losses=fighter.losses,
        draws=fighter.draws,
        source=SOURCE,
    )
    session.add(model)
    session.flush()  # materializa ``model.id`` para o mapa slug -> fighter_id
    logger.info("Fighter %r inserido com source=%s (id=%d)", fighter.name, SOURCE, model.id)
    return model.id


def resolve_event_fighters(
    session: Session, event: CitoEvent, client: CitoClient
) -> dict[str, int]:
    """Resolve cada canto do evento a um ``fighter_id`` e devolve o mapa ``slug -> fighter_id``.

    Para cada slug único dos cantos, busca o perfil na Cito uma única vez (economia de quota:
    slug já resolvido não refaz ``get_fighter``) e chama ``resolve_or_create_fighter``. O mapa
    resultante alimenta o upsert de bouts (``_ingest_bouts``). Propaga
    ``AmbiguousFighterMatchError`` -- a resolução falha alto, sem inserir parcial silencioso.
    """
    fighter_ids: dict[str, int] = {}
    for bout in event.bouts:
        for corner in bout.corners:
            if corner.slug in fighter_ids:
                continue
            profile = client.get_fighter(corner.slug)
            fighter_ids[corner.slug] = resolve_or_create_fighter(session, profile)
    return fighter_ids


class BoutCore(TypedDict):
    """Campos da luta como um todo (tabela ``bouts``), tipados a partir do DTO da Cito.

    Espelha o ``BoutCore`` do ``seed_bouts`` sem acoplar ao módulo do Kaggle -- a origem do
    dado é outra (Cito), e o mapeamento de método é próprio (``_CITO_METHOD_BY_TOKEN``).
    """

    method: BoutMethod
    round: int | None
    ending_time_seconds: int | None
    weight_class: str | None
    winner_corner: Corner | None  # ``None`` em empate / no contest


def _map_cito_method(token: str | None) -> BoutMethod:
    """Mapeia o token de método da Cito ao enum; ausente/não previsto -> ``NO_CONTEST`` (log)."""
    if token is None:
        logger.warning("Luta sem método informado; mapeada conservadoramente para NO_CONTEST")
        return BoutMethod.NO_CONTEST
    method = _CITO_METHOD_BY_TOKEN.get(token.strip())
    if method is None:
        logger.warning(
            "Método Cito não previsto %r; mapeado conservadoramente para NO_CONTEST", token
        )
        return BoutMethod.NO_CONTEST
    return method


def _winner_corner(bout: CitoBout, method: BoutMethod) -> Corner | None:
    """Determina o canto vencedor pelo ``winner_slug``; nulo em no contest / sem vencedor.

    Convenção de canto: ``corners[0]`` é o vermelho, ``corners[1]`` o azul. Um ``winner_slug``
    que não casa com nenhum canto degrada para vencedor nulo (log) em vez de estourar.
    """
    if method is BoutMethod.NO_CONTEST:
        return None
    winner = bout.winner_slug
    if winner is None:
        return None
    red, blue = bout.corners
    if winner == red.slug:
        return Corner.RED
    if winner == blue.slug:
        return Corner.BLUE
    logger.warning("Vencedor %r não casa com nenhum canto da luta; vencedor nulo", winner)
    return None


def map_bout_core(bout: CitoBout) -> BoutCore:
    """Mapeia o resultado de uma luta do DTO da Cito nos campos core de ``Bout`` (função pura)."""
    method = _map_cito_method(bout.method)
    return BoutCore(
        method=method,
        round=bout.finish_round,
        ending_time_seconds=bout.finish_time_seconds,
        weight_class=bout.weight_class,
        winner_corner=_winner_corner(bout, method),
    )


def _bout_key(event_id: int, fid_a: int, fid_b: int) -> tuple[int, int, int]:
    """Chave natural determinística da luta: evento + par não-ordenado de ``fighter_id``.

    Segundo uso concreto da canonização (o primeiro é ``seed_bouts._bout_key``);
    reimplementada localmente de propósito -- a rule of three não foi atingida, então não se
    extrai helper compartilhado ainda (ver plano da Slice 03).
    """
    low, high = sorted((fid_a, fid_b))
    return (event_id, low, high)


def _winner_id(winner_corner: Corner | None, red_id: int, blue_id: int) -> int | None:
    """Traduz o canto vencedor no ``fighter_id`` correspondente (nulo em empate/no contest)."""
    if winner_corner is Corner.RED:
        return red_id
    if winner_corner is Corner.BLUE:
        return blue_id
    return None


def _existing_bout_index(session: Session, event_id: int) -> dict[tuple[int, int, int], int]:
    """Indexa as lutas já persistidas **do evento** pela chave natural order-independent.

    Filtra por ``event_id`` (o incremental processa um evento por execução), evitando varrer a
    tabela inteira. Cada luta com exatamente dois cantos vira uma entrada
    ``(event_id, low_fid, high_fid) -> bout_id``.
    """
    grouped: dict[int, list[int]] = {}
    for bout_id, fighter_id in session.execute(
        select(BoutFighter.bout_id, BoutFighter.fighter_id)
        .join(Bout, Bout.id == BoutFighter.bout_id)
        .where(Bout.event_id == event_id)
    ):
        grouped.setdefault(bout_id, []).append(fighter_id)

    index: dict[tuple[int, int, int], int] = {}
    for bout_id, fighter_ids in grouped.items():
        if len(fighter_ids) != 2:
            continue
        index[_bout_key(event_id, fighter_ids[0], fighter_ids[1])] = bout_id
    return index


def upsert_bout(
    session: Session,
    event_id: int,
    red_fighter_id: int,
    blue_fighter_id: int,
    core: BoutCore,
) -> int:
    """Get-or-create do ``Bout`` pela chave natural order-independent; devolve o ``bout_id``.

    A chave é ``(event_id, par não-ordenado de fighter_id)`` -- trocar os cantos R/B não cria
    luta duplicada (a idempotência é de nível de aplicação, ADR 0001; não há constraint no
    banco). Na inserção grava ``winner_id`` a partir do canto vencedor, o resultado e
    ``source="cito"``, e faz ``flush`` para materializar ``bout.id`` antes das FKs de
    ``bout_fighters``.
    """
    index = _existing_bout_index(session, event_id)
    key = _bout_key(event_id, red_fighter_id, blue_fighter_id)
    existing = index.get(key)
    if existing is not None:
        logger.info("Luta (evento %d, %d vs %d) já presente; upsert é no-op", *key)
        return existing

    bout = Bout(
        event_id=event_id,
        winner_id=_winner_id(core["winner_corner"], red_fighter_id, blue_fighter_id),
        method=core["method"],
        round=core["round"],
        ending_time_seconds=core["ending_time_seconds"],
        weight_class=core["weight_class"],
        source=SOURCE,
    )
    session.add(bout)
    session.flush()  # materializa ``bout.id`` para as FKs de bout_fighters
    logger.info("Luta inserida (evento %d, id=%d) com source=%s", event_id, bout.id, SOURCE)
    return bout.id


def upsert_bout_fighters(
    session: Session,
    bout_id: int,
    stats: CitoBoutStats,
    fighter_id_by_corner: Mapping[Corner, int],
) -> int:
    """Get-or-create long de ``bout_fighters`` por ``(bout_id, fighter_id)``; devolve inseridos.

    Uma linha por lutador-por-luta com as stats granulares do canto (mapeadas 1:1 do DTO;
    ausência vira ``None``) e ``source="cito"``. **Nunca** pré-agrega: red e blue guardam seus
    próprios números. A unicidade ``uq_bout_fighter`` sustenta a chave no banco; a reexecução é
    no-op (o ``fighter_id`` já presente é pulado).
    """
    existing: set[int] = {
        fighter_id
        for (fighter_id,) in session.execute(
            select(BoutFighter.fighter_id).where(BoutFighter.bout_id == bout_id)
        )
    }
    inserted = 0
    for line in stats.fighters:
        fighter_id = fighter_id_by_corner[line.corner]
        if fighter_id in existing:
            continue
        session.add(
            BoutFighter(
                bout_id=bout_id,
                fighter_id=fighter_id,
                corner=line.corner,
                knockdowns=line.knockdowns,
                sig_strikes_landed=line.sig_strikes_landed,
                sig_strikes_attempted=line.sig_strikes_attempted,
                takedowns_landed=line.takedowns_landed,
                takedowns_attempted=line.takedowns_attempted,
                submission_attempts=line.submission_attempts,
                control_time_seconds=line.control_time_seconds,
                source=SOURCE,
            )
        )
        existing.add(fighter_id)
        inserted += 1
    return inserted


def _resolve_event_db_id(session: Session, event: CitoEvent) -> int:
    """Resolve o id persistido do evento pela chave natural ``(normalize_name(name), date)``.

    Chamado após ``upsert_event`` garantir que o evento existe; casa contra a mesma chave
    normalizada usada no upsert (tolera grafia divergente de um evento semeado do Kaggle).
    """
    index: dict[tuple[str, date], int] = {
        (normalize_name(name), event_date): event_id
        for event_id, name, event_date in session.execute(select(Event.id, Event.name, Event.date))
    }
    resolved = index.get((normalize_name(event.name), event.date))
    if resolved is None:
        raise RuntimeError(f"Evento {event.name!r} ({event.date}) não encontrado após o upsert.")
    return resolved


def _ingest_bouts(
    session: Session,
    event: CitoEvent,
    client: CitoClient,
    fighter_ids: Mapping[str, int],
) -> tuple[TableDelta, TableDelta]:
    """Encadeia, por luta do evento: ``map_bout_core`` -> ``upsert_bout`` -> stats -> long.

    ``corners[0]`` é o canto vermelho e ``corners[1]`` o azul (convenção do DTO). Uma luta cujo
    lutador não resolve (ambiguidade tratada na resolução) é registrada e pulada -- nunca se
    insere luta com FK inventada. Devolve ``(delta_bouts, delta_bout_fighters)``, cada um com
    inseridos e atualizados (linhas já presentes por chave natural, reencontradas no rerun).
    """
    event_id = _resolve_event_db_id(session, event)
    known_bout_ids = set(_existing_bout_index(session, event_id).values())
    bouts_inserted = 0
    bouts_updated = 0
    bout_fighters_inserted = 0
    bout_fighters_updated = 0
    for bout in event.bouts:
        red_slug, blue_slug = bout.corners[0].slug, bout.corners[1].slug
        red_fighter_id = fighter_ids.get(red_slug)
        blue_fighter_id = fighter_ids.get(blue_slug)
        if red_fighter_id is None or blue_fighter_id is None:
            logger.warning("Luta %s sem lutador resolvido; pulada", bout.bout_id)
            continue

        bout_db_id = upsert_bout(
            session, event_id, red_fighter_id, blue_fighter_id, map_bout_core(bout)
        )
        if bout_db_id not in known_bout_ids:
            known_bout_ids.add(bout_db_id)
            bouts_inserted += 1
        else:
            bouts_updated += 1

        stats = client.fetch_bout_stats(bout.bout_id)
        fighter_id_by_corner = {Corner.RED: red_fighter_id, Corner.BLUE: blue_fighter_id}
        inserted = upsert_bout_fighters(session, bout_db_id, stats, fighter_id_by_corner)
        bout_fighters_inserted += inserted
        # Cada linha de stat não inserida já estava presente (chave ``(bout_id, fighter_id)``).
        bout_fighters_updated += len(stats.fighters) - inserted
    return (
        TableDelta(inserted=bouts_inserted, updated=bouts_updated),
        TableDelta(inserted=bout_fighters_inserted, updated=bout_fighters_updated),
    )


@dataclass(frozen=True)
class TableDelta:
    """Linhas inseridas e atualizadas (reencontradas por chave natural) de uma tabela."""

    inserted: int
    updated: int


@dataclass(frozen=True)
class IncrementalSummary:
    """Resumo observável de uma execução do job: deltas por tabela + chamadas gastas + origem.

    ``cito_calls_used`` é o consumo do ``CallBudget`` da execução (dentro do teto). ``source`` é
    sempre ``"cito"`` -- toda escrita do incremental rastreia a origem.
    """

    events: TableDelta
    fighters: TableDelta
    bouts: TableDelta
    bout_fighters: TableDelta
    cito_calls_used: int
    source: str


def _log_summary(summary: IncrementalSummary, limit: int) -> None:
    """Emite o resumo de execução via ``logging`` (``print`` é proibido)."""
    logger.info(
        "Resumo incremental (source=%s): "
        "events inseridos=%d atualizados=%d; fighters inseridos=%d atualizados=%d; "
        "bouts inseridos=%d atualizados=%d; bout_fighters inseridos=%d atualizados=%d; "
        "chamadas Cito=%d/%d",
        summary.source,
        summary.events.inserted,
        summary.events.updated,
        summary.fighters.inserted,
        summary.fighters.updated,
        summary.bouts.inserted,
        summary.bouts.updated,
        summary.bout_fighters.inserted,
        summary.bout_fighters.updated,
        summary.cito_calls_used,
        limit,
    )


def run_incremental(
    session: Session, event_id: str, client: CitoClient, budget: CallBudget
) -> IncrementalSummary:
    """Busca o evento ``event_id`` na Cito e persiste evento + lutas + stats atomicamente.

    Encadeia ``client.fetch_event`` -> ``upsert_event`` -> ``resolve_event_fighters`` ->
    ``_ingest_bouts`` dentro de um **SAVEPOINT** (``session.begin_nested``): uma falha no meio
    do evento -- inclusive o estouro do ``budget`` (``QuotaExceededError``) -- reverte todas as
    escritas daquele evento e propaga, deixando o banco consistente para o retry idempotente
    (nenhuma linha parcial persistida). O ``charge`` do orçamento é cobrado pelo cliente a cada
    fetch (o evento é buscado uma única vez -- economia de quota; as stats consomem uma chamada
    por luta). Opera sobre a ``Session`` recebida; o commit externo é do chamador (``main`` em
    produção, o rollback da fixture no teste). Devolve o ``IncrementalSummary`` e o emite no log.
    """
    with session.begin_nested():
        event = client.fetch_event(event_id)
        fighters_before = session.scalar(select(func.count()).select_from(Fighter)) or 0
        events_inserted = upsert_event(session, event)
        fighter_ids = resolve_event_fighters(session, event, client)
        fighters_after = session.scalar(select(func.count()).select_from(Fighter)) or 0
        bouts_delta, bout_fighters_delta = _ingest_bouts(session, event, client, fighter_ids)

    fighters_inserted = fighters_after - fighters_before
    summary = IncrementalSummary(
        events=TableDelta(inserted=events_inserted, updated=1 - events_inserted),
        fighters=TableDelta(
            inserted=fighters_inserted, updated=len(fighter_ids) - fighters_inserted
        ),
        bouts=bouts_delta,
        bout_fighters=bout_fighters_delta,
        cito_calls_used=budget.used,
        source=SOURCE,
    )
    _log_summary(summary, budget.limit)
    return summary


def _build_client(*, fixture: bool, fixture_dir: Path, budget: CallBudget) -> CitoClient:
    """Constrói o ``CitoClient`` com o ``budget`` da execução: modo fixture ou HTTP autenticado.

    O orçamento é cobrado a cada fetch inclusive em modo fixture (o custo modela o free tier).
    """
    if fixture:
        return CitoClient(
            token=settings.cito_api_token,
            base_url=settings.cito_base_url,
            fixture_dir=fixture_dir,
            budget=budget,
        )
    return CitoClient(token=settings.cito_api_token, base_url=settings.cito_base_url, budget=budget)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do entrypoint."""
    parser = argparse.ArgumentParser(
        description="Ingestão incremental de um evento do UFC via Cito API.",
    )
    parser.add_argument("--event", required=True, help="Id do evento na Cito a ingerir.")
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Usa o modo fixture do cliente (lê JSON local, sem consumir a quota da Cito).",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=_DEFAULT_FIXTURE_DIR,
        help="Diretório das fixtures de evento (usado apenas com --fixture).",
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint ``python -m ingestion.incremental --event <id> [--fixture] [--call-budget N]``.

    Resolve o teto (``resolve_call_budget``), instancia o ``CallBudget`` da execução, abre a
    sessão real e encadeia fetch + upsert atômico via ``run_incremental`` (SAVEPOINT por evento)
    e **commita** só no sucesso. Estourar o teto (``QuotaExceededError``) é capturado aqui: o
    SAVEPOINT já reverteu as escritas do evento (zero parcial), então ``main`` apenas emite a
    mensagem clara via ``logger.error`` -- sem propagar traceback cru ao operador -- e encerra com
    ``sys.exit(1)`` (exit code != 0), sem alcançar o commit. O resumo de execução é emitido por
    ``run_incremental`` via ``logging`` (``print`` é proibido); ``main`` é fino e verificado pela
    dupla execução manual do DoD (o commit escapa a transação de teste).
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    budget = CallBudget(limit=resolve_call_budget(args.call_budget, os.environ))
    client = _build_client(fixture=args.fixture, fixture_dir=args.fixture_dir, budget=budget)

    with SessionLocal() as session:
        try:
            run_incremental(session, event_id=args.event, client=client, budget=budget)
        except QuotaExceededError as exc:
            logger.error("Ingestão interrompida sem escrita parcial: %s", exc)
            sys.exit(1)
        session.commit()


if __name__ == "__main__":
    main()
