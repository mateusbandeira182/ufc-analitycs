"""Enums do app de lutas."""

from __future__ import annotations

from enum import StrEnum


class BoutMethod(StrEnum):
    """Método de encerramento da luta.

    Empate é representado por ``winner_id`` nulo com o ``method`` que o produziu
    (tipicamente ``DECISION``); no contest usa ``NO_CONTEST``. Não há valor
    ``DRAW`` -- o empate mora na nulabilidade do vencedor, não no método.
    """

    KO_TKO = "ko_tko"
    SUBMISSION = "submission"
    DECISION = "decision"
    DQ = "dq"
    NO_CONTEST = "no_contest"


class Corner(StrEnum):
    """Canto do lutador na luta."""

    RED = "red"
    BLUE = "blue"
