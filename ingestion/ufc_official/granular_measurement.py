"""Medição do portão da Slice 07: a fonte oficial reproduz o granular que já temos?

**Módulo de medição, não de produção.** Ele não escreve nada: nenhuma linha aqui faz ``add``,
``merge``, ``update``, ``delete`` ou ``commit``. A pergunta que ele responde -- "a fonte oficial
da UFC pode substituir a Cito no round-a-round?" -- é decidida por número, e a decisão vive no
relatório da sprint, não no código.

Custo
-----
**Zero quota da Cito.** Este módulo não importa nada de ``ingestion.cito``: a fonte oficial é
gratuita e sem autenticação, e o outro lado da comparação é o **nosso banco**. Foi o próprio
CLAUDE.md que fixou a regra ("medir antes de gastar quota"), e esta medição é o caso em que ela
custa literalmente nada.

Duas comparações distintas, e só uma decide o portão
----------------------------------------------------
As duas tabelas do granular têm **linhagens diferentes** no mesmo evento:

======================= ======================================= ============================
Bloco persistido        Linhagem real dos números               O que a comparação responde
======================= ======================================= ============================
``bout_fighters``       **Kaggle** (o backfill da Cito só       "A fonte oficial concorda com
(totais por canto)      preencheu campos nulos, e ``source``    o Kaggle?" -- contexto
                        continua ``"kaggle"``)
``bout_fighter_rounds`` **Cito** integral, ``source="cito"``    "A fonte oficial pode
(round-a-round)                                                 substituir a Cito?" -- **é
                                                                esta que decide o portão**
======================= ======================================= ============================

Por isso o relatório sai **separado por bloco e por linhagem**. Um percentual agregado dos dois
responderia a pergunta errada.

Chave de casamento
------------------
``(nome normalizado do lutador, round)`` dentro do evento -- **nunca o canto**, que é exatamente
o campo sob suspeita nas duas fontes e o que a Sprint 008-04 corrige. O ``ufc_fighter_id``
também não é usado: é entrega de outra sprint e ainda não está preenchido.

Regras da contagem
------------------
- **Ausência não é divergência**: valor nulo em qualquer um dos lados tira a linha do
  denominador daquele campo. Contá-la como discordância inventaria divergência.
- **Linha sem contraparte não é divergência**: é contada como não casada, à parte.
- **Nunca casar no escuro**: nome que casa com mais de uma linha do mesmo evento e round
  levanta ``AmbiguousGranularMatchError``.
- **Janela** (RF-03/CA-08 da SPEC): evento anterior a ``OFFICIAL_WINDOW_START`` não é lido.

Uso
---
``uv run python -m ingestion.ufc_official.granular_measurement --event-slug <cito_slug>``
(``--corroboracao N`` acrescenta os N eventos escolhidos pela regra determinística).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.normalize import normalize_name
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.client import UfcOfficialClient
from ingestion.ufc_official.dto import (
    UfcOfficialFightGranular,
    UfcOfficialStatLine,
)
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

# Os 22 campos de estatística que ``bout_fighter_rounds`` guarda e que a fonte oficial também
# publica. São os mesmos nomes dos campos do model e dos do ``UfcOfficialStatLine``, o que
# permite comparar os dois lados sem tabela de tradução -- a tradução acontece no alias do DTO.
#
# A ordem é a do critério pré-comprometido no relatório da sprint: os 8 campos base primeiro,
# depois os 7 pares de split.
MEASURED_FIELDS: tuple[str, ...] = (
    "knockdowns",
    "sig_strikes_landed",
    "sig_strikes_attempted",
    "takedowns_landed",
    "takedowns_attempted",
    "submission_attempts",
    "control_time_seconds",
    "reversals",
    "total_strikes_landed",
    "total_strikes_attempted",
    "head_landed",
    "head_attempted",
    "body_landed",
    "body_attempted",
    "leg_landed",
    "leg_attempted",
    "distance_landed",
    "distance_attempted",
    "clinch_landed",
    "clinch_attempted",
    "ground_landed",
    "ground_attempted",
)

# Rótulos dos dois blocos, usados no relatório e na quebra por linhagem.
BLOCK_TOTALS = "bout_fighters"
BLOCK_ROUNDS = "bout_fighter_rounds"

# Tolerância do tempo de controle, declarada **antes** da medição. As duas fontes publicam
# ``"m:ss"``, então a conversão é simétrica e a divergência esperada é zero; o segundo de folga
# cobre arredondamento de operação, não erro de unidade.
CONTROL_TIME_TOLERANCE_SECONDS = 1


class GranularMeasurementError(Exception):
    """Falha na medição do granular da fonte oficial."""


class OfficialFighterNotInCardError(GranularMeasurementError):
    """Estatística de um ``FighterId`` que não está em ``Fighters[]`` da mesma luta.

    Sem o lutador no card não há nome, e sem nome não há chave de casamento. Deixar a linha
    passar em silêncio removeria dado da medição sem que ninguém soubesse.
    """


class AmbiguousGranularMatchError(GranularMeasurementError):
    """Duas linhas do mesmo evento disputam a mesma chave ``(nome normalizado, round)``.

    Espelha o ``AmbiguousBoutFighterMatchError`` do matching da Cito: ambiguidade **falha
    alto**, nunca casa no escuro. Um casamento errado aqui não corrompe o banco, mas corrompe
    o número que decide a arquitetura de ingestão do projeto.
    """


class EventOutsideWindowError(GranularMeasurementError):
    """Evento anterior a ``OFFICIAL_WINDOW_START`` -- fora da janela da SPEC 008 (RF-03)."""


@dataclass(frozen=True)
class GranularLine:
    """Uma linha de granular achatada, comum aos dois lados da comparação.

    ``round=None`` identifica o total da luta (bloco ``bout_fighters``); ``round=N`` identifica
    o round N (bloco ``bout_fighter_rounds``). ``values`` é indexado pelos nomes de
    ``MEASURED_FIELDS``; ausência é ``None``, nunca zero.
    """

    fighter_name_normalized: str
    round: int | None
    values: Mapping[str, int | None]

    @property
    def block(self) -> str:
        """Bloco a que a linha pertence, derivado de ``round`` -- não é um campo à parte."""
        return BLOCK_TOTALS if self.round is None else BLOCK_ROUNDS


@dataclass(frozen=True)
class FieldAgreement:
    """Concordância de um campo: ``compared`` é o denominador (ambos os lados não nulos)."""

    field: str
    compared: int
    agreed: int
    disagreements: tuple[str, ...]

    @property
    def rate(self) -> float:
        """Fração de concordância; ``0.0`` quando não há nada comparável (ver ``measured``)."""
        return self.agreed / self.compared if self.compared else 0.0

    @property
    def measured(self) -> bool:
        """``False`` quando o denominador é zero -- reportado como "não medido", nunca aprovado."""
        return self.compared > 0


@dataclass(frozen=True)
class GranularComparisonReport:
    """Relatório de um bloco de um evento, para uma linhagem do lado persistido."""

    event_id: int
    event_name: str
    block: str
    persisted_source: str
    matched_lines: int
    persisted_lines: int
    unmatched_persisted: tuple[str, ...]
    unmatched_official: tuple[str, ...]
    fields: tuple[FieldAgreement, ...]

    @property
    def match_rate(self) -> float:
        """Cobertura de casamento sobre as linhas **persistidas** (o denominador do critério).

        Linha oficial sem contraparte persistida não entra aqui: é buraco do nosso backfill,
        não falha da fonte, e contá-la reprovaria a fonte por um defeito nosso.
        """
        return self.matched_lines / self.persisted_lines if self.persisted_lines else 0.0


def extract_official_lines(fight: UfcOfficialFightGranular) -> list[GranularLine]:
    """Achata ``FightStats`` (total) + ``RoundStats`` (por round) em ``GranularLine``.

    O nome vem de ``Fighters[]`` da própria luta, casado por ``FighterId``; estatística de um
    id que não está no card levanta ``OfficialFighterNotInCardError``. Luta ainda não realizada
    tem os dois blocos vazios e não produz linha nenhuma -- ausência nunca vira linha zerada.
    """
    name_by_id = {
        fighter.fighter_id: normalize_name(f"{fighter.name.first_name} {fighter.name.last_name}")
        for fighter in fight.fighters
    }

    def nome(fighter_id: int) -> str:
        name = name_by_id.get(fighter_id)
        if name is None:
            raise OfficialFighterNotInCardError(
                f"A luta {fight.fight_id} tem estatística do lutador {fighter_id}, que não está "
                "no card da própria luta -- sem nome não há chave de casamento."
            )
        return name

    lines = [
        GranularLine(nome(total.fighter_id), None, _values(total)) for total in fight.fight_stats
    ]
    lines.extend(
        GranularLine(nome(entry.fighter_id), round_line.round_number, _values(round_line))
        for entry in fight.round_stats
        for round_line in entry.rounds
    )
    return lines


def _values(line: UfcOfficialStatLine | BoutFighter | BoutFighterRound) -> Mapping[str, int | None]:
    """Projeta uma linha dos dois lados nos 22 campos medidos, pelo **mesmo nome**.

    Funciona para os dois lados porque o alias do DTO já traduziu ``SigStrikesLanded`` para
    ``sig_strikes_landed``, que é como a coluna se chama no model. O ``cast`` existe porque o
    acesso é por nome dinâmico (a lista de campos é dado, não código); os tipos reais são
    garantidos pelo DTO (``int``) e pelo model (``Mapped[int | None]``).
    """
    return {field: cast("int | None", getattr(line, field)) for field in MEASURED_FIELDS}


def load_persisted_lines(session: Session, event: Event) -> list[tuple[GranularLine, str]]:
    """Lê ``bout_fighters`` (``round=None``) e ``bout_fighter_rounds`` do evento.

    Devolve ``(linha, source_da_linha)`` -- o ``source`` acompanha porque as duas tabelas têm
    linhagens diferentes no mesmo evento e a medição precisa reportá-las em separado. Evento
    fora da janela levanta. **Leitura pura**: nenhuma escrita, nenhum ``flush``.
    """
    if event.date < OFFICIAL_WINDOW_START:
        raise EventOutsideWindowError(
            f"O evento {event.id} ({event.name}, {event.date}) é anterior a "
            f"{OFFICIAL_WINDOW_START} e está fora da janela da SPEC 008."
        )

    corners = session.execute(
        select(BoutFighter, Fighter.name)
        .join(Bout, Bout.id == BoutFighter.bout_id)
        .join(Fighter, Fighter.id == BoutFighter.fighter_id)
        .where(Bout.event_id == event.id)
    ).all()

    lines: list[tuple[GranularLine, str]] = [
        (GranularLine(normalize_name(name), None, _values(corner)), corner.source)
        for corner, name in corners
    ]

    rounds = session.execute(
        select(BoutFighterRound, Fighter.name)
        .join(BoutFighter, BoutFighter.id == BoutFighterRound.bout_fighter_id)
        .join(Bout, Bout.id == BoutFighter.bout_id)
        .join(Fighter, Fighter.id == BoutFighter.fighter_id)
        .where(Bout.event_id == event.id)
    ).all()

    lines.extend(
        (
            GranularLine(normalize_name(name), round_row.round, _values(round_row)),
            round_row.source,
        )
        for round_row, name in rounds
    )
    return lines


def compare_granular(
    official: Sequence[GranularLine],
    persisted: Sequence[tuple[GranularLine, str]],
    *,
    event_id: int,
    event_name: str,
    control_time_tolerance_seconds: int = CONTROL_TIME_TOLERANCE_SECONDS,
) -> tuple[GranularComparisonReport, ...]:
    """Casa por ``(nome normalizado, round)`` e devolve um relatório por bloco e linhagem.

    Função **pura**: sem ``Session``, sem rede, sem estado. Chave repetida em qualquer um dos
    lados levanta ``AmbiguousGranularMatchError`` -- nunca casar no escuro. Campo nulo em
    qualquer um dos lados sai do denominador daquele campo: ausência não é divergência.
    """
    official_by_key = _index(official)
    reports: list[GranularComparisonReport] = []

    for (block, source), grupo in _group_by_block_and_source(persisted).items():
        persisted_by_key = _index(line for line, _ in grupo)
        casadas = sorted(set(persisted_by_key) & set(official_by_key), key=_ordem)
        acumulado: dict[str, list[tuple[int, int]]] = defaultdict(list)
        divergencias: dict[str, list[str]] = defaultdict(list)

        for key in casadas:
            _acumula(
                official_by_key[key],
                persisted_by_key[key],
                key,
                control_time_tolerance_seconds,
                acumulado,
                divergencias,
            )

        reports.append(
            GranularComparisonReport(
                event_id=event_id,
                event_name=event_name,
                block=block,
                persisted_source=source,
                matched_lines=len(casadas),
                persisted_lines=len(persisted_by_key),
                unmatched_persisted=tuple(
                    _rotulo(key)
                    for key in sorted(set(persisted_by_key) - set(official_by_key), key=_ordem)
                ),
                unmatched_official=tuple(
                    _rotulo(key)
                    for key in sorted(set(official_by_key) - set(persisted_by_key), key=_ordem)
                    if _mesmo_bloco(key, block)
                ),
                fields=tuple(
                    FieldAgreement(
                        field=field,
                        compared=len(acumulado[field]),
                        agreed=sum(
                            1
                            for esquerda, direita in acumulado[field]
                            if _bate(field, esquerda, direita, control_time_tolerance_seconds)
                        ),
                        disagreements=tuple(divergencias[field]),
                    )
                    for field in MEASURED_FIELDS
                ),
            )
        )

    return tuple(reports)


def _acumula(
    oficial: GranularLine,
    persistida: GranularLine,
    key: tuple[str, int | None],
    tolerancia: int,
    acumulado: dict[str, list[tuple[int, int]]],
    divergencias: dict[str, list[str]],
) -> None:
    """Compara uma linha casada campo a campo, alimentando denominador e divergências."""
    for field in MEASURED_FIELDS:
        esquerda = oficial.values.get(field)
        direita = persistida.values.get(field)
        if esquerda is None or direita is None:
            continue  # ausência não é divergência: a linha sai do denominador deste campo
        acumulado[field].append((esquerda, direita))
        if not _bate(field, esquerda, direita, tolerancia):
            divergencias[field].append(f"{_rotulo(key)}: oficial={esquerda} persistido={direita}")


def _bate(field: str, oficial: int, persistido: int, tolerancia: int) -> bool:
    """Igualdade exata, exceto o tempo de controle, que admite a tolerância declarada."""
    if field == "control_time_seconds":
        return abs(oficial - persistido) <= tolerancia
    return oficial == persistido


def _index(lines: Iterable[GranularLine]) -> dict[tuple[str, int | None], GranularLine]:
    """Indexa por ``(nome normalizado, round)``; chave repetida **falha alto**."""
    indexado: dict[tuple[str, int | None], GranularLine] = {}
    for line in lines:
        key = (line.fighter_name_normalized, line.round)
        if key in indexado:
            raise AmbiguousGranularMatchError(
                f"Duas linhas disputam a chave {_rotulo(key)} no mesmo evento -- casar no escuro "
                "corromperia o número que decide o portão."
            )
        indexado[key] = line
    return indexado


def _group_by_block_and_source(
    persisted: Sequence[tuple[GranularLine, str]],
) -> dict[tuple[str, str], list[tuple[GranularLine, str]]]:
    """Agrupa o lado persistido por ``(bloco, source)``, em ordem estável de rótulo."""
    grupos: dict[tuple[str, str], list[tuple[GranularLine, str]]] = defaultdict(list)
    for line, source in persisted:
        grupos[(line.block, source)].append((line, source))
    return dict(sorted(grupos.items()))


def _ordem(key: tuple[str, int | None]) -> tuple[str, int]:
    """Ordem estável para chaves de blocos diferentes: o total vem antes do round 1.

    ``sorted`` sobre ``(str, int | None)`` levanta ``TypeError`` assim que uma chave de total
    (``round=None``) e uma de round disputam a comparação -- defeito encontrado na primeira
    execução real, não nos testes sintéticos, porque exige duas linhas não casadas de blocos
    diferentes no mesmo conjunto.
    """
    name, rodada = key
    return (name, -1 if rodada is None else rodada)


def _mesmo_bloco(key: tuple[str, int | None], block: str) -> bool:
    """A linha oficial pertence ao bloco do relatório? (``round=None`` é o bloco de totais.)"""
    return (BLOCK_TOTALS if key[1] is None else BLOCK_ROUNDS) == block


def _rotulo(key: tuple[str, int | None]) -> str:
    """``('caio borralho', 3)`` -> ``"caio borralho (round 3)"``; ``None`` -> ``"(total)"``."""
    name, rodada = key
    return f"{name} (total)" if rodada is None else f"{name} (round {rodada})"


def select_corroboration_events(session: Session, *, limit: int) -> list[Event]:
    """Regra **determinística** de escolha dos eventos de corroboração, declarada antes de medir.

    Entre os eventos de 2023-2025 com ``ufc_event_id`` preenchido, os ``limit`` com mais linhas
    em ``bout_fighter_rounds``, desempate por ``events.id`` crescente. Existe como função para
    que a escolha seja reproduzível: um evento escolhido a dedo depois de ver o resultado não
    corrobora coisa nenhuma.
    """
    linhas = func.count(BoutFighterRound.id).label("linhas")
    rows = session.execute(
        select(Event, linhas)
        .join(Bout, Bout.event_id == Event.id)
        .join(BoutFighter, BoutFighter.bout_id == Bout.id)
        .join(BoutFighterRound, BoutFighterRound.bout_fighter_id == BoutFighter.id)
        .where(
            Event.ufc_event_id.is_not(None),
            Event.date.between(date(2023, 1, 1), date(2025, 12, 31)),
        )
        .group_by(Event.id)
        .order_by(linhas.desc(), Event.id)
        .limit(limit)
    ).all()
    return [event for event, _ in rows]


def measure_event(
    session: Session, client: UfcOfficialClient, event: Event
) -> tuple[GranularComparisonReport, ...]:
    """Mede um evento: busca o granular oficial, lê o persistido e compara. **Leitura pura**.

    Uma chamada ao endpoint de evento (para descobrir os ``fightId`` do card) mais uma por
    luta. Tudo gratuito -- a fonte não tem quota. Luta sem granular publicado simplesmente não
    produz linha, e a ausência aparece no relatório como não casada.
    """
    if event.ufc_event_id is None:
        raise GranularMeasurementError(
            f"O evento {event.id} ({event.name}) não tem 'ufc_event_id' -- a Sprint 008-02 não "
            "o mapeou, então não há como localizá-lo na fonte oficial."
        )

    official_event = client.fetch_event(int(event.ufc_event_id))
    official: list[GranularLine] = []
    for fight in official_event.fight_card:
        official.extend(extract_official_lines(client.fetch_fight_granular(fight.fight_id)))

    persisted = load_persisted_lines(session, event)
    return compare_granular(official, persisted, event_id=event.id, event_name=event.name)


def log_report(report: GranularComparisonReport) -> None:
    """Escreve o relatório de um bloco no logger -- ``print`` é proibido (regra ``T20``)."""
    logger.info(
        "Evento '%s' (id %d) -- bloco %s, linhagem %s",
        report.event_name,
        report.event_id,
        report.block,
        report.persisted_source,
    )
    logger.info(
        "  casadas: %d/%d linhas persistidas (%.1f%%); %d linhas oficiais sem contraparte",
        report.matched_lines,
        report.persisted_lines,
        report.match_rate * 100,
        len(report.unmatched_official),
    )
    for field in report.fields:
        if not field.measured:
            logger.info("  %-24s      --  nao medido (nenhuma linha comparavel)", field.field)
            continue
        logger.info(
            "  %-24s %4d/%-4d %6.1f%%%s",
            field.field,
            field.agreed,
            field.compared,
            field.rate * 100,
            f"  ({len(field.disagreements)} divergencias)" if field.disagreements else "",
        )
        for divergencia in field.disagreements[:10]:
            logger.info("      %s", divergencia)


def run_measurement(
    session: Session,
    client: UfcOfficialClient,
    *,
    event_slug: str,
    corroboration: int = 0,
) -> tuple[GranularComparisonReport, ...]:
    """Mede o evento primário e, opcionalmente, os de corroboração. Nenhuma escrita."""
    primario = session.scalars(select(Event).where(Event.cito_slug == event_slug)).one_or_none()
    if primario is None:
        raise GranularMeasurementError(f"Nenhum evento persistido com cito_slug {event_slug!r}.")

    eventos = [primario]
    if corroboration:
        eventos.extend(
            event
            for event in select_corroboration_events(session, limit=corroboration + 1)
            if event.id != primario.id
        )
        eventos = eventos[: corroboration + 1]

    reports: list[GranularComparisonReport] = []
    for event in eventos:
        for report in measure_event(session, client, event):
            log_report(report)
            reports.append(report)
    return tuple(reports)


def main(argv: Sequence[str] | None = None) -> int:
    """Entrypoint da medição. Abre a sessão, mede e loga -- nunca commita."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event-slug",
        required=True,
        help="cito_slug do evento primário da medição.",
    )
    parser.add_argument(
        "--corroboracao",
        type=int,
        default=0,
        help="Quantos eventos de corroboração acrescentar (regra determinística).",
    )
    args = parser.parse_args(argv)

    with SessionLocal() as session:
        run_measurement(
            session,
            UfcOfficialClient(),
            event_slug=args.event_slug,
            corroboration=args.corroboracao,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
