"""Features de trajetória de carreira e contexto físico sobre a frame longa.

Núcleo da Slice 03 da SPEC 005 (M4 -- prontidão preditiva). ``add_trajectory_features``
enriquece a frame longa por lutador-luta da Slice 01 com a **idade na luta** (de
``date_of_birth`` vs. data do evento), o **layoff** (dias desde a última luta do mesmo
lutador), a **experiência acumulada** (contagem cumulativa point-in-time) e os **atributos
físicos vigentes** (altura, alcance, base). O bio do lutador não é presumido na frame longa:
é lido separadamente de ``fighters`` via ``load_fighters_bio`` e mesclado por ``fighter_id``.

Corretude point-in-time: ``layoff_days`` e ``career_bouts_before`` usam apenas as lutas
**anteriores** do lutador (``NA``/``0`` na estreia). O orquestrador reordena defensivamente
para a ordem canônica ``(fighter_id, event_date, bout_id)`` (sort estável) antes desses
cálculos, de forma que a corretude não dependa do caller. Tipos nullable (``Int64``) propagam
``NA`` explícito -- a imputação é decisão da fase 2 (decisão #3 da SPEC).

O DataFrame do Pandas é fronteira dinâmica (``pyproject.toml`` marca ``pandas.*`` como
``follow_imports=skip``): as funções públicas recebem/devolvem ``pd.DataFrame`` tipado. Os
nomes de coluna são centralizados nas constantes ``COL_*``/feature (fonte única do mapeamento
com o contrato da Slice 01).
"""

from __future__ import annotations

import logging

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Connection

from apps.bouts.models import BoutFighter, BoutFighterRound
from apps.fighters.models import Fighter

logger = logging.getLogger(__name__)

# Colunas de entrada esperadas na frame longa (Slice 01) e no bio (``load_fighters_bio``).
COL_FIGHTER_ID = "fighter_id"
COL_EVENT_DATE = "event_date"
COL_BOUT_ID = "bout_id"
COL_DATE_OF_BIRTH = "date_of_birth"
COL_ROUND = "round"
COL_ROUND_SIG_STRIKES_LANDED = "sig_strikes_landed"

# Colunas de feature produzidas por esta slice.
AGE_YEARS = "age_years"
LAYOFF_DAYS = "layoff_days"
CAREER_BOUTS_BEFORE = "career_bouts_before"
HEIGHT_CM = "height_cm"
REACH_CM = "reach_cm"
STANCE = "stance"

TRAJECTORY_FEATURES: list[str] = [
    AGE_YEARS,
    LAYOFF_DAYS,
    CAREER_BOUTS_BEFORE,
    HEIGHT_CM,
    REACH_CM,
    STANCE,
]

# Ordem canônica que garante a corretude point-in-time de layoff/experiência.
_SORT_KEY: list[str] = [COL_FIGHTER_ID, COL_EVENT_DATE, COL_BOUT_ID]

# Atributos físicos trazidos do bio para cada linha lutador-luta.
_PHYSICAL_COLUMNS: list[str] = [HEIGHT_CM, REACH_CM, STANCE]


def add_age(frame: pd.DataFrame) -> pd.DataFrame:
    """Devolve cópia da frame com ``age_years`` (anos completos na data da luta).

    Requer ``event_date`` e ``date_of_birth``. Ambas as datas são normalizadas para
    date-only (``dt.normalize``) antes do cálculo -- mitiga o risco de fuso: a idade é o
    ano do evento menos o ano de nascimento, decrementado quando o aniversário ainda não
    ocorreu no ano do evento (anti-off-by-one). ``NA`` (``Int64``) quando ``date_of_birth``
    é ausente (``NaT``). A frame de entrada não é mutada.
    """
    frame = frame.copy()
    event = pd.to_datetime(frame[COL_EVENT_DATE]).dt.normalize()
    birth = pd.to_datetime(frame[COL_DATE_OF_BIRTH]).dt.normalize()
    before_birthday = (event.dt.month < birth.dt.month) | (
        (event.dt.month == birth.dt.month) & (event.dt.day < birth.dt.day)
    )
    age = event.dt.year - birth.dt.year - before_birthday.astype("int64")
    frame[AGE_YEARS] = age.astype("Int64")  # ``NaT`` no nascimento -> ``NA``.
    return frame


def add_layoff(frame: pd.DataFrame) -> pd.DataFrame:
    """Devolve cópia da frame com ``layoff_days`` (dias desde a última luta do lutador).

    Assume a frame já na ordem canônica ``(fighter_id, event_date, bout_id)``. Usa
    ``shift(1)`` dentro do grupo do lutador (``sort=False`` preserva a ordem recebida) para
    obter a data da luta anterior; a estreia produz ``NaT`` -> ``NA`` (``Int64``). O layoff
    não vaza entre lutadores (agregação por grupo).
    """
    frame = frame.copy()
    previous_date = frame.groupby(COL_FIGHTER_ID, sort=False)[COL_EVENT_DATE].shift(1)
    delta = pd.to_datetime(frame[COL_EVENT_DATE]) - pd.to_datetime(previous_date)
    frame[LAYOFF_DAYS] = delta.dt.days.astype("Int64")
    return frame


def add_experience(frame: pd.DataFrame) -> pd.DataFrame:
    """Devolve cópia da frame com ``career_bouts_before`` (nº de lutas anteriores).

    Assume a frame já na ordem canônica. ``cumcount`` 0-indexado por lutador é exatamente o
    número de lutas **anteriores** (point-in-time, exclui a corrente): ``0`` na estreia. A
    contagem é isolada por ``fighter_id`` (grupo). Coluna ``int64`` (sempre definida).
    """
    frame = frame.copy()
    frame[CAREER_BOUTS_BEFORE] = (
        frame.groupby(COL_FIGHTER_ID, sort=False).cumcount().astype("int64")
    )
    return frame


def add_physical_attributes(frame: pd.DataFrame, fighters: pd.DataFrame) -> pd.DataFrame:
    """Devolve cópia da frame com altura/alcance/base vigentes por linha lutador-luta.

    ``merge`` left por ``fighter_id`` trazendo só as três colunas de bio físico, com
    ``validate="many_to_one"``: um ``fighter_id`` duplicado em ``fighters`` fragmentaria a
    série e é erro de entity resolution -- falha alto (``MergeError``), nunca mescla em
    silêncio. Tipos nullable: ``height_cm``/``reach_cm`` em ``Int64``, ``stance`` em
    ``string`` -- atributos ausentes viram ``NA`` explícito. A frame de entrada não é mutada.
    """
    merged = frame.merge(
        fighters[[COL_FIGHTER_ID, *_PHYSICAL_COLUMNS]],
        on=COL_FIGHTER_ID,
        how="left",
        validate="many_to_one",
    )
    merged[HEIGHT_CM] = merged[HEIGHT_CM].astype("Int64")
    merged[REACH_CM] = merged[REACH_CM].astype("Int64")
    merged[STANCE] = merged[STANCE].astype("string")
    return merged


def add_trajectory_features(long_frame: pd.DataFrame, fighters: pd.DataFrame) -> pd.DataFrame:
    """Enriquece a frame longa com idade, layoff, experiência e atributos físicos.

    ``fighters`` tem uma linha por lutador (id + bio), tipicamente de ``load_fighters_bio``.
    A frame é reordenada defensivamente para a ordem canônica ``(fighter_id, event_date,
    bout_id)`` (sort estável) antes de layoff/experiência, garantindo a corretude
    point-in-time independentemente da ordem recebida. O ``date_of_birth`` é mesclado apenas
    para calcular a idade e depois descartado (não é feature de saída). A frame de entrada
    não é mutada.
    """
    frame = long_frame.sort_values(by=_SORT_KEY, kind="stable").reset_index(drop=True)
    frame = frame.merge(
        fighters[[COL_FIGHTER_ID, COL_DATE_OF_BIRTH]],
        on=COL_FIGHTER_ID,
        how="left",
        validate="many_to_one",
    )
    frame = add_age(frame)
    frame = frame.drop(columns=[COL_DATE_OF_BIRTH])
    frame = add_layoff(frame)
    frame = add_experience(frame)
    frame = add_physical_attributes(frame, fighters)
    return frame


def load_fighters_bio(connection: Connection) -> pd.DataFrame:
    """Lê ``fighters`` (id + DOB + físico) do Postgres via Pandas, uma linha por lutador.

    Recebe a ``Connection`` da sessão (nos testes, ``session.connection()`` -- enxerga o
    estado semeado dentro da transação). Seleciona explicitamente as colunas (nunca
    ``SELECT *``) rotulando o id como ``fighter_id`` para casar com a frame longa.
    """
    statement = select(
        Fighter.id.label(COL_FIGHTER_ID),
        Fighter.date_of_birth,
        Fighter.height_cm,
        Fighter.reach_cm,
        Fighter.stance,
    )
    return pd.read_sql_query(statement, connection)


def load_round_stats(connection: Connection) -> pd.DataFrame:
    """Lê ``bout_fighter_rounds`` do Postgres via Pandas, uma linha por canto-por-round.

    Junta ``bout_fighter_rounds`` a ``bout_fighters`` para expor ``bout_id``/``fighter_id``
    (a granularidade round-a-round não carrega o canto além da FK). Recebe a ``Connection``
    da sessão (nos testes, ``session.connection()`` -- enxerga o round-a-round semeado dentro
    da transação, como ``load_fighters_bio``). Projeta apenas as colunas necessárias à
    dinâmica por round (golpes conectados por round), mantendo o conjunto mínimo (YAGNI).
    """
    statement = select(
        BoutFighter.bout_id.label(COL_BOUT_ID),
        BoutFighter.fighter_id.label(COL_FIGHTER_ID),
        BoutFighterRound.round.label(COL_ROUND),
        BoutFighterRound.sig_strikes_landed.label(COL_ROUND_SIG_STRIKES_LANDED),
    ).join(BoutFighterRound, BoutFighterRound.bout_fighter_id == BoutFighter.id)
    return pd.read_sql_query(statement, connection)
