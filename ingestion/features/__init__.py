"""Feature engineering sobre o granular do UFC (M4 -- prontidão preditiva).

Camada de análise que lê o dado granular já persistido (fighters/events/bouts/
bout_fighters) via Pandas e o materializa em memória como frame longa por
lutador-luta -- a série temporal que as slices seguintes usam para as features.
A Slice 02 acrescenta as features de forma recente (rolling/expanding) point-in-time;
a Slice 03 as de trajetória de carreira e contexto físico (idade/layoff/experiência/
atributos físicos). Não altera o schema nem persiste artefato ainda (YAGNI).
"""

from __future__ import annotations

from ingestion.features.rolling import (
    RECENT_FORM_FEATURES,
    WINDOW_RECENT,
    add_recent_form_features,
)
from ingestion.features.trajectory import (
    TRAJECTORY_FEATURES,
    add_trajectory_features,
    load_fighters_bio,
)

__all__ = [
    "RECENT_FORM_FEATURES",
    "TRAJECTORY_FEATURES",
    "WINDOW_RECENT",
    "add_recent_form_features",
    "add_trajectory_features",
    "load_fighters_bio",
]
