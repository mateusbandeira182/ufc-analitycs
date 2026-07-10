"""Enums do app de lutadores."""

from __future__ import annotations

from enum import StrEnum


class Stance(StrEnum):
    """Base (guarda) do lutador."""

    ORTHODOX = "orthodox"
    SOUTHPAW = "southpaw"
    SWITCH = "switch"
