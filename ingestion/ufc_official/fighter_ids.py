"""Identificador de lutador da fonte oficial como chave forte (SPEC 008, Slice 03).

Preenche ``fighters.ufc_fighter_id``/``ufc_mma_id`` a partir dos cantos das lutas dos eventos
já mapeados pela Slice 02, e é o dado que faz a entity resolution
(``ingestion.entity_resolution.match_fighter_id``) resolver por id **antes** do nome.

Um payload de evento, não um por luta (decisão do PASSO-04, medida)
-------------------------------------------------------------------
O ``FighterId`` e o ``MMAId`` vêm dentro de ``FightCard[].Fighters[]`` do **endpoint de
evento** -- verificado nas capturas verbatim da Sprint 008-01 e reconferido nos 808 eventos do
UFC materializados em cache (8.943 lutas, todas com exatamente dois cantos, ``FighterId`` em
100% delas). Logo a coleta custa **uma requisição por evento** (~641 na janela) em vez de uma
por luta em ``fight/live/{fightId}`` (~11.635). A fonte é gratuita e não tem quota, mas 18x
mais requisições é 18x mais superfície de falha transitória e 18x mais tempo, sem nada em
troca -- é a regra "medir antes de gastar" do CLAUDE.md aplicada a um custo que não é quota.

Na prática o custo é ainda menor: a coleta lê pelo mesmo ``OfficialEventCache`` da Slice 02,
então um cache já materializado a torna offline.

O id **complementa** o nome normalizado, não o substitui (decisão em aberto nº 3 da SPEC)
------------------------------------------------------------------------------------------
1. **A cobertura do id nunca será total, por decisão da própria SPEC.** RF-03/CA-08 proíbem
   tocar evento anterior a 2010-03-21; lutador ativo apenas antes dessa data jamais receberá
   ``ufc_fighter_id``. Substituir o nome deixaria esse conjunto **sem chave nenhuma**.
2. **A Cito não carrega o id da fonte oficial.** ``ingestion.incremental.resolve_or_create_fighter``
   recebe um ``CitoFighter`` cujo payload não tem ``FighterId``; substituir a chave arrancaria a
   única que aquele caminho tem.
3. **Simple design**: a mudança é um branch novo no topo de ``match_fighter_id``, sem módulo
   nem classe nova, e sem remover código que funciona.

Consequência: ``ufc_fighter_id is None`` **não é falha** -- é o estado normal de quem está fora
da janela. Nada aqui trata a ausência como erro.

Coleta e escrita são fases separadas
------------------------------------
``collect_identity_observations`` **não escreve nada**: acumula uma observação por canto de
luta casada e devolve. A escrita (``run_fighter_id_backfill``) só acontece depois da
consolidação, e é isso que torna estruturalmente impossível gravar um conflito. Conflito
(mesmo lutador observado com dois ids), colisão (id que já pertence a outro lutador) e
divergência (valor persistido diferente do observado) são **contados e nomeados**, nunca
escritos -- e nenhum deles derruba o run: o problema de um lutador não custa os outros 2.267.

O casamento é por par NÃO-ORDENADO de nomes normalizados dentro do evento já mapeado. É ele que
resolve o homônimo: o nome sozinho não distingue os dois ``Bruno Silva``, mas o par
(nome, adversário, evento) distingue -- sem nenhuma chamada extra e sem desempate por idade.

``source`` não muda
-------------------
Preencher ``ufc_fighter_id`` numa linha semeada do Kaggle **mantém** ``source="kaggle"``:
``source`` é a origem da **linha**, não de cada campo (decisão do humano em 2026-09-01,
registrada no CLAUDE.md). Não há valor composto.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.entity_resolution import (
    AmbiguousFighterMatchError,
    ExistingFighter,
    FighterCandidate,
    match_fighter_id,
)
from ingestion.incremental import load_existing_fighters
from ingestion.normalize import normalize_name
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.cache import OfficialEventCache
from ingestion.ufc_official.client import UfcOfficialClient
from ingestion.ufc_official.dto import (
    UfcOfficialError,
    UfcOfficialEvent,
    UfcOfficialFighter,
    is_absent_event_payload,
    parse_event,
)
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

# Diretório default do cache em disco da varredura -- o mesmo da Slice 02, de propósito: o
# payload de evento que a descoberta já baixou é exatamente o que esta coleta precisa.
_DEFAULT_CACHE_DIR = Path(".cache") / "ufc_official"

# Cantos por luta na fonte. Medido em 8.943 lutas de 808 eventos do UFC: 100% têm exatamente
# dois. Uma luta fora dessa forma não é adivinhada -- entra em ``unmatched_bouts``.
_CORNERS_PER_BOUT = 2


@dataclass(frozen=True)
class SourceCornerIdentity:
    """Um canto da fonte oficial, já tipado: **identidade**, nunca desfecho.

    Os identificadores são texto porque é assim que ``fighters`` os guarda: id externo é opaco
    e nunca entra em aritmética. ``ufc_mma_id`` ausente permanece ``None`` (falta em 69 de 450
    lutadores medidos) -- nunca zero, nunca string vazia.
    """

    ufc_fighter_id: str
    ufc_mma_id: str | None
    name: str
    name_normalized: str


@dataclass(frozen=True)
class SourceBoutIdentities:
    """Os dois cantos de uma luta da fonte, com a chave de casamento contra a base."""

    corners: tuple[SourceCornerIdentity, SourceCornerIdentity]

    @property
    def name_key(self) -> frozenset[str]:
        """Par **não-ordenado** de nomes normalizados.

        Mesma chave natural de ``bouts`` desde o M0 (evento + par de lutadores), imune à ordem
        em que a fonte lista os cantos.
        """
        return frozenset(corner.name_normalized for corner in self.corners)


def extract_bout_identities(event: UfcOfficialEvent) -> list[SourceBoutIdentities]:
    """Desembrulha o DTO da Sprint 008-01 nas identidades por luta. Função **pura**.

    Luta que não tenha exatamente dois cantos é descartada: o par não-ordenado é a chave de
    casamento, e sem dois nomes não há chave -- adivinhar qual canto é qual gravaria o id
    errado no lutador errado.
    """
    lutas: list[SourceBoutIdentities] = []
    for fight in event.fight_card:
        if len(fight.fighters) != _CORNERS_PER_BOUT:
            logger.warning(
                "Luta %d do evento %d tem %d cantos (esperado %d); pulada na coleta de "
                "identidades.",
                fight.fight_id,
                event.event_id,
                len(fight.fighters),
                _CORNERS_PER_BOUT,
            )
            continue
        primeiro, segundo = (_identidade(fighter) for fighter in fight.fighters)
        lutas.append(SourceBoutIdentities(corners=(primeiro, segundo)))
    return lutas


def _identidade(fighter: UfcOfficialFighter) -> SourceCornerIdentity:
    """Projeta um canto do DTO na identidade usada no casamento."""
    nome = f"{fighter.name.first_name} {fighter.name.last_name}".strip()
    return SourceCornerIdentity(
        ufc_fighter_id=str(fighter.fighter_id),
        ufc_mma_id=None if fighter.mma_id is None else str(fighter.mma_id),
        name=nome,
        name_normalized=normalize_name(nome),
    )


@dataclass(frozen=True)
class IdentityObservation:
    """Uma atribuição observada em UMA luta: quem é, na fonte, o lutador persistido."""

    fighter_id: int
    ufc_fighter_id: str
    ufc_mma_id: str | None
    event_id: int


@dataclass(frozen=True)
class IdentityCollection:
    """O que a coleta viu, sem nada escrito.

    ``unmatched_bouts`` e ``events_without_payload`` são diagnósticos **diferentes**: o
    primeiro é luta persistida cujo par de nomes não existe no card da fonte (divergência de
    grafia ou luta que a fonte não tem), e o segundo é evento mapeado que a fonte não devolveu
    nesta execução. Colapsá-los esconderia qual dos dois problemas a cobertura tem.
    """

    observations: tuple[IdentityObservation, ...]
    unmatched_bouts: tuple[tuple[int, str], ...]
    events_without_ufc_event_id: int
    events_without_payload: tuple[tuple[int, str], ...]


def _fetch_payload(client: UfcOfficialClient | None, event_id: int) -> object | None:
    """Payload cru de um id; ``None`` quando o id não existe ou a busca falha.

    Gêmea de ``discovery._fetch_payload``, e deliberadamente **não** compartilhada: são duas
    ocorrências de dez linhas, e extrair um lugar comum na segunda violaria a rule of three.
    Se uma terceira aparecer, aí sim a extração se justifica.

    ``client=None`` é o **modo offline**: nada é buscado e a coleta passa a enxergar apenas o
    que já está no cache.
    """
    if client is None:
        return None
    try:
        payload = client.fetch_event_payload(event_id)
    except UfcOfficialError as exc:
        logger.warning("Evento %d pulado (falha transitória na fonte): %s", event_id, exc)
        return None
    return None if is_absent_event_payload(payload) else payload


@dataclass(frozen=True)
class _PersistedBout:
    """Uma luta persistida reduzida ao que o casamento precisa: o par de nomes normalizados."""

    bout_id: int
    fighters_by_name: dict[str, int]


def _persisted_bouts_by_event(
    session: Session, window_start: date
) -> dict[int, tuple[Event, list[_PersistedBout]]]:
    """Lutas persistidas dos eventos da janela **com** ``ufc_event_id``, agrupadas por evento.

    O filtro por ``Event.date >= window_start`` é **explícito**, além da exigência de
    ``ufc_event_id``. As duas condições são redundantes hoje (a Sprint 008-02 só preencheu a
    janela), e a redundância é deliberada: CA-08 é invariante de SPEC e não pode depender do
    acerto de outra sprint.
    """
    statement = (
        select(Event, Bout.id, Fighter.id, Fighter.name_normalized)
        .join(Bout, Bout.event_id == Event.id)
        .join(BoutFighter, BoutFighter.bout_id == Bout.id)
        .join(Fighter, Fighter.id == BoutFighter.fighter_id)
        .where(Event.date >= window_start, Event.ufc_event_id.is_not(None))
        .order_by(Event.id, Bout.id, Fighter.id)
    )
    por_evento: dict[int, tuple[Event, list[_PersistedBout]]] = {}
    lutas: dict[int, _PersistedBout] = {}
    for event, bout_id, fighter_id, name_normalized in session.execute(statement):
        _, do_evento = por_evento.setdefault(event.id, (event, []))
        luta = lutas.get(bout_id)
        if luta is None:
            luta = _PersistedBout(bout_id=bout_id, fighters_by_name={})
            lutas[bout_id] = luta
            do_evento.append(luta)
        luta.fighters_by_name[name_normalized] = fighter_id
    return por_evento


def collect_identity_observations(
    session: Session,
    client: UfcOfficialClient | None,
    cache: OfficialEventCache,
    *,
    window_start: date = OFFICIAL_WINDOW_START,
) -> IdentityCollection:
    """Observações por luta casada + o que não casou. **Não escreve nada.**

    O laço é persisted-driven (mesmo padrão de ``ingestion.cito.backfill_rounds`` e
    ``sync_catalog``): para cada evento da janela com ``ufc_event_id``, casa cada luta
    persistida com a luta da fonte pelo **par não-ordenado de nomes normalizados** e, dentro da
    luta casada, atribui cada canto ao ``fighter_id`` persistido de mesmo nome normalizado.

    É o par que resolve o homônimo: o nome sozinho não distingue os dois ``Bruno Silva``, mas
    (nome, adversário, evento) distingue. Luta sem correspondência entra em ``unmatched_bouts``
    e é pulada -- nunca se adivinha o par.
    """
    observations: list[IdentityObservation] = []
    unmatched: list[tuple[int, str]] = []
    sem_payload: list[tuple[int, str]] = []

    for event, lutas_persistidas in _persisted_bouts_by_event(session, window_start).values():
        # O ``select`` garante que não é nulo; o ``assert`` existe para o mypy --strict.
        assert event.ufc_event_id is not None  # noqa: S101
        payload, _ = cache.get_or_fetch(
            int(event.ufc_event_id), lambda id_alvo: _fetch_payload(client, id_alvo)
        )
        if payload is None:
            sem_payload.append((event.id, event.name))
            continue
        da_fonte = {
            luta.name_key: luta
            for luta in extract_bout_identities(
                parse_event(payload, event_id=int(event.ufc_event_id))
            )
        }
        for persistida in lutas_persistidas:
            correspondente = da_fonte.get(frozenset(persistida.fighters_by_name))
            if correspondente is None:
                unmatched.append((persistida.bout_id, event.name))
                continue
            observations.extend(
                IdentityObservation(
                    fighter_id=persistida.fighters_by_name[canto.name_normalized],
                    ufc_fighter_id=canto.ufc_fighter_id,
                    ufc_mma_id=canto.ufc_mma_id,
                    event_id=event.id,
                )
                for canto in correspondente.corners
            )

    return IdentityCollection(
        observations=tuple(observations),
        unmatched_bouts=tuple(unmatched),
        events_without_ufc_event_id=_count_events_without_official_id(session, window_start),
        events_without_payload=tuple(sem_payload),
    )


def _count_events_without_official_id(session: Session, window_start: date) -> int:
    """Eventos da janela que a Slice 02 não conseguiu mapear -- teto da cobertura possível."""
    statement = (
        select(func.count())
        .select_from(Event)
        .where(Event.date >= window_start, Event.ufc_event_id.is_(None))
    )
    return session.execute(statement).scalar_one()


def consolidate_observations(
    observations: Sequence[IdentityObservation],
) -> tuple[dict[int, tuple[str, str | None]], dict[int, tuple[str, ...]]]:
    """Reduz as observações a ``(candidatos a escrita, conflitantes)``. **Não escreve.**

    Um ``fighter_id`` com exatamente **um** ``ufc_fighter_id`` observado vira candidato; com
    mais de um, entra em ``conflitantes`` com **todos** os ids observados nomeados -- a fonte
    não concorda consigo mesma sobre quem ele é, e escolher um seria casar no escuro.

    O ``ufc_mma_id`` acompanha o id âncora: vale a primeira ocorrência **não nula**, porque a
    ausência é de um payload de evento, não do lutador. Medido nos 1.341 payloads do cache:
    nenhum ``FighterId`` tem dois ``MMAId`` distintos, e nenhum aparece ora nulo ora
    preenchido -- a regra é uma guarda barata, não um desempate frequente.
    """
    por_lutador: dict[int, list[IdentityObservation]] = {}
    for observation in observations:
        por_lutador.setdefault(observation.fighter_id, []).append(observation)

    resolvidos: dict[int, tuple[str, str | None]] = {}
    conflitantes: dict[int, tuple[str, ...]] = {}
    for fighter_id, do_lutador in por_lutador.items():
        ids = tuple(sorted({observation.ufc_fighter_id for observation in do_lutador}))
        if len(ids) > 1:
            conflitantes[fighter_id] = ids
            continue
        mma_id = next(
            (obs.ufc_mma_id for obs in do_lutador if obs.ufc_mma_id is not None),
            None,
        )
        resolvidos[fighter_id] = (ids[0], mma_id)
    return resolvidos, conflitantes


@dataclass(frozen=True)
class FighterIdBackfillReport:
    """Cobertura do backfill: o que casou, o que não casou e o que precisa de humano.

    As quatro listas guardam identificação **nominal**, não só contagem: quem lê o relatório
    precisa saber *quais* inspecionar. E os quatro diagnósticos são separados de propósito --

    - ``unmatched_bouts``: luta persistida cujo par de nomes não existe no card da fonte;
    - ``conflicting``: a fonte deu **dois** ids ao mesmo lutador persistido;
    - ``colliding``: o id observado já pertence a **outro** lutador persistido;
    - ``divergent``: o valor já persistido difere do observado.

    Colapsá-los num balde só esconderia qual dos quatro problemas a cobertura tem, e os quatro
    pedem correções diferentes.
    """

    fighters_in_window: int
    fighters_with_identifier: int
    assigned: int
    already_filled: int
    unmatched_bouts: tuple[tuple[int, str], ...]
    conflicting: tuple[tuple[int, str, tuple[str, ...]], ...]
    colliding: tuple[tuple[int, str, str, int], ...]
    divergent: tuple[tuple[int, str, str, str], ...]
    events_without_ufc_event_id: int

    @property
    def coverage(self) -> float:
        """Fração de lutadores da janela com id; base vazia -> ``0.0`` (sem divisão por zero)."""
        return (
            self.fighters_with_identifier / self.fighters_in_window
            if self.fighters_in_window
            else 0.0
        )


def _select_window_fighters(session: Session, window_start: date) -> list[Fighter]:
    """Lutadores com ao menos uma luta persistida em evento da janela, em ordem de id.

    É o denominador da cobertura, e é **mais largo** que o conjunto que recebe id: quem lutou
    num evento da janela que a Slice 02 não mapeou entra na conta e fica sem identificador --
    o que é exatamente o que a cobertura precisa mostrar.
    """
    statement = (
        select(Fighter)
        .join(BoutFighter, BoutFighter.fighter_id == Fighter.id)
        .join(Bout, Bout.id == BoutFighter.bout_id)
        .join(Event, Event.id == Bout.event_id)
        .where(Event.date >= window_start)
        .order_by(Fighter.id)
        .distinct()
    )
    return list(session.scalars(statement))


def _colliding_fighter_id(
    fighter: Fighter, ufc_fighter_id: str, index: Sequence[ExistingFighter]
) -> int | None:
    """O id externo já pertence a outro lutador persistido? Devolve o dono, ou ``None``.

    Consome o branch preferencial de ``match_fighter_id`` (RF-07): o candidato é montado a
    partir do **próprio** lutador alvo, então o fallback por nome + DOB, quando o id ainda não
    está em ninguém, devolve o próprio alvo -- e não um falso conflito.

    ``AmbiguousFighterMatchError`` vinda do fallback **não** é colisão: ela diz que o *nome* não
    decide, e aqui o nome não precisa decidir nada -- o alvo já é conhecido e nenhum persistido
    carrega o id. Acontece com homônimos sem DOB, que é justamente o caso que o id veio
    resolver.
    """
    candidate = FighterCandidate(
        name=fighter.name,
        date_of_birth=fighter.date_of_birth,
        ufc_fighter_id=ufc_fighter_id,
    )
    try:
        matched = match_fighter_id(candidate, index)
    except AmbiguousFighterMatchError:
        return None
    return matched if matched is not None and matched != fighter.id else None


def run_fighter_id_backfill(
    session: Session,
    client: UfcOfficialClient | None,
    cache: OfficialEventCache,
    *,
    window_start: date = OFFICIAL_WINDOW_START,
) -> FighterIdBackfillReport:
    """Preenche ``ufc_fighter_id``/``ufc_mma_id`` dos lutadores da janela. Commit é do chamador.

    Idempotente por construção: o campo só é atribuído quando **muda**, então a reexecução não
    emite ``UPDATE`` nenhum. Valor já preenchido e divergente do observado é **reportado**,
    nunca sobrescrito -- a fonte corrige o que está nulo, não o que alguém já decidiu.
    """
    collection = collect_identity_observations(session, client, cache, window_start=window_start)
    resolvidos, conflitantes = consolidate_observations(collection.observations)

    fighters = _select_window_fighters(session, window_start)
    por_id = {fighter.id: fighter for fighter in fighters}
    index = load_existing_fighters(session)
    posicao = {existing.id: i for i, existing in enumerate(index)}

    assigned = 0
    already_filled = 0
    colliding: list[tuple[int, str, str, int]] = []
    divergent: list[tuple[int, str, str, str]] = []

    for fighter_id, (ufc_fighter_id, ufc_mma_id) in resolvidos.items():
        fighter = por_id[fighter_id]
        if fighter.ufc_fighter_id is not None:
            if fighter.ufc_fighter_id == ufc_fighter_id:
                already_filled += 1
            else:
                divergent.append((fighter.id, fighter.name, fighter.ufc_fighter_id, ufc_fighter_id))
            continue

        dono = _colliding_fighter_id(fighter, ufc_fighter_id, index)
        if dono is not None:
            colliding.append((fighter.id, fighter.name, ufc_fighter_id, dono))
            continue

        fighter.ufc_fighter_id = ufc_fighter_id
        fighter.ufc_mma_id = ufc_mma_id
        assigned += 1
        # Mantém o índice em memória coerente: o próximo lutador precisa enxergar este id já
        # atribuído, senão dois lutadores da mesma execução receberiam o mesmo identificador.
        index[posicao[fighter.id]] = replace(
            index[posicao[fighter.id]], ufc_fighter_id=ufc_fighter_id
        )

    report = FighterIdBackfillReport(
        fighters_in_window=len(fighters),
        fighters_with_identifier=sum(1 for f in fighters if f.ufc_fighter_id is not None),
        assigned=assigned,
        already_filled=already_filled,
        unmatched_bouts=collection.unmatched_bouts,
        conflicting=tuple(
            (fighter_id, por_id[fighter_id].name, ids) for fighter_id, ids in conflitantes.items()
        ),
        colliding=tuple(colliding),
        divergent=tuple(divergent),
        events_without_ufc_event_id=collection.events_without_ufc_event_id,
    )
    _log_coverage(report, collection)
    return report


def _log_coverage(report: FighterIdBackfillReport, collection: IdentityCollection) -> None:
    """Emite o relatório via ``logging`` (``print`` é proibido, regra T20), um caso por linha."""
    logger.info(
        "Resumo do backfill de identificadores da fonte oficial: lutadores da janela=%d; "
        "cobertura %d/%d (%.1f%%); atribuídos=%d; já preenchidos=%d; conflitantes=%d; "
        "colisões=%d; divergentes=%d; lutas não casadas=%d; eventos da janela sem "
        "ufc_event_id=%d",
        report.fighters_in_window,
        report.fighters_with_identifier,
        report.fighters_in_window,
        report.coverage * 100,
        report.assigned,
        report.already_filled,
        len(report.conflicting),
        len(report.colliding),
        len(report.divergent),
        len(report.unmatched_bouts),
        report.events_without_ufc_event_id,
    )
    for fighter_id, nome, ids in report.conflicting:
        logger.warning(
            "Lutador %r (id %d) em CONFLITO: a fonte o identifica como %s em lutas diferentes; "
            "nada foi escrito para ele.",
            nome,
            fighter_id,
            ", ".join(ids),
        )
    for fighter_id, nome, ufc_fighter_id, dono in report.colliding:
        logger.warning(
            "Lutador %r (id %d) pulado por COLISÃO: o identificador %s já pertence ao lutador "
            "persistido %d; nada foi escrito para ele.",
            nome,
            fighter_id,
            ufc_fighter_id,
            dono,
        )
    for fighter_id, nome, persistido, observado in report.divergent:
        logger.warning(
            "Lutador %r (id %d) DIVERGENTE: persistido %s, observado %s na fonte; o valor "
            "persistido foi MANTIDO (nunca sobrescrever em silêncio).",
            nome,
            fighter_id,
            persistido,
            observado,
        )
    for bout_id, evento in report.unmatched_bouts:
        logger.info(
            "Luta %d (%s) NÃO CASADA: o par de nomes não existe no card da fonte; pulada.",
            bout_id,
            evento,
        )
    for event_id, nome in collection.events_without_payload:
        logger.warning(
            "Evento %r (id %d) tem ufc_event_id mas a fonte não devolveu o payload nesta "
            "execução; nenhuma luta dele foi observada.",
            nome,
            event_id,
        )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do backfill."""
    parser = argparse.ArgumentParser(
        description=(
            "Preenche fighters.ufc_fighter_id/ufc_mma_id a partir dos cantos das lutas dos "
            "eventos da janela (2010-03-21 em diante) já mapeados na fonte oficial da UFC."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=_DEFAULT_CACHE_DIR,
        help=(
            "Diretório do cache em disco por eventId (o mesmo da descoberta). "
            "Um cache já materializado torna a execução offline."
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
    """``python -m ingestion.ufc_official.fighter_ids [--cache-dir DIR] [--fixture-dir DIR]``.

    **Sem gate de quota**: a fonte é gratuita e não autenticada, ao contrário da Cito -- a
    ausência do ``--confirmar-gasto-de-quota`` é deliberada, não esquecimento. Commita só no
    sucesso.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)

    offline = args.fixture_dir is not None
    cache = OfficialEventCache(args.fixture_dir if offline else args.cache_dir)
    client = None if offline else UfcOfficialClient()

    with SessionLocal() as session:
        run_fighter_id_backfill(session, client, cache)
        session.commit()


if __name__ == "__main__":
    main()
