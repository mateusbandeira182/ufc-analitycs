"""Leitura do granular via Pandas e construção da frame longa por lutador-luta.

Núcleo da Slice 01 da SPEC 005 (M4 -- prontidão preditiva). ``read_granular`` lê as
quatro tabelas granulares na ``Connection`` ligada à ``Session`` recebida -- ler pela
conexão da sessão (e não por um engine novo) é o que permite ao teste semear via ORM
e ler de volta os dados ainda não commitados na mesma transação. ``build_long_frame``
junta as quatro frames numa linha por participação (lutador em uma luta), deriva o
resultado do lutador e ordena a série de forma determinística.

O DataFrame do Pandas é fronteira dinâmica (``pyproject.toml`` marca ``pandas.*`` como
``follow_imports=skip``): as funções públicas devolvem ``pd.DataFrame`` tipado e toda
extração linha-a-linha é convertida para tipos do domínio na borda -- nenhum ``Any``
do DataFrame propaga. As stats são preservadas brutas por luta (nunca médias, ADR 0001).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter

# Resultado do lutador naquela luta. Não há ``BoutMethod.DRAW`` (ver ``apps/bouts/enums``):
# o empate mora na nulabilidade do vencedor, distinto do no contest pelo método.
BoutResult = Literal["win", "loss", "no_contest", "draw"]

# Splits de golpe wide (M5 -- ADR 0004, Sprint 02), projetados junto das 7 stats base.
# São contagens granulares POR LUTA (nunca médias): base das features de perfil de
# striking (share cabeça/corpo/perna, distância/clinch/solo) da Slice 06.
SPLIT_COLUMNS: list[str] = [
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
    "reversals",
]

# Colunas da frame longa (uma linha por ``bout_fighter``), na ordem de projeção.
LONG_FRAME_COLUMNS: list[str] = [
    "fighter_id",
    "fighter_name",
    "event_id",
    "event_name",
    "event_date",
    "bout_id",
    "corner",
    "result",
    "method",
    "round",
    "ending_time_seconds",
    "knockdowns",
    "sig_strikes_landed",
    "sig_strikes_attempted",
    "takedowns_landed",
    "takedowns_attempted",
    "submission_attempts",
    "control_time_seconds",
    *SPLIT_COLUMNS,
    "source",
]

# Chave de ordenação temporal determinística: por lutador, por data do evento, com
# desempate estável por ``bout_id`` quando duas lutas do lutador caem na mesma data.
_SORT_KEY: list[str] = ["fighter_id", "event_date", "bout_id"]


@dataclass(frozen=True)
class GranularFrames:
    """As quatro tabelas granulares lidas do Postgres, já com colunas nomeadas por chave.

    As colunas de identidade são rotuladas na origem (``bout_id``/``event_id``/
    ``fighter_id``) para que as junções em ``build_long_frame`` casem por nome natural,
    sem sufixos ``_x``/``_y``.
    """

    fighters: pd.DataFrame
    events: pd.DataFrame
    bouts: pd.DataFrame
    bout_fighters: pd.DataFrame


def read_granular(session: Session) -> GranularFrames:
    """Lê fighters/events/bouts/bout_fighters na ``Connection`` da sessão via Pandas.

    Seleciona explicitamente as colunas necessárias à frame longa (nunca ``SELECT *``)
    e as rotula pela chave natural. Usa ``session.connection()`` -- a mesma conexão da
    transação -- para enxergar dados semeados e ainda não commitados (essencial no teste).
    """
    con = session.connection()
    fighters = pd.read_sql_query(
        select(Fighter.id.label("fighter_id"), Fighter.name.label("fighter_name")), con
    )
    events = pd.read_sql_query(
        select(
            Event.id.label("event_id"),
            Event.name.label("event_name"),
            Event.date.label("event_date"),
        ),
        con,
    )
    bouts = pd.read_sql_query(
        select(
            Bout.id.label("bout_id"),
            Bout.event_id,
            Bout.winner_id,
            Bout.method,
            Bout.round,
            Bout.ending_time_seconds,
        ),
        con,
    )
    bout_fighters = pd.read_sql_query(
        select(
            BoutFighter.bout_id,
            BoutFighter.fighter_id,
            BoutFighter.corner,
            BoutFighter.knockdowns,
            BoutFighter.sig_strikes_landed,
            BoutFighter.sig_strikes_attempted,
            BoutFighter.takedowns_landed,
            BoutFighter.takedowns_attempted,
            BoutFighter.submission_attempts,
            BoutFighter.control_time_seconds,
            BoutFighter.total_strikes_landed,
            BoutFighter.total_strikes_attempted,
            BoutFighter.head_landed,
            BoutFighter.head_attempted,
            BoutFighter.body_landed,
            BoutFighter.body_attempted,
            BoutFighter.leg_landed,
            BoutFighter.leg_attempted,
            BoutFighter.distance_landed,
            BoutFighter.distance_attempted,
            BoutFighter.clinch_landed,
            BoutFighter.clinch_attempted,
            BoutFighter.ground_landed,
            BoutFighter.ground_attempted,
            BoutFighter.reversals,
            BoutFighter.source,
        ),
        con,
    )
    return GranularFrames(
        fighters=fighters, events=events, bouts=bouts, bout_fighters=bout_fighters
    )


def _result_for(winner_id: int | None, fighter_id: int, method: str) -> BoutResult:
    """Deriva o resultado do lutador naquela luta a partir do vencedor e do método.

    Vencedor igual ao lutador -> ``win``; vencedor presente e diferente -> ``loss``;
    vencedor nulo com método ``NO_CONTEST`` -> ``no_contest``; vencedor nulo com outro
    método -> ``draw`` (empate mora na nulabilidade do vencedor, ver ``apps/bouts/enums``).
    """
    if winner_id == fighter_id:
        return "win"
    if winner_id is not None:
        return "loss"
    if method == BoutMethod.NO_CONTEST:
        return "no_contest"
    return "draw"


def _derive_results(merged: pd.DataFrame) -> list[BoutResult]:
    """Aplica ``_result_for`` linha a linha, tipando cada valor na borda do DataFrame.

    ``winner_id`` pode chegar como nulo (float ``NaN``) quando há empate/no contest; a
    conversão explícita para ``int | None``/``int``/``str`` impede que ``Any`` do Pandas
    propague para o domínio.
    """
    results: list[BoutResult] = []
    for winner_raw, fighter_raw, method_raw in zip(
        merged["winner_id"], merged["fighter_id"], merged["method"], strict=True
    ):
        winner_id = None if pd.isna(winner_raw) else int(winner_raw)
        results.append(_result_for(winner_id, int(fighter_raw), str(method_raw)))
    return results


def build_long_frame(frames: GranularFrames) -> pd.DataFrame:
    """Junta as quatro frames numa linha por lutador-luta, com o resultado por linha.

    Parte de ``bout_fighters`` (o grão da participação) e junta ``bouts`` (por
    ``bout_id``), ``events`` (por ``event_id``) e ``fighters`` (por ``fighter_id``) em
    junções ``many_to_one``. Deriva a coluna ``result`` e projeta as colunas finais na
    ordem de ``LONG_FRAME_COLUMNS`` e ordena a série de forma determinística por
    ``fighter_id``/``event_date``/``bout_id`` (ordenação estável -- ``mergesort``).
    """
    merged = (
        frames.bout_fighters.merge(frames.bouts, on="bout_id", how="inner", validate="many_to_one")
        .merge(frames.events, on="event_id", how="inner", validate="many_to_one")
        .merge(frames.fighters, on="fighter_id", how="inner", validate="many_to_one")
    )
    merged["result"] = _derive_results(merged)
    return (
        merged[LONG_FRAME_COLUMNS].sort_values(by=_SORT_KEY, kind="stable").reset_index(drop=True)
    )
