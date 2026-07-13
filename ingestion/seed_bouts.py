"""Carga idempotente de ``bouts`` + ``bout_fighters`` a partir do seed do Kaggle (ADR 0002).

Fluxo: aquisicao (``sources.kaggle``: ``fight_details.csv`` traz o box-score por luta e por
canto; ``event_details.csv`` traz a data do evento e o vencedor por luta) -> combinacao das duas
bordas por ``event_id``/``fight_id`` -> mapeamento do core da luta e explosao wide->long em
exatamente duas linhas de ``bout_fighters`` (canto red e blue; ADR 0001) -> resolucao das FKs
contra os fighters/events ja semeados (Slices 02 e 03) -> get-or-create idempotente por chave
natural. Grava ``source="kaggle"`` em toda escrita.

Decisoes desta slice (confirmadas contra o dataset real):

- As stats sao granulares **por luta** (nao medias): ``r_/b_{kd, sig_str_landed/atmpted,
  td_landed/atmpted, sub_att, ctrl}``. Nenhuma coluna ``*_avg_*`` e usada -- o principio
  inegociavel de granularidade e a razao da troca de fonte (ADR 0002).
- O metodo cobre 100% dos tokens do dataset; tokens sem vencedor (``Overturned``,
  ``Could Not Continue``, ``Other``) mapeiam para ``NO_CONTEST`` com vencedor nulo (ADR 0001,
  Decisao #4). Um token nao previsto degrada de forma conservadora para ``NO_CONTEST`` (log).
- A chave natural de ``bouts`` -- ``(event_id, par nao-ordenado de fighter_ids)`` -- nao tem
  constraint no banco (schema da Slice 01); a idempotencia e de nivel de aplicacao, via um indice
  em memoria com desempate deterministico por ``sorted((red_fid, blue_fid))``. ``bout_fighters``
  tem unicidade ``(bout_id, fighter_id)`` e usa get-or-create nessa chave.
- Lutas cujo lutador/evento nao resolve (ex.: homonimo ambiguo por nome, sem DOB no
  ``fight_details``) sao registradas em log e puladas -- nunca se insere luta com FK inventada.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TypedDict

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.normalize import normalize_name
from ingestion.sources.kaggle import (
    EVENT_DETAILS_FILE,
    FIGHT_DETAILS_FILE,
    load_event_details,
    load_fight_details,
)
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

SOURCE = "kaggle"

# Formato da data em ``event_details.csv`` (ex.: ``"September 06, 2025"``).
_DATE_FORMAT = "%B %d, %Y"

# Colunas reais dos CSVs (ADR 0002).
_EVENT_NAME_COL = "event_name"
_EVENT_ID_COL = "event_id"
_FIGHT_ID_COL = "fight_id"
_R_NAME_COL = "r_name"
_B_NAME_COL = "b_name"
_DIVISION_COL = "division"
_METHOD_COL = "method"
_FINISH_ROUND_COL = "finish_round"
_MATCH_TIME_COL = "match_time_sec"
_DATE_COL = "date"
_WINNER_COL = "winner"

# Campo do model ``BoutFighter`` -> sufixo da coluna de stat no CSV (prefixado por canto).
_STAT_COLUMN_BY_FIELD: dict[str, str] = {
    "knockdowns": "kd",
    "sig_strikes_landed": "sig_str_landed",
    "sig_strikes_attempted": "sig_str_atmpted",
    "takedowns_landed": "td_landed",
    "takedowns_attempted": "td_atmpted",
    "submission_attempts": "sub_att",
    "control_time_seconds": "ctrl",
}

_CORNER_PREFIX: dict[Corner, str] = {Corner.RED: "r", Corner.BLUE: "b"}

# Mapeamento do token de metodo do dataset -> enum. Cobre 100% dos valores reais.
_METHOD_BY_TOKEN: dict[str, BoutMethod] = {
    "Decision - Unanimous": BoutMethod.DECISION,
    "Decision - Split": BoutMethod.DECISION,
    "Decision - Majority": BoutMethod.DECISION,
    "KO/TKO": BoutMethod.KO_TKO,
    "TKO - Doctor's Stoppage": BoutMethod.KO_TKO,
    "Submission": BoutMethod.SUBMISSION,
    "DQ": BoutMethod.DQ,
    "Overturned": BoutMethod.NO_CONTEST,
    "Could Not Continue": BoutMethod.NO_CONTEST,
    "Other": BoutMethod.NO_CONTEST,
}

# Colunas de ``fight_details.csv`` consumidas pelo dominio (core + box-score por canto).
_CORE_FIGHT_COLUMNS: tuple[str, ...] = (
    _EVENT_NAME_COL,
    _EVENT_ID_COL,
    _FIGHT_ID_COL,
    _R_NAME_COL,
    _B_NAME_COL,
    _DIVISION_COL,
    _METHOD_COL,
    _FINISH_ROUND_COL,
    _MATCH_TIME_COL,
)
_STAT_FIGHT_COLUMNS: tuple[str, ...] = tuple(
    f"{prefix}_{suffix}" for prefix in ("r", "b") for suffix in _STAT_COLUMN_BY_FIELD.values()
)
_FIGHT_COLUMNS: tuple[str, ...] = _CORE_FIGHT_COLUMNS + _STAT_FIGHT_COLUMNS

# --- Backfill M5 (Slice 02): splits de golpe totais + contexto da luta, do CSV do seed --------
#
# O ``fight_details.csv`` do seed (ADR 0002) já traz, ~100% preenchidos e nunca ingeridos, os
# golpes por alvo/posição (totais da luta) e o contexto (title fight, rounds agendados, árbitro).
# O backfill faz UPDATE idempotente por chave natural nas linhas já persistidas -- 0 quota Cito.

# Campo do model ``BoutFighter`` (WIDE) -> sufixo da coluna de split no CSV (prefixado por canto).
# Só pares landed/attempted: ``*_acc``/``*_per`` são deriváveis (SPEC "Fora do escopo") e não
# entram. ``reversals`` NÃO tem coluna no ``fight_details.csv`` do seed -- permanece ``NULL`` aqui
# (virá do ``roundStats`` da Cito na Slice 05); não se inventa valor.
_SPLIT_COLUMN_BY_FIELD: dict[str, str] = {
    "total_strikes_landed": "total_str_landed",
    "total_strikes_attempted": "total_str_atmpted",
    "head_landed": "head_landed",
    "head_attempted": "head_atmpted",
    "body_landed": "body_landed",
    "body_attempted": "body_atmpted",
    "leg_landed": "leg_landed",
    "leg_attempted": "leg_atmpted",
    "distance_landed": "dist_landed",
    "distance_attempted": "dist_atmpted",
    "clinch_landed": "clinch_landed",
    "clinch_attempted": "clinch_atmpted",
    "ground_landed": "ground_landed",
    "ground_attempted": "ground_atmpted",
}

# Colunas de contexto da luta (tabela ``bouts``), presentes na linha do ``fight_details.csv``.
_TITLE_FIGHT_COL = "title_fight"
_TOTAL_ROUNDS_COL = "total_rounds"
_REFEREE_COL = "referee"

_SPLIT_FIGHT_COLUMNS: tuple[str, ...] = tuple(
    f"{prefix}_{suffix}" for prefix in ("r", "b") for suffix in _SPLIT_COLUMN_BY_FIELD.values()
)
_CONTEXT_FIGHT_COLUMNS: tuple[str, ...] = (_TITLE_FIGHT_COL, _TOTAL_ROUNDS_COL, _REFEREE_COL)
# Colunas projetadas pelo backfill: core (para resolver as FKs) + splits + contexto.
_BACKFILL_COLUMNS: tuple[str, ...] = (
    _CORE_FIGHT_COLUMNS + _SPLIT_FIGHT_COLUMNS + _CONTEXT_FIGHT_COLUMNS
)

# Variável de ambiente que aponta o diretório local do dataset (paridade com ``ingestion.seed``).
_ENV_DATASET_DIR = "SEED_DATASET_DIR"


class BoutCore(TypedDict):
    """Campos da luta como um todo (tabela ``bouts``), tipados a partir da linha crua."""

    method: BoutMethod
    round: int | None
    ending_time_seconds: int | None
    weight_class: str | None
    winner_corner: Corner | None  # ``None`` em empate / no contest


@dataclass(frozen=True)
class BoutLoadResult:
    """Resumo de uma execucao da carga de bouts (observavel para idempotencia e taxa de skip)."""

    bouts_inserted: int
    bout_fighters_inserted: int
    skipped: int


def _parse_optional_int(value: str) -> int | None:
    """Converte um numero do CSV (``"17.0"``/``"2"``/``""``) em inteiro ou ``None``.

    O dataset traz contagens como floats textuais (``"17.0"``); vazio (apos trim) vira ``None``.
    """
    stripped = value.strip()
    if not stripped:
        return None
    return round(float(stripped))


def _parse_event_date(value: str) -> date:
    """Data do evento no formato ``"April 13, 2024"`` -> ``date`` de calendario.

    E uma data de calendario (sem instante/timezone); por isso ``strptime`` sem ``%z``.
    """
    return datetime.strptime(value.strip(), _DATE_FORMAT).date()  # noqa: DTZ007  # data de calendario, sem timezone


def _parse_event_date_or_none(value: str) -> date | None:
    """Como ``_parse_event_date``, mas devolve ``None`` para data ausente ou nao parseavel.

    ``_merge_fight_rows`` preenche ``date`` com string vazia quando a luta referencia um
    ``event_id`` inexistente em ``event_details``; nesse caso o evento nao resolve e a luta
    e pulada pelo chamador, sem abortar o seed inteiro.
    """
    if not value.strip():
        return None
    try:
        return _parse_event_date(value)
    except ValueError:
        return None


def _map_method(token: str) -> BoutMethod:
    """Mapeia o token de metodo do dataset ao enum; token nao previsto -> ``NO_CONTEST`` (log)."""
    method = _METHOD_BY_TOKEN.get(token.strip())
    if method is None:
        logger.warning("Metodo nao previsto %r; mapeado conservadoramente para NO_CONTEST", token)
        return BoutMethod.NO_CONTEST
    return method


def _winner_corner(row: Mapping[str, str], method: BoutMethod) -> Corner | None:
    """Determina o canto vencedor pelo nome normalizado; nulo em no contest / sem vencedor."""
    if method is BoutMethod.NO_CONTEST:
        return None
    winner = row[_WINNER_COL].strip()
    if not winner:
        return None
    winner_normalized = normalize_name(winner)
    if winner_normalized == normalize_name(row[_R_NAME_COL]):
        return Corner.RED
    if winner_normalized == normalize_name(row[_B_NAME_COL]):
        return Corner.BLUE
    logger.warning("Vencedor %r nao casa com nenhum canto da luta; vencedor nulo", winner)
    return None


def map_bout_core(row: Mapping[str, str]) -> BoutCore:
    """Mapeia uma linha crua da luta nos campos core de ``Bout`` (funcao pura, sem I/O)."""
    method = _map_method(row[_METHOD_COL])
    weight_class = row[_DIVISION_COL].strip() or None
    return BoutCore(
        method=method,
        round=_parse_optional_int(row[_FINISH_ROUND_COL]),
        ending_time_seconds=_parse_optional_int(row[_MATCH_TIME_COL]),
        weight_class=weight_class,
        winner_corner=_winner_corner(row, method),
    )


def build_bout_fighter(
    row: Mapping[str, str], corner: Corner, fighter_id: int, bout_id: int
) -> BoutFighter:
    """Explode o lado ``corner`` da linha wide numa linha long de ``bout_fighters``.

    Extrai as stats granulares daquele canto (prefixo ``r_``/``b_``); stats ausentes viram
    ``None`` (CA-06). Grava ``source="kaggle"``. Nenhuma media pre-agregada e usada.
    """
    prefix = _CORNER_PREFIX[corner]
    stats = {
        field: _parse_optional_int(row[f"{prefix}_{suffix}"])
        for field, suffix in _STAT_COLUMN_BY_FIELD.items()
    }
    return BoutFighter(
        bout_id=bout_id,
        fighter_id=fighter_id,
        corner=corner,
        source=SOURCE,
        **stats,
    )


class BoutContext(TypedDict):
    """Contexto da luta (tabela ``bouts``) lido do CSV do seed -- fronteira dinâmica tipada."""

    title_bout: bool | None
    scheduled_rounds: int | None
    referee: str | None


def _parse_optional_bool(value: str) -> bool | None:
    """Converte a flag textual do CSV (``"1"``/``"1.0"`` -> ``True``; ``"0"``/``"0.0"`` ->
    ``False``) em booleano; vazio ou valor inesperado degrada para ``None`` (com log)."""
    stripped = value.strip()
    if stripped in ("1", "1.0"):
        return True
    if stripped in ("0", "0.0"):
        return False
    if stripped:
        logger.warning("Flag booleana inesperada %r; degradada para None", stripped)
    return None


def _parse_optional_split(value: str) -> int | None:
    """Como ``_parse_optional_int``, mas degrada célula não-parseável para ``None`` (com log).

    O split do ``fight_details.csv`` é ~100% preenchido; uma célula não-numérica é anomalia
    pontual de dado -- não erro de programa. Degrada graciosamente para ``None`` (CA-01),
    simétrico ao ``_parse_optional_bool``, em vez de abortar o backfill inteiro. A ausência
    (``""``) segue virando ``None`` pelo próprio ``_parse_optional_int`` (sem log). Restringido
    ao split de propósito: não mascara valor malformado em campos core (``round``/tempo/rounds
    agendados), que continuam levantando via ``_parse_optional_int``.
    """
    try:
        return _parse_optional_int(value)
    except ValueError:
        logger.warning("Valor de split não numérico %r; degradado para None", value)
        return None


def build_bout_fighter_splits(row: Mapping[str, str], corner: Corner) -> dict[str, int | None]:
    """Extrai os 7 grupos de split (landed/attempted) daquele canto da linha crua da luta.

    Lê as colunas ``{r,b}_{total_str/head/body/leg/dist/clinch/ground}_{landed,atmpted}``;
    valor ausente/malformado vira ``None`` (função pura, sem I/O). ``reversals`` **não** vem do
    CSV do seed (permanece fora do dicionário -- será populado da Cito na Slice 05).
    """
    prefix = _CORNER_PREFIX[corner]
    return {
        field: _parse_optional_split(row[f"{prefix}_{suffix}"])
        for field, suffix in _SPLIT_COLUMN_BY_FIELD.items()
    }


def map_bout_context(row: Mapping[str, str]) -> BoutContext:
    """Mapeia o contexto da luta (title fight, rounds agendados, árbitro) -- função pura.

    ``title_bout`` degrada ausência/valor inesperado para ``None``; ``scheduled_rounds`` reusa o
    parser numérico da borda; ``referee`` vazio (após trim) vira ``None``.
    """
    return BoutContext(
        title_bout=_parse_optional_bool(row[_TITLE_FIGHT_COL]),
        scheduled_rounds=_parse_optional_int(row[_TOTAL_ROUNDS_COL]),
        referee=row[_REFEREE_COL].strip() or None,
    )


def build_fighter_index(session: Session) -> dict[str, int]:
    """Indexa os fighters persistidos por nome normalizado -> id.

    Nomes que mapeiam para mais de um lutador (homonimos, ex.: dois ``Bruno Silva``) sao
    **ambiguos** e ficam fora do indice: como ``fight_details`` so traz o nome (sem DOB),
    nao ha como desempatar, e a luta correspondente e pulada em vez de receber um id chutado.
    """
    counts: dict[str, int] = {}
    first_id: dict[str, int] = {}
    for fighter_id, name_normalized in session.execute(select(Fighter.id, Fighter.name_normalized)):
        counts[name_normalized] = counts.get(name_normalized, 0) + 1
        first_id.setdefault(name_normalized, fighter_id)
    return {name: first_id[name] for name, count in counts.items() if count == 1}


def build_event_index(session: Session) -> dict[tuple[str, date], int]:
    """Indexa os events persistidos pela chave natural ``(name, date)`` -> id."""
    return {
        (name, event_date): event_id
        for event_id, name, event_date in session.execute(select(Event.id, Event.name, Event.date))
    }


def resolve_bout_fks(
    row: Mapping[str, str],
    fighter_index: Mapping[str, int],
    event_index: Mapping[tuple[str, date], int],
) -> tuple[int, int, int] | None:
    """Resolve ``(event_id, red_fighter_id, blue_fighter_id)`` da linha contra os indices.

    Devolve ``None`` quando qualquer FK nao resolve (lutador ausente/ambiguo ou evento
    ausente) -- o chamador registra e pula a luta, sem inserir orfao.
    """
    red_id = fighter_index.get(normalize_name(row[_R_NAME_COL]))
    blue_id = fighter_index.get(normalize_name(row[_B_NAME_COL]))
    event_date = _parse_event_date_or_none(row[_DATE_COL])
    event_id = (
        event_index.get((row[_EVENT_NAME_COL], event_date)) if event_date is not None else None
    )
    if red_id is None or blue_id is None or event_id is None:
        return None
    return (event_id, red_id, blue_id)


def _bout_key(event_id: int, red_id: int, blue_id: int) -> tuple[int, int, int]:
    """Chave natural determinística da luta: evento + par nao-ordenado de fighter_ids."""
    low, high = sorted((red_id, blue_id))
    return (event_id, low, high)


def _winner_id(winner_corner: Corner | None, red_id: int, blue_id: int) -> int | None:
    """Traduz o canto vencedor no ``fighter_id`` correspondente (nulo em empate/no contest)."""
    if winner_corner is Corner.RED:
        return red_id
    if winner_corner is Corner.BLUE:
        return blue_id
    return None


def _existing_bout_index(session: Session) -> dict[tuple[int, int, int], int]:
    """Indexa as lutas ja persistidas pela chave natural ``(event_id, low_fid, high_fid)``."""
    grouped: dict[int, tuple[int, list[int]]] = {}
    for bout_id, fighter_id, event_id in session.execute(
        select(BoutFighter.bout_id, BoutFighter.fighter_id, Bout.event_id).join(
            Bout, Bout.id == BoutFighter.bout_id
        )
    ):
        _, fighter_ids = grouped.setdefault(bout_id, (event_id, []))
        fighter_ids.append(fighter_id)

    index: dict[tuple[int, int, int], int] = {}
    for bout_id, (event_id, fighter_ids) in grouped.items():
        if len(fighter_ids) != 2:
            continue
        index[_bout_key(event_id, fighter_ids[0], fighter_ids[1])] = bout_id
    return index


def _existing_bout_fighter_keys(session: Session) -> set[tuple[int, int]]:
    """Conjunto das chaves ``(bout_id, fighter_id)`` ja persistidas em ``bout_fighters``."""
    return {
        (bout_id, fighter_id)
        for bout_id, fighter_id in session.execute(
            select(BoutFighter.bout_id, BoutFighter.fighter_id)
        )
    }


def _merge_fight_rows(
    fight_details: pd.DataFrame,
    event_details: pd.DataFrame,
    columns: tuple[str, ...] = _FIGHT_COLUMNS,
) -> list[dict[str, str]]:
    """Combina as bordas: anexa ``date`` (por ``event_id``) e ``winner`` (por ``fight_id``).

    Projeta apenas ``columns`` do ``fight_details`` (default: core + box-score do seed; o backfill
    do M5 passa ``_BACKFILL_COLUMNS`` para trazer splits + contexto). Tipa a borda dinamica do
    Pandas -- cada celula consumida vira texto; nenhum ``Any`` do ``DataFrame`` propaga para o
    dominio.
    """
    date_by_event = _first_by_key(event_details, _EVENT_ID_COL, _DATE_COL)
    winner_by_fight = _first_by_key(event_details, _FIGHT_ID_COL, _WINNER_COL)

    rows: list[dict[str, str]] = []
    for record in fight_details[list(columns)].to_dict(orient="records"):
        row = {column: str(record[column]) for column in columns}
        row[_DATE_COL] = date_by_event.get(row[_EVENT_ID_COL], "")
        row[_WINNER_COL] = winner_by_fight.get(row[_FIGHT_ID_COL], "")
        rows.append(row)
    return rows


def _first_by_key(frame: pd.DataFrame, key_column: str, value_column: str) -> dict[str, str]:
    """Mapeia ``key_column`` -> ``value_column`` (primeira ocorrencia vence), tudo como texto."""
    mapping: dict[str, str] = {}
    for record in frame[[key_column, value_column]].to_dict(orient="records"):
        key = str(record[key_column])
        if key not in mapping:
            mapping[key] = str(record[value_column])
    return mapping


def load_bouts(
    session: Session, fight_details: pd.DataFrame, event_details: pd.DataFrame
) -> BoutLoadResult:
    """Carrega ``bouts`` + ``bout_fighters`` idempotentemente e devolve o resumo da execucao.

    Get-or-create de ``bouts`` pela chave natural ``(event_id, par nao-ordenado)`` (indice em
    memoria, sem constraint no banco) e de ``bout_fighters`` por ``(bout_id, fighter_id)``.
    Lutas cujo lutador/evento nao resolve sao registradas e puladas. ``source="kaggle"``.
    """
    fighter_index = build_fighter_index(session)
    event_index = build_event_index(session)
    existing_bouts = _existing_bout_index(session)
    existing_bout_fighters = _existing_bout_fighter_keys(session)

    bouts_inserted = 0
    bout_fighters_inserted = 0
    skipped = 0

    for row in _merge_fight_rows(fight_details, event_details):
        fks = resolve_bout_fks(row, fighter_index, event_index)
        if fks is None:
            skipped += 1
            logger.warning(
                "Luta %s (%s vs %s) sem FK resolvida; pulada",
                row[_FIGHT_ID_COL],
                row[_R_NAME_COL],
                row[_B_NAME_COL],
            )
            continue

        event_id, red_id, blue_id = fks
        key = _bout_key(event_id, red_id, blue_id)
        bout_id = existing_bouts.get(key)
        if bout_id is None:
            core = map_bout_core(row)
            bout = Bout(
                event_id=event_id,
                winner_id=_winner_id(core["winner_corner"], red_id, blue_id),
                method=core["method"],
                round=core["round"],
                ending_time_seconds=core["ending_time_seconds"],
                weight_class=core["weight_class"],
                source=SOURCE,
            )
            session.add(bout)
            session.flush()  # materializa ``bout.id`` para as FKs de bout_fighters
            bout_id = bout.id
            existing_bouts[key] = bout_id
            bouts_inserted += 1

        for corner, fighter_id in ((Corner.RED, red_id), (Corner.BLUE, blue_id)):
            if (bout_id, fighter_id) in existing_bout_fighters:
                continue
            session.add(build_bout_fighter(row, corner, fighter_id, bout_id))
            existing_bout_fighters.add((bout_id, fighter_id))
            bout_fighters_inserted += 1

    session.flush()
    logger.info(
        "Seed de bouts: %d bouts inseridos, %d bout_fighters inseridos, %d lutas puladas",
        bouts_inserted,
        bout_fighters_inserted,
        skipped,
    )
    return BoutLoadResult(
        bouts_inserted=bouts_inserted,
        bout_fighters_inserted=bout_fighters_inserted,
        skipped=skipped,
    )


def seed_bouts(
    session: Session,
    fight_details_path: Path | None = None,
    event_details_path: Path | None = None,
) -> BoutLoadResult:
    """Adquire ``fight_details.csv`` + ``event_details.csv`` e carrega bouts idempotentemente."""
    fight_details = load_fight_details(fight_details_path)
    event_details = load_event_details(event_details_path)
    return load_bouts(session, fight_details, event_details)


# --- Backfill M5 (Slice 02): UPDATE idempotente de splits + contexto (0 quota Cito) ----------


@dataclass(frozen=True)
class BackfillResult:
    """Resumo observável do backfill: linhas atualizadas e lutas puladas (idempotência/skip)."""

    bouts_updated: int
    bout_fighters_updated: int
    skipped: int


def _existing_bout_by_key(session: Session) -> dict[tuple[int, int, int], Bout]:
    """Indexa os ``Bout`` persistidos pela chave natural ``(event_id, low_fid, high_fid)``.

    Devolve o **objeto** ORM (não o id) para que o backfill sete atributos diretamente (UPDATE).
    Lutas sem exatamente dois cantos ficam fora do índice (não é uma luta completa a atualizar).
    """
    fighter_ids_by_bout: dict[int, list[int]] = {}
    for bout_id, fighter_id in session.execute(select(BoutFighter.bout_id, BoutFighter.fighter_id)):
        fighter_ids_by_bout.setdefault(bout_id, []).append(fighter_id)

    index: dict[tuple[int, int, int], Bout] = {}
    for bout in session.scalars(select(Bout)):
        fighter_ids = fighter_ids_by_bout.get(bout.id, [])
        if len(fighter_ids) != 2:
            continue
        index[_bout_key(bout.event_id, fighter_ids[0], fighter_ids[1])] = bout
    return index


def _existing_bout_fighter_by_key(session: Session) -> dict[tuple[int, int], BoutFighter]:
    """Indexa os ``BoutFighter`` persistidos por ``(bout_id, fighter_id)`` -> objeto ORM."""
    return {(bf.bout_id, bf.fighter_id): bf for bf in session.scalars(select(BoutFighter))}


def backfill_splits_and_context(
    session: Session, fight_details: pd.DataFrame, event_details: pd.DataFrame
) -> BackfillResult:
    """Faz UPDATE idempotente dos splits (``bout_fighters``) e do contexto (``bouts``) do CSV.

    Caminho **distinto** do seed: carrega os objetos ORM já persistidos por chave natural (evento
    + par não-ordenado de fighter_ids; ``(bout_id, fighter_id)`` por canto) e seta os atributos --
    **nunca** insere. Luta/linha sem correspondência já semeada é **pulada** (contada em
    ``skipped``), nunca criada. Grava/preserva ``source="kaggle"``. Rodar de novo mantém contagem
    e conteúdo (o UPDATE por atributo é naturalmente idempotente). Zero chamadas à Cito.
    """
    fighter_index = build_fighter_index(session)
    event_index = build_event_index(session)
    bout_by_key = _existing_bout_by_key(session)
    bout_fighter_by_key = _existing_bout_fighter_by_key(session)

    bouts_updated = 0
    bout_fighters_updated = 0
    skipped = 0

    for row in _merge_fight_rows(fight_details, event_details, _BACKFILL_COLUMNS):
        fks = resolve_bout_fks(row, fighter_index, event_index)
        if fks is None:
            skipped += 1
            logger.warning(
                "Backfill: luta %s (%s vs %s) sem FK resolvida; pulada",
                row[_FIGHT_ID_COL],
                row[_R_NAME_COL],
                row[_B_NAME_COL],
            )
            continue

        event_id, red_id, blue_id = fks
        bout = bout_by_key.get(_bout_key(event_id, red_id, blue_id))
        if bout is None:
            skipped += 1
            logger.warning(
                "Backfill: luta %s (%s vs %s) ainda não semeada; pulada (não cria linha)",
                row[_FIGHT_ID_COL],
                row[_R_NAME_COL],
                row[_B_NAME_COL],
            )
            continue

        context = map_bout_context(row)
        bout.title_bout = context["title_bout"]
        bout.scheduled_rounds = context["scheduled_rounds"]
        bout.referee = context["referee"]
        bout.source = SOURCE
        bouts_updated += 1

        for corner, fighter_id in ((Corner.RED, red_id), (Corner.BLUE, blue_id)):
            bout_fighter = bout_fighter_by_key.get((bout.id, fighter_id))
            if bout_fighter is None:
                continue
            for field, value in build_bout_fighter_splits(row, corner).items():
                setattr(bout_fighter, field, value)
            bout_fighter.source = SOURCE
            bout_fighters_updated += 1

    session.flush()
    logger.info(
        "Backfill de splits + contexto: %d bouts atualizados, %d bout_fighters atualizados, "
        "%d lutas puladas",
        bouts_updated,
        bout_fighters_updated,
        skipped,
    )
    return BackfillResult(
        bouts_updated=bouts_updated,
        bout_fighters_updated=bout_fighters_updated,
        skipped=skipped,
    )


def _parse_backfill_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do backfill."""
    parser = argparse.ArgumentParser(
        description=(
            "Backfill dos splits totais + contexto da luta a partir do CSV do seed "
            "(UPDATE idempotente, source=kaggle, 0 quota Cito)."
        ),
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help=(
            "Diretório local com fight_details.csv e event_details.csv. Omitido: baixa o "
            "dataset via kagglehub. Alternativa: variável SEED_DATASET_DIR."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint de ``python -m ingestion.seed_bouts``: backfill de splits + contexto e commit.

    A lógica testável (``backfill_splits_and_context``) é isolada; ``main`` é fino e escapa à
    transação de teste (por isso commita). Pressupõe o banco já semeado (M0): faz UPDATE nas
    linhas existentes, nunca INSERT. Reporta via ``logging`` (``print`` é proibido).
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_backfill_args(argv)
    raw_env_dir = os.environ.get(_ENV_DATASET_DIR)
    dataset_dir = args.dataset_dir or (Path(raw_env_dir) if raw_env_dir else None)
    fight_path = dataset_dir / FIGHT_DETAILS_FILE if dataset_dir is not None else None
    event_path = dataset_dir / EVENT_DETAILS_FILE if dataset_dir is not None else None

    fight_details = load_fight_details(fight_path)
    event_details = load_event_details(event_path)
    with SessionLocal() as session:
        result = backfill_splits_and_context(session, fight_details, event_details)
        session.commit()

    logger.info("Backfill de splits + contexto concluído: %s", result)


if __name__ == "__main__":
    main()
