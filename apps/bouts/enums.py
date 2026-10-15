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


class BoutStatus(StrEnum):
    """A luta já aconteceu, ou está apenas agendada?

    Existe porque ``bouts`` passou a comportar luta **sem** resultado (card por vir, M9):
    sem esta coluna, a pipeline de features e o histórico do lutador tratariam uma luta que
    ainda não ocorreu como se fosse passado. ``COMPLETED`` é o default do model -- é ele que
    preserva a semântica de toda escrita que existia antes da coluna.
    """

    COMPLETED = "completed"
    SCHEDULED = "scheduled"


class Corner(StrEnum):
    """Canto do lutador na luta."""

    RED = "red"
    BLUE = "blue"
