"""Canto autoritativo da fonte oficial da UFC (SPEC 008, Slice 04).

Substitui, na janela de ``OFFICIAL_WINDOW_START`` em diante e onde a fonte cobre, o canto que
**nós** derivamos -- a arte promocional do card e o fallback determinístico do M6 -- pelo canto
que a fonte publica como campo de primeira classe.

Comparar vem antes de escrever, estruturalmente (RF-02)
-------------------------------------------------------
A comparação (``run_corner_comparison``) é uma função de **leitura**: percorre a janela, casa
cada luta e devolve o diagnóstico. A escrita (``apply_official_corners``) só aceita uma
``CornerComparison`` já produzida -- não refaz a varredura e não busca payload. É por isso que
"inspecionar antes de escrever" não depende de o operador lembrar de olhar o relatório: sem a
comparação em mãos não há o que aplicar, e o modo default da linha de comando não escreve.

O motivo é medido: nas 7.507 lutas da janela a fonte concorda com a nossa base em **99,72%**,
e as **21** discordâncias estão todas em linhas cujo canto nós derivamos (17 do fallback
determinístico, que acerta 39,29%; 2 da arte; 2 sem cache local). Nas 6.991 linhas semeadas do
Kaggle a concordância é de **100,00%** em quinze anos consecutivos. A fonte não só substitui a
heurística: corrige erro nosso.

Por que não existe aqui um ``official_corner(token)``
-----------------------------------------------------
O vocabulário do canto (``"Red"``/``"Blue"``) é traduzido para ``apps.bouts.enums.Corner`` **na
borda**, pelo DTO da Sprint 008-01 (``UfcOfficialFighter._map_corner``), que já falha alto em
rótulo desconhecido (RF-10). Redeclarar o mapa aqui daria duas tabelas do mesmo vocabulário,
que divergiriam em silêncio na primeira correção. O que este módulo acrescenta é o que o DTO
não sabe: que os dois cantos de uma luta formam um **par**, e que um par com dois lados iguais
não diz qual é qual.

A arte e o fallback continuam vivos
-----------------------------------
A aposentadoria é de **precedência**, não de código. ``assign_corners``, ``art_side`` e
``assign_deterministic_corners`` permanecem em ``ingestion.cito.gap_sync``: a arte se sustenta
em 99,58% num conjunto independente e é o plano B documentado se a fonte oficial sair do ar, e
o fallback continua sendo o último recurso onde a fonte não cobre -- inclusive fora da janela,
que esta slice não toca. O que muda é que, dentro da janela e onde a fonte cobre, o canto final
persistido nunca é o deles.

``source`` não muda, ``winner_id`` não é tocado
-----------------------------------------------
Corrigir o canto de uma linha semeada do Kaggle **mantém** ``source="kaggle"``: ``source`` é a
origem da **linha**, não de cada campo (decisão do humano em 2026-09-01, registrada no
CLAUDE.md). E ``bouts.winner_id`` é um ``fighter_id``: trocar o rótulo de canto muda em qual
lado o vencedor cai, nunca quem ele é.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.enums import Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.cache import OfficialEventCache
from ingestion.ufc_official.client import UfcOfficialClient
from ingestion.ufc_official.dto import (
    UfcOfficialError,
    UfcOfficialFight,
    is_absent_event_payload,
    parse_event,
)
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

# Cantos por luta na fonte. Medido em 8.943 lutas de 808 eventos do UFC: 100% têm exatamente
# dois, um de cada lado. Uma luta fora dessa forma não é adivinhada.
_CORNERS_PER_BOUT = 2

# Diretório default do cache em disco por ``eventId`` -- o mesmo da Slice 02, de propósito: o
# payload que a descoberta já baixou é exatamente o que esta comparação precisa.
_DEFAULT_CACHE_DIR = Path(".cache") / "ufc_official"

# Os dois motivos pelos quais um evento inteiro fica fora da comparação. Textos fixos porque
# entram no relatório e são conferidos por teste -- um motivo redigido caso a caso viraria
# ruído no lugar de diagnóstico.
_SEM_IDENTIFICADOR = "sem ufc_event_id"
_SEM_PAYLOAD = "sem payload na fonte"


class OfficialCornerError(RuntimeError):
    """O par de cantos da fonte é malformado: não são dois, ou são dois do mesmo lado.

    Distinta de ``UfcOfficialContractError``: aquela diz que a **forma** do payload mudou (e a
    ingestão precisa parar), esta diz que **esta luta** não tem par legível. Quem varre a
    janela conta a luta como não coberta e segue -- o problema de uma luta não custa as outras.
    """


def official_corners(fight: UfcOfficialFight) -> dict[str, Corner]:
    """Par de cantos da luta oficial, indexado pelo identificador externo do lutador.

    A chave é ``str`` porque é assim que ``fighters.ufc_fighter_id`` guarda o identificador:
    id externo é opaco e nunca entra em aritmética.

    Levanta ``OfficialCornerError`` quando a luta não tem exatamente dois cantos, quando os
    dois trazem o mesmo lado, ou quando o mesmo lutador aparece nos dois -- nunca escolhe um
    lado por desempate. O canto é o **alvo** do modelo; um palpite aqui grava dado falso com
    aparência de dado.
    """
    if len(fight.fighters) != _CORNERS_PER_BOUT:
        raise OfficialCornerError(
            f"A luta {fight.fight_id} da fonte oficial tem {len(fight.fighters)} cantos "
            f"(esperado {_CORNERS_PER_BOUT}); o par não é legível."
        )
    corners = {str(fighter.fighter_id): fighter.corner for fighter in fight.fighters}
    if len(corners) != _CORNERS_PER_BOUT or len(set(corners.values())) != _CORNERS_PER_BOUT:
        raise OfficialCornerError(
            f"A luta {fight.fight_id} da fonte oficial não tem um lado de cada: "
            f"{sorted((id_externo, canto.value) for id_externo, canto in corners.items())}."
        )
    return corners


@dataclass(frozen=True)
class CornerDivergence:
    """Uma linha de ``bout_fighters`` cujo canto persistido discorda da fonte oficial.

    Carrega o que o humano precisa para julgar **sem abrir o banco** (RF-02): evento, data e os
    dois lutadores pelo nome, mais a procedência da linha (``source``). Uma divergência que só
    dissesse "a luta 4.312 diverge" seria um número, não um caso inspecionável.

    A divergência vem sempre em **par** -- uma linha por canto da mesma luta --, porque os dois
    pares (o persistido e o oficial) têm um lado de cada; um lado só divergindo seria par
    malformado, que a comparação classifica como não coberto.
    """

    bout_id: int
    event_name: str
    event_date: date
    fighter_id: int
    fighter_name: str
    opponent_name: str
    persisted_corner: Corner
    official_corner: Corner
    persisted_source: str


@dataclass(frozen=True)
class CornerComparison:
    """Diagnóstico read-only da janela: o que bate, o que diverge, o que a fonte não cobre.

    ``uncovered_bouts`` e ``uncovered_events`` são diagnósticos **diferentes** e ficam
    separados de propósito: o primeiro é luta cuja chave de casamento não fechou (lutador sem
    identificador externo, luta ausente do card, par malformado dos dois lados), o segundo é
    evento inteiro que a fonte não alcança (sem ``ufc_event_id`` ou sem payload nesta
    execução). Colapsá-los esconderia qual dos dois problemas a cobertura tem, e os dois pedem
    correções diferentes.

    ``window_start`` é **diagnóstico**, não autoridade: quem decide o que pode ser escrito é a
    constante ``OFFICIAL_WINDOW_START``, checada de novo dentro do escritor.
    """

    compared_bouts: int
    agreeing_bouts: int
    divergences: tuple[CornerDivergence, ...]
    uncovered_bouts: tuple[int, ...]
    uncovered_events: tuple[tuple[int, str, str], ...]
    window_start: date

    @property
    def diverging_bouts(self) -> int:
        """Quantas **lutas** divergem (a divergência vem em par, uma linha por canto)."""
        return len({divergencia.bout_id for divergencia in self.divergences})


@dataclass(frozen=True)
class _PersistedCorner:
    """Um canto persistido reduzido ao que a comparação precisa."""

    fighter_id: int
    fighter_name: str
    ufc_fighter_id: str | None
    corner: Corner
    source: str


@dataclass(frozen=True)
class _PersistedBout:
    """Uma luta persistida com os seus dois cantos, na ordem em que a query os devolveu."""

    bout_id: int
    corners: tuple[_PersistedCorner, ...]

    @property
    def official_key(self) -> frozenset[str] | None:
        """Par **não-ordenado** de ``ufc_fighter_id``, ou ``None`` quando a chave não fecha.

        Mesma disciplina de chave natural order-independent do ``upsert_bout`` do M1. Um canto
        sem identificador externo, ou os dois com o mesmo, não formam chave -- e sem chave a
        luta é não coberta, nunca casada por aproximação.
        """
        ids = [corner.ufc_fighter_id for corner in self.corners]
        if len(ids) != _CORNERS_PER_BOUT or any(id_externo is None for id_externo in ids):
            return None
        chave = frozenset(id_externo for id_externo in ids if id_externo is not None)
        return chave if len(chave) == _CORNERS_PER_BOUT else None

    @property
    def has_one_corner_per_side(self) -> bool:
        """A base tem um lado de cada nesta luta?

        Não há constraint em ``(bout_id, corner)`` -- a unicidade de ``bout_fighters`` é
        ``(bout_id, fighter_id)`` (ADR 0001) --, então duas linhas do mesmo lado são possíveis.
        Nesse estado não há o que comparar: qual dos dois lados a fonte estaria corrigindo?
        """
        return (
            len(self.corners) == _CORNERS_PER_BOUT
            and len({corner.corner for corner in self.corners}) == _CORNERS_PER_BOUT
        )


def _persisted_bouts(session: Session, event: Event) -> list[_PersistedBout]:
    """Lutas persistidas do evento com os dois cantos, numa query só (sem N+1 por luta)."""
    statement = (
        select(
            Bout.id,
            Fighter.id,
            Fighter.name,
            Fighter.ufc_fighter_id,
            BoutFighter.corner,
            BoutFighter.source,
        )
        .join(BoutFighter, BoutFighter.bout_id == Bout.id)
        .join(Fighter, Fighter.id == BoutFighter.fighter_id)
        .where(Bout.event_id == event.id)
        .order_by(Bout.id, BoutFighter.id)
    )
    por_luta: dict[int, list[_PersistedCorner]] = {}
    for bout_id, fighter_id, name, ufc_fighter_id, corner, source in session.execute(statement):
        por_luta.setdefault(bout_id, []).append(
            _PersistedCorner(
                fighter_id=fighter_id,
                fighter_name=name,
                ufc_fighter_id=ufc_fighter_id,
                corner=corner,
                source=source,
            )
        )
    return [
        _PersistedBout(bout_id=bout_id, corners=tuple(corners))
        for bout_id, corners in por_luta.items()
    ]


def _official_corners_by_key(
    official_bouts: Sequence[UfcOfficialFight],
) -> dict[frozenset[str], dict[str, Corner]]:
    """Indexa o card oficial pelo par não-ordenado de identificadores.

    Luta com par ilegível (``OfficialCornerError``) é **deixada fora do índice**: ela deixa de
    casar e a luta persistida correspondente cai em ``uncovered_bouts``. Derrubar a varredura
    inteira por causa de uma luta custaria as outras 7.506 da janela.
    """
    indexado: dict[frozenset[str], dict[str, Corner]] = {}
    for fight in official_bouts:
        try:
            corners = official_corners(fight)
        except OfficialCornerError as exc:
            logger.warning("Luta da fonte oficial ignorada na comparação: %s", exc)
            continue
        indexado[frozenset(corners)] = corners
    return indexado


def compare_event_corners(
    session: Session, event: Event, official_bouts: Sequence[UfcOfficialFight]
) -> CornerComparison:
    """Compara o canto persistido de cada luta do evento com o da fonte oficial.

    Leitura pura -- nenhuma escrita, nenhum ``flush``, nenhuma mutação de objeto mapeado. Casa
    a luta pelo par **não-ordenado** de ``ufc_fighter_id`` (é a razão de a Slice 03 existir:
    nada de casar por nome aqui); par que não fecha -- lutador sem identificador externo, luta
    ausente do card, dois cantos iguais de qualquer um dos lados -- entra em
    ``uncovered_bouts``, nunca em ``divergences``. Nunca casar no escuro.
    """
    do_card = _official_corners_by_key(official_bouts)
    divergences: list[CornerDivergence] = []
    uncovered: list[int] = []
    compared = 0
    agreeing = 0

    for persistida in _persisted_bouts(session, event):
        chave = persistida.official_key
        oficiais = do_card.get(chave) if chave is not None else None
        if oficiais is None or not persistida.has_one_corner_per_side:
            uncovered.append(persistida.bout_id)
            continue

        compared += 1
        da_luta = [
            _divergencia(persistida, canto, event, oficiais)
            for canto in persistida.corners
            if oficiais[str(canto.ufc_fighter_id)] is not canto.corner
        ]
        if da_luta:
            divergences.extend(da_luta)
        else:
            agreeing += 1

    return CornerComparison(
        compared_bouts=compared,
        agreeing_bouts=agreeing,
        divergences=tuple(divergences),
        uncovered_bouts=tuple(uncovered),
        uncovered_events=(),
        window_start=OFFICIAL_WINDOW_START,
    )


def _divergencia(
    bout: _PersistedBout, corner: _PersistedCorner, event: Event, oficiais: dict[str, Corner]
) -> CornerDivergence:
    """Monta a divergência de um canto, nomeando o adversário do outro lado da mesma luta."""
    adversario = next(outro for outro in bout.corners if outro.fighter_id != corner.fighter_id)
    return CornerDivergence(
        bout_id=bout.bout_id,
        event_name=event.name,
        event_date=event.date,
        fighter_id=corner.fighter_id,
        fighter_name=corner.fighter_name,
        opponent_name=adversario.fighter_name,
        persisted_corner=corner.corner,
        official_corner=oficiais[str(corner.ufc_fighter_id)],
        persisted_source=corner.source,
    )


def _fetch_payload(client: UfcOfficialClient | None, event_id: int) -> object | None:
    """Payload cru de um id; ``None`` quando o id não existe ou a busca falha.

    Terceira ocorrência desta forma no pacote (``discovery`` e ``fighter_ids`` têm as outras
    duas). A rule of three autorizaria a extração, e ela **não** é feita aqui de propósito: as
    três diferem no que fazem com a falha (a descoberta conta o buraco na fronteira, o backfill
    de identidade pula o evento, esta conta o evento como não coberto e o nomeia). Extrair um
    corpo comum obrigaria a parametrizar o tratamento -- mais elementos, não menos.

    ``client=None`` é o **modo offline**: nada é buscado e a comparação enxerga apenas o que já
    está no cache. É o modo dos testes e o modo natural desta slice, já que a descoberta da
    Slice 02 materializa os 1.354 payloads antes.
    """
    if client is None:
        return None
    try:
        payload = client.fetch_event_payload(event_id)
    except UfcOfficialError as exc:
        logger.warning("Evento %d pulado (falha transitória na fonte): %s", event_id, exc)
        return None
    return None if is_absent_event_payload(payload) else payload


def _window_events(session: Session, window_start: date) -> list[Event]:
    """Eventos da janela em ordem cronológica -- **todos**, com ou sem ``ufc_event_id``.

    Os sem identificador entram porque precisam ser **contados e nomeados** como não cobertos
    (CA-03): o teto da cobertura é parte do diagnóstico, não uma ausência silenciosa.
    """
    statement = select(Event).where(Event.date >= window_start).order_by(Event.date, Event.id)
    return list(session.scalars(statement))


def run_corner_comparison(
    session: Session,
    client: UfcOfficialClient | None,
    cache: OfficialEventCache,
    *,
    window_start: date = OFFICIAL_WINDOW_START,
) -> CornerComparison:
    """Compara a janela inteira e **reporta**; nunca escreve (RF-02).

    É o passo que o humano lê antes de autorizar a escrita. Percorre os eventos da janela em
    ordem cronológica, busca o payload oficial de cada um (pelo cache da Slice 02, então um
    cache materializado torna a execução offline) e agrega o diagnóstico luta a luta.

    ``window_start`` entra por parâmetro só para o teste poder estreitá-la; o default é a
    constante e a linha de comando **não** a expõe. Alargá-la aqui não abre caminho para
    escrita nenhuma: o escritor checa a constante de novo, por conta própria (CA-08).
    """
    compared = 0
    agreeing = 0
    divergences: list[CornerDivergence] = []
    uncovered_bouts: list[int] = []
    uncovered_events: list[tuple[int, str, str]] = []

    for event in _window_events(session, window_start):
        if event.ufc_event_id is None:
            uncovered_events.append((event.id, event.name, _SEM_IDENTIFICADOR))
            continue
        ufc_event_id = int(event.ufc_event_id)
        payload, _ = cache.get_or_fetch(
            ufc_event_id, lambda id_alvo: _fetch_payload(client, id_alvo)
        )
        # A ausência é reconhecida **também** no que veio do cache, e não só no que veio da
        # rede: a fonte responde 200 com envelope vazio para id inexistente, e um diretório de
        # capturas lido como cache (o modo offline) pode ter essa resposta gravada. Validar sem
        # esta checagem transformaria "este id não existe" em quebra de contrato.
        if payload is None or is_absent_event_payload(payload):
            uncovered_events.append((event.id, event.name, _SEM_PAYLOAD))
            continue

        do_evento = compare_event_corners(
            session, event, parse_event(payload, event_id=ufc_event_id).fight_card
        )
        compared += do_evento.compared_bouts
        agreeing += do_evento.agreeing_bouts
        divergences.extend(do_evento.divergences)
        uncovered_bouts.extend(do_evento.uncovered_bouts)

    comparison = CornerComparison(
        compared_bouts=compared,
        agreeing_bouts=agreeing,
        divergences=tuple(divergences),
        uncovered_bouts=tuple(uncovered_bouts),
        uncovered_events=tuple(uncovered_events),
        window_start=window_start,
    )
    log_comparison(comparison)
    return comparison


def log_comparison(comparison: CornerComparison) -> None:
    """Emite o diagnóstico via ``logging`` (``print`` é proibido, regra T20), um caso por linha.

    A divergência é logada com evento, data, os dois lutadores, os dois cantos e o ``source``
    da linha: é este texto que vira a lista inspecionada no relatório da sprint (RF-02), e sem
    os seis campos ele não seria inspecionável.
    """
    logger.info(
        "Comparação de canto com a fonte oficial (janela a partir de %s): lutas comparadas=%d; "
        "concordantes=%d; divergentes=%d (em %d luta(s)); lutas não cobertas=%d; eventos não "
        "cobertos=%d",
        comparison.window_start,
        comparison.compared_bouts,
        comparison.agreeing_bouts,
        len(comparison.divergences),
        comparison.diverging_bouts,
        len(comparison.uncovered_bouts),
        len(comparison.uncovered_events),
    )
    for divergencia in comparison.divergences:
        logger.warning(
            "DIVERGÊNCIA de canto: evento %r (%s), luta %d, %r x %r -- %r está no %s na base e "
            "no %s na fonte oficial; source da linha=%r.",
            divergencia.event_name,
            divergencia.event_date,
            divergencia.bout_id,
            divergencia.fighter_name,
            divergencia.opponent_name,
            divergencia.fighter_name,
            divergencia.persisted_corner.value,
            divergencia.official_corner.value,
            divergencia.persisted_source,
        )
    for event_id, nome, motivo in comparison.uncovered_events:
        logger.info("Evento %r (id %d) NÃO COBERTO pela fonte oficial: %s.", nome, event_id, motivo)


# --------------------------------------------------------------------------- #
# Escrita: consome a comparação já produzida -- nunca revarre, nunca busca payload.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CornerUpdateResult:
    """O que a aplicação escreveu -- base do resumo e da prova de idempotência.

    ``bouts_updated`` e ``rows_updated`` respondem perguntas diferentes: a divergência vem em
    par, então uma luta corrigida conta uma vez na primeira e duas na segunda.

    ``rows_already_official`` é a linha que a comparação apontava como divergente e que já
    estava no canto da fonte quando a escrita chegou -- é o que torna a reexecução com uma
    comparação **velha** um no-op observável, em vez de um ``UPDATE`` que reescreve o mesmo
    valor e some do relatório.
    """

    bouts_updated: int
    rows_updated: int
    rows_already_official: int
    skipped_out_of_window: tuple[int, ...]
    missing_rows: tuple[tuple[int, int], ...]


def apply_official_corners(session: Session, comparison: CornerComparison) -> CornerUpdateResult:
    """Adota o canto da fonte nas lutas divergentes da comparação **recebida**.

    Não refaz a varredura e não busca payload: é assim que o RF-02 fica estrutural em vez de
    depender de o operador lembrar de olhar o relatório. Antes de escrever, loga cada
    divergência que vai aplicar.

    Só toca ``bout_fighters.corner``. ``source`` é preservado (origem da LINHA, não do campo) e
    ``bouts.winner_id`` nunca é tocado. Divergência de evento anterior a
    ``OFFICIAL_WINDOW_START`` é pulada e contada, não escrita (CA-08) -- e a trava consulta a
    **constante**, não ``comparison.window_start``: uma comparação obtida com a janela alargada
    não pode alargar a autoridade da escrita junto.

    Não commita -- o commit é do chamador.
    """
    aplicaveis = [
        divergencia
        for divergencia in comparison.divergences
        if divergencia.event_date >= OFFICIAL_WINDOW_START
    ]
    fora_da_janela = tuple(
        divergencia.bout_id
        for divergencia in comparison.divergences
        if divergencia.event_date < OFFICIAL_WINDOW_START
    )
    _log_before_writing(aplicaveis, fora_da_janela)

    linhas = _rows_by_key(session, aplicaveis)
    rows_updated = 0
    already_official = 0
    ausentes: list[tuple[int, int]] = []
    bouts_tocadas: set[int] = set()

    for divergencia in aplicaveis:
        chave = (divergencia.bout_id, divergencia.fighter_id)
        linha = linhas.get(chave)
        if linha is None:
            ausentes.append(chave)
            continue
        if linha.corner is divergencia.official_corner:
            # A comparação está velha em relação ao banco (tipicamente uma reexecução sobre a
            # mesma comparação). Nada a escrever: reemitir o mesmo valor inflaria a contagem e
            # esconderia a idempotência atrás de um UPDATE que não muda nada.
            already_official += 1
            continue
        linha.corner = divergencia.official_corner
        rows_updated += 1
        bouts_tocadas.add(divergencia.bout_id)

    result = CornerUpdateResult(
        bouts_updated=len(bouts_tocadas),
        rows_updated=rows_updated,
        rows_already_official=already_official,
        skipped_out_of_window=fora_da_janela,
        missing_rows=tuple(ausentes),
    )
    _log_update(result)
    return result


def _rows_by_key(
    session: Session, divergences: Sequence[CornerDivergence]
) -> dict[tuple[int, int], BoutFighter]:
    """Carrega as linhas de ``bout_fighters`` das divergências numa query só (sem N+1).

    A chave é ``(bout_id, fighter_id)``, que é a unicidade da tabela (ADR 0001) -- e não
    ``(bout_id, corner)``, que não tem constraint nenhuma. Por isso trocar os dois cantos na
    mesma transação é seguro e não exige valor temporário.
    """
    if not divergences:
        return {}
    bout_ids = {divergencia.bout_id for divergencia in divergences}
    statement = select(BoutFighter).where(BoutFighter.bout_id.in_(bout_ids))
    return {(linha.bout_id, linha.fighter_id): linha for linha in session.scalars(statement)}


def _log_before_writing(
    aplicaveis: Sequence[CornerDivergence], fora_da_janela: Sequence[int]
) -> None:
    """Nomeia, **antes** de escrever, cada divergência que a aplicação vai adotar."""
    for divergencia in aplicaveis:
        logger.info(
            "Adotando o canto da fonte oficial: evento %r (%s), luta %d, %r sai do %s e vai "
            "para o %s (source da linha=%r, preservado).",
            divergencia.event_name,
            divergencia.event_date,
            divergencia.bout_id,
            divergencia.fighter_name,
            divergencia.persisted_corner.value,
            divergencia.official_corner.value,
            divergencia.persisted_source,
        )
    for bout_id in dict.fromkeys(fora_da_janela):
        logger.warning(
            "Luta %d RECUSADA pela trava de janela: o evento é anterior a %s, e nada antes "
            "dessa data é escrito por esta ingestão (RF-03).",
            bout_id,
            OFFICIAL_WINDOW_START,
        )


def _log_update(result: CornerUpdateResult) -> None:
    """Emite o resumo da escrita via ``logging`` (``print`` é proibido, regra T20)."""
    logger.info(
        "Canto autoritativo aplicado: lutas corrigidas=%d; linhas escritas=%d; linhas que já "
        "estavam no canto da fonte=%d; linhas recusadas fora da janela=%d; linhas ausentes=%d",
        result.bouts_updated,
        result.rows_updated,
        result.rows_already_official,
        len(result.skipped_out_of_window),
        len(result.missing_rows),
    )
    for bout_id, fighter_id in result.missing_rows:
        logger.warning(
            "Divergência da luta %d / lutador %d ignorada: a linha de bout_fighters não existe "
            "mais; a comparação está velha em relação ao banco.",
            bout_id,
            fighter_id,
        )


# --------------------------------------------------------------------------- #
# CLI: compara e reporta por padrão; escreve só com --aplicar, e commita no sucesso.
# --------------------------------------------------------------------------- #


def run_corner_command(
    session: Session,
    client: UfcOfficialClient | None,
    cache: OfficialEventCache,
    *,
    apply: bool,
) -> tuple[CornerComparison, CornerUpdateResult | None]:
    """Despacho do comando: compara sempre, escreve só quando ``apply``.

    Existe como função para que "o modo default não escreve" seja **verificável por teste**, e
    não uma promessa da docstring do ``main``. A comparação é a mesma nos dois modos: a escrita
    é literalmente o passo seguinte sobre o diagnóstico já produzido (RF-02).
    """
    comparison = run_corner_comparison(session, client, cache)
    if not apply:
        logger.info(
            "Modo somente leitura: nada foi escrito. Inspecione as %d divergência(s) acima e, "
            "para adotar o canto da fonte, rode de novo com --aplicar.",
            len(comparison.divergences),
        )
        return comparison, None
    return comparison, apply_official_corners(session, comparison)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do canto autoritativo.

    A janela **não** é exposta: ``OFFICIAL_WINDOW_START`` é limitação de fonte medida, e um
    corte que se pode afrouxar por conveniência deixa de ser garantia.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Compara o canto persistido com o da fonte oficial da UFC na janela (2010-03-21 em "
            "diante) e reporta as divergências. Sem --aplicar, nada é escrito."
        ),
    )
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help=(
            "Adota o canto da fonte nas divergências encontradas. Sem esta flag o comando é "
            "somente leitura -- inspecione o relatório antes (RF-02)."
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
    """Entrypoint ``python -m ingestion.ufc_official.corner`` (canto autoritativo).

    ``[--aplicar] [--cache-dir DIR] [--fixture-dir DIR]``.

    **Sem gate de quota**: a fonte é gratuita e não autenticada, ao contrário da Cito -- a
    ausência do ``--confirmar-gasto-de-quota`` é deliberada, não esquecimento.

    Commita **só** quando houve escrita e ela terminou sem erro. No modo default não há o que
    commitar: a comparação não toca em nada.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)

    offline = args.fixture_dir is not None
    cache = OfficialEventCache(args.fixture_dir if offline else args.cache_dir)
    client = None if offline else UfcOfficialClient()

    with SessionLocal() as session:
        run_corner_command(session, client, cache, apply=args.aplicar)
        if args.aplicar:
            session.commit()


if __name__ == "__main__":
    main()
