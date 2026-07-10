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

import logging
from collections.abc import Mapping
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
from ingestion.sources.kaggle import load_event_details, load_fight_details

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
    event_id = event_index.get((row[_EVENT_NAME_COL], _parse_event_date(row[_DATE_COL])))
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
    fight_details: pd.DataFrame, event_details: pd.DataFrame
) -> list[dict[str, str]]:
    """Combina as bordas: anexa ``date`` (por ``event_id``) e ``winner`` (por ``fight_id``).

    Tipa a borda dinamica do Pandas -- cada celula consumida vira texto; nenhum ``Any`` do
    ``DataFrame`` propaga para o dominio.
    """
    date_by_event = _first_by_key(event_details, _EVENT_ID_COL, _DATE_COL)
    winner_by_fight = _first_by_key(event_details, _FIGHT_ID_COL, _WINNER_COL)

    rows: list[dict[str, str]] = []
    for record in fight_details[list(_FIGHT_COLUMNS)].to_dict(orient="records"):
        row = {column: str(record[column]) for column in _FIGHT_COLUMNS}
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
