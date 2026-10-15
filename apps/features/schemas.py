"""Schemas Pydantic do cache as-of (SPEC 011, Slice 01).

``FeaturesFreshnessOut`` é o diagnóstico de defasagem embutido nas **três** respostas que leem
o cache as-of (D-09): ``GET /predict/matchup``, ``GET /predict/event/{id}`` e
``GET /fighters/{id}/stats``. Mora em ``apps/features`` -- e não em ``apps/predictions`` --
porque tem dois consumidores concretos hoje (predições e lutadores), e pendurá-lo num deles
faria o outro importar de um app alheio.

``CurrentFormOut`` (Slice 06) mora ao lado dele pela mesma razão: os dois descrevem o contrato
do **cache as-of**, não o agregado on demand de ``apps/fighters``.
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


class CurrentFormOut(BaseModel):
    """Forma atual as-of do lutador, lida de ``fighter_asof_features`` (RF-15).

    Mesma tabela que alimenta a predição: perfil e palpite contam a mesma história **por
    construção**, e não por dois cálculos que se espera que coincidam.

    Todo campo é nulável -- estreia, janela sem o dado e denominador zero produzem ausência,
    e ausência **não** é zero (RF-03 da SPEC 009). O bloco inteiro vem nulo quando o lutador
    não tem linha no cache; nesse caso ``features_freshness`` é quem diz se o motivo é falta
    de histórico ou cache nunca materializado.

    Os nomes replicam os das features de propósito (DP-08 do Plano 011-06): a seção da tela os
    rotula em pt-BR, e renomeá-los no contrato criaria uma tradução cuja deriva seria muda.

    ``extra="forbid"`` é load-bearing: uma divergência entre este schema e
    ``selectors.CURRENT_FORM_FEATURES`` falha na validação em vez de servir campo a menos. O
    teste-guarda de ``apps/features/tests/test_selectors.py`` antecipa essa falha para o gate.
    """

    model_config = ConfigDict(extra="forbid")

    # Janela das três lutas anteriores.
    sig_strikes_landed_pm_r3: float | None
    sig_strikes_absorbed_pm_r3: float | None
    takedowns_landed_avg_r3: float | None
    takedown_defense_r3: float | None
    control_time_avg_r3: float | None
    # Tendência: a janela de três menos a gêmea de carreira (positivo = acima da carreira).
    sig_strikes_landed_pm_trend: float | None
    sig_strikes_absorbed_pm_trend: float | None
    takedowns_landed_avg_trend: float | None
    takedown_defense_trend: float | None
    control_time_avg_trend: float | None
    # Desgaste acumulado na carreira anterior -- não são números da janela de três.
    career_minutes_before: float | None
    ko_losses_prior: float | None
    submission_losses_prior: float | None
