"""DTOs que tipam o payload da Cito API na borda (Pydantic v2).

O JSON da Cito é uma fronteira dinâmica: é tipado aqui, na entrada, antes de qualquer
uso no domínio -- nenhum ``Any`` propaga para dentro da ingestão. O evento
(``CitoEvent``) traz os metadados, os cantos das lutas e o **resultado** de cada luta
(método/round/tempo/vencedor); as stats granulares por canto vêm à parte, de
``GET /bouts/{boutId}/stats`` (``CitoBoutStats``).

Convenção de canto: em ``CitoBout.corners`` o índice 0 é o canto **vermelho** (red) e o
índice 1 é o **azul** (blue) -- é a fonte da verdade da atribuição de canto, com a qual
os rótulos ``corner`` das stats devem concordar.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, field_validator

from apps.bouts.enums import Corner
from apps.fighters.enums import Stance


class CitoCorner(BaseModel):
    """Um canto de uma luta: o lutador identificado por ``slug`` e nome de exibição."""

    model_config = ConfigDict(extra="ignore")

    slug: str
    name: str


class CitoBout(BaseModel):
    """Uma luta do card: id externo da Cito, o resultado e os dois cantos.

    O resultado (``method``/``finish_round``/``finish_time_seconds``/``weight_class``/
    ``winner_slug``) vem do payload do evento (``GET /events``), não das stats. Todos são
    opcionais: uma luta sem resultado (cartão futuro) degrada para vencedor/round nulos e
    método ``NO_CONTEST`` no mapeamento (ver ``ingestion.incremental.map_bout_core``). O
    ``winner_slug`` referencia o ``slug`` de um dos cantos.
    """

    model_config = ConfigDict(extra="ignore")

    bout_id: str
    corners: tuple[CitoCorner, CitoCorner]
    method: str | None = None
    finish_round: int | None = None
    finish_time_seconds: int | None = None
    weight_class: str | None = None
    winner_slug: str | None = None


class CitoFighterStats(BaseModel):
    """Box-score granular de um canto numa luta (``GET /bouts/{boutId}/stats``).

    Uma linha por lutador-por-luta (long): o ``corner`` liga ao ``fighter_id`` já resolvido
    e as métricas são as daquela luta (nunca médias). Métrica ausente no payload vira
    ``None`` -- não se inventa zero.
    """

    model_config = ConfigDict(extra="ignore")

    corner: Corner
    fighter_slug: str
    knockdowns: int | None = None
    sig_strikes_landed: int | None = None
    sig_strikes_attempted: int | None = None
    takedowns_landed: int | None = None
    takedowns_attempted: int | None = None
    submission_attempts: int | None = None
    control_time_seconds: int | None = None


class CitoBoutStats(BaseModel):
    """As stats de uma luta: o id externo da Cito e a linha de cada canto (red/blue)."""

    model_config = ConfigDict(extra="ignore")

    bout_id: str
    fighters: list[CitoFighterStats]


class CitoEvent(BaseModel):
    """Um evento do UFC vindo da Cito: metadados + a lista de lutas com os dois cantos."""

    model_config = ConfigDict(extra="ignore")

    event_id: str
    name: str
    date: date  # data de calendário do evento (sem instante/timezone)
    bouts: list[CitoBout]


class CitoFighter(BaseModel):
    """Perfil de um lutador da Cito (``GET /fighters/{slug}``).

    A ``date_of_birth`` é a base do desempate da entity resolution cross-source; pode
    vir ausente (``None``), caso em que a política de matching degrada para o nome
    normalizado (ver ``ingestion.entity_resolution``). O cartel (``wins``/``losses``/
    ``draws``) alimenta os campos NOT NULL de ``Fighter`` na criação de um lutador novo;
    ausente no payload, cada um degrada para ``0``.
    """

    model_config = ConfigDict(extra="ignore")

    slug: str
    name: str
    date_of_birth: date | None = None
    nickname: str | None = None
    height_cm: int | None = None
    reach_cm: int | None = None
    stance: Stance | None = None
    wins: int = 0
    losses: int = 0
    draws: int = 0

    @field_validator("stance", mode="before")
    @classmethod
    def _coerce_stance(cls, value: object) -> Stance | None:
        """Normaliza o rótulo de stance da Cito ao enum; fora do enum (ou vazio) -> ``None``.

        A grafia da Cito pode variar em caixa (``"Orthodox"``); o rótulo é baixado à caixa
        antes de casar com o enum. Rótulos não previstos degradam para ``None`` em vez de
        estourar a validação -- consistente com o tratamento de stance do seed (ADR 0002).
        """
        if value is None or isinstance(value, Stance):
            return value
        try:
            return Stance(str(value).strip().casefold())
        except ValueError:
            return None
