"""Model do artefato derivado ``bout_features`` (cache reconstrutível por luta).

Núcleo da Slice 05 da SPEC 005 (M4 -- prontidão preditiva). ``BoutFeatures`` materializa
a matriz de confronto (saída da Slice 04) como um **cache reconstrutível**, uma linha por
luta, chaveada por ``bout_id``. **Não é fonte de verdade**: o granular
(``bouts``/``bout_fighters``) permanece a fonte; esta tabela pode ser dropada e
reconstruída a qualquer momento a partir do granular, de forma idempotente.

As features (``*_a``/``*_b``/``*_diff``) vivem num payload JSONB tipado -- ``FeatureValue``
é um alias concreto (``float | int | str | None``), nunca ``Any`` (mypy --strict). O alvo
``target_winner_corner`` fica **separado** das features e reusa o enum ``corner`` já
existente (dono: ``bout_fighters``), sem recriar o tipo. ``source`` e ``generated_at``
carimbam a origem/geração do artefato (RNF de rastreio da SPEC).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apps.bouts.enums import Corner
from mma_analytics.db import Base

# Fronteira JSONB tipada com alias concreto -- mypy --strict proíbe ``Any`` no payload.
FeatureValue = float | int | str | None
FeaturePayload = dict[str, FeatureValue]


class BoutFeatures(Base):
    """Cache reconstrutível da matriz de confronto por luta (não é fonte de verdade).

    O granular (``bouts``/``bout_fighters``) é a fonte; esta tabela é derivada e
    reconstruível a qualquer momento. Chave natural: ``bout_id`` (PK/FK não-nula), o
    que torna o upsert ``ON CONFLICT (bout_id) DO UPDATE`` determinístico.
    """

    __tablename__ = "bout_features"

    bout_id: Mapped[int] = mapped_column(ForeignKey("bouts.id"), primary_key=True)
    # Payload de features as-of (``*_a``/``*_b``/``*_diff``); o alvo NÃO entra aqui.
    features: Mapped[FeaturePayload] = mapped_column(JSONB)
    # Alvo separado das features; reusa o enum ``corner`` existente (create_type=False)
    # com os mesmos valores nativos ('red'/'blue') do enum de ``bout_fighters``.
    target_winner_corner: Mapped[Corner | None] = mapped_column(
        Enum(
            Corner,
            name="corner",
            create_type=False,
            values_callable=lambda enum: [m.value for m in enum],
        )
    )
    source: Mapped[str] = mapped_column(String(32))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
