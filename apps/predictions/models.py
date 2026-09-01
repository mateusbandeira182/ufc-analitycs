"""Model do registro de predições (``bout_predictions``).

Núcleo da Slice 07 da SPEC 007 (M6 -- RF-10): dar **memória** às predições, uma linha
por (luta, versão do modelo), para que a curva de refino do walk-forward exista como
dado e não como log perdido.

Duas decisões de modelagem sustentam a tabela:

- **O acerto não é coluna.** Aqui guarda-se o que foi *previsto*; o que *ocorreu* já é
  dado de ``bouts.winner_id``, a única fonte da verdade do resultado. O acerto é a
  comparação entre os dois, derivada sob demanda em
  ``apps.predictions.selectors.get_prediction_outcomes``. Persistir "acertou" seria
  pré-agregação destrutiva e ficaria silenciosamente errado se o resultado fosse
  corrigido depois (princípio de granularidade do ``CLAUDE.md``, ADR 0001).
- **Unicidade em ``(bout_id, model_version)``.** Prever a mesma luta com o mesmo modelo é
  idempotente (não duplica); prever com um modelo novo gera linha nova. É exatamente isso
  que faz a série temporal por versão de modelo existir. Ambas as colunas são NOT NULL, o
  que torna o upsert ``ON CONFLICT (bout_id, model_version) DO UPDATE`` determinístico
  (ver ``apps.predictions.services.record_prediction``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mma_analytics.db import Base


class BoutPrediction(Base):
    """Uma predição por (luta, versão do modelo). O acerto **não** é coluna.

    Chave natural: ``(bout_id, model_version)``. A PK é o serial ``id`` porque uma
    mesma luta acumula várias linhas ao longo do tempo -- uma por versão de modelo.
    """

    __tablename__ = "bout_predictions"
    __table_args__ = (UniqueConstraint("bout_id", "model_version", name="uq_bout_prediction"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bout_id: Mapped[int] = mapped_column(ForeignKey("bouts.id"), index=True)
    # Nullable por simetria com ``bouts.winner_id``: a comparação previsto x ocorrido é
    # de três estados nos dois lados (sem vencedor previsto não há acerto nem erro).
    predicted_winner_id: Mapped[int | None] = mapped_column(ForeignKey("fighters.id"))
    prob_predicted_winner: Mapped[float]  # probabilidade atribuída ao vencedor previsto
    # ``trained_at`` do artefato, ISO-8601, **exatamente como persistido**. Reformatar
    # este valor (normalizar fuso, truncar microssegundos) quebraria a unicidade da chave
    # natural em silêncio -- ver ``analysis.model.LoadedModel.trained_at``.
    model_version: Mapped[str] = mapped_column(String(64))
    n_features: Mapped[int]
    # Instante tz-aware (UTC), como ``apps.features.models.BoutFeatures.generated_at``:
    # sem ``DateTime(timezone=True)`` a coluna nasceria naive.
    predicted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32))  # "walk_forward" | "api"
