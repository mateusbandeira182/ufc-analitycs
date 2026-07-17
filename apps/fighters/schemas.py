"""Schemas Pydantic de saída do app de fighters.

``FighterOut`` é o contrato público de leitura, reusado em list e detail. Expõe
identidade, atributos lentos, o cartel snapshot e ``source`` (RF-09). Lista os
campos explicitamente para nunca vazar ``name_normalized`` -- chave interna de
dedup do seed, não contrato público.

``FighterBoutOut`` é o item do histórico do lutador (Slice 04): resumo do evento
e resultado da luta, mais as stats granulares do canto consultado via reuso de
``BoutFighterStatsOut`` (Slice 03) -- sem redefinir os campos de stats -- e o
adversário daquela luta (``FighterOpponentOut``: id e nome do outro canto) para a
SPA renderizar o confronto.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict

from apps.bouts.enums import BoutMethod
from apps.bouts.schemas import BoutFighterStatsOut
from apps.fighters.enums import Stance


class FighterOut(BaseModel):
    """Representação pública de um lutador."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    nickname: str | None
    date_of_birth: date | None
    height_cm: int | None
    reach_cm: int | None
    stance: Stance | None
    weight_kg: float | None  # atributo físico do M5 (ADR 0004), nullable
    wins: int
    losses: int
    draws: int
    source: str


class FighterOpponentOut(BaseModel):
    """Adversário do lutador naquela luta: o outro canto (só identidade)."""

    fighter_id: int
    name: str


class FighterBoutOut(BaseModel):
    """Uma luta do histórico do lutador, com as stats daquele lutador naquela luta."""

    bout_id: int
    event_id: int
    event_name: str
    event_date: date
    method: BoutMethod
    round: int | None
    ending_time_seconds: int | None
    won: bool  # ``winner_id == fighter_id`` consultado (empate/no contest -> False)
    stats: BoutFighterStatsOut  # stats granulares do canto consultado (reuso Slice 03)
    opponent: FighterOpponentOut | None  # o outro canto; ``None`` em dados sujos


class StrikingProfileOut(BaseModel):
    """Perfil de striking agregado on demand: shares de golpe conectado por grupo.

    Dois grupos que somam 1 quando definidos: alvo (cabeça/corpo/perna) e posição
    (distância/clinch/solo). Cada share é razão de somas na carreira; denominador
    zero -> ``None`` (nunca ``inf``/``NaN`` no JSON).
    """

    share_head: float | None
    share_body: float | None
    share_leg: float | None
    share_distance: float | None
    share_clinch: float | None
    share_ground: float | None


class FighterStatsOut(BaseModel):
    """Estatísticas resumidas do lutador, computadas on demand (Slice 06).

    Não inclui ``source``: RF-09 se aplica a schemas de item; o agregado é
    computado e mistura origens (kaggle/cito), não é um registro único (decisão
    do plano 003-06). Médias ``None`` quando não há valor a agregar.
    ``striking_profile`` traz os shares de golpe por alvo/posição, agregados on
    demand a partir dos splits granulares do M5.
    """

    fighter_id: int
    bouts_counted: int
    avg_sig_strikes_landed: float | None
    avg_takedowns_landed: float | None
    avg_control_time_seconds: float | None
    wins_by_method: dict[str, int]
    striking_profile: StrikingProfileOut
