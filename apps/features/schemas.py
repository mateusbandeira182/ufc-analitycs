"""Schemas Pydantic do cache as-of (SPEC 011, Slice 01).

``FeaturesFreshnessOut`` é o diagnóstico de defasagem embutido nas **três** respostas que leem
o cache as-of (D-09): ``GET /predict/matchup``, ``GET /predict/event/{id}`` e
``GET /fighters/{id}/stats``. Mora em ``apps/features`` -- e não em ``apps/predictions`` --
porque tem dois consumidores concretos hoje (predições e lutadores), e pendurá-lo num deles
faria o outro importar de um app alheio.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FeaturesFreshnessOut(BaseModel):
    """Diagnóstico de defasagem do cache as-of: um ``COUNT``, não um recomputo (RF-05).

    ``stale`` é ``n_bouts_current != n_bouts_source`` -- lutas entraram no granular desde a
    última materialização. A interface exibe o aviso **na seção que lê o dado defasado**
    (RF-06/D-05); o componente compartilhado que o renderiza nasce na Sprint 06, com a
    primeira consumidora real (D-10).

    **Emenda DP-05 ao texto da SPEC**: ``materialized_at`` é ``datetime | None``, enquanto a
    SPEC aprovada o escreve não-nulo. Com o cache nunca materializado não existe valor, e um
    campo obrigatório forçaria os três endpoints a falhar exatamente no estado que este
    diagnóstico existe para reportar (``n_bouts_source = 0``, ``stale = true``).
    """

    model_config = ConfigDict(from_attributes=True)

    materialized_at: datetime | None
    n_bouts_source: int
    n_bouts_current: int
    stale: bool
