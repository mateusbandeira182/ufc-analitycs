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

O M9 (SPEC 011, Slice 01) acrescenta a **irmã no grão lutador**: ``FighterAsOfFeatures``, o
cache as-of "agora" que o serving lê em vez de reconstruir a pipeline a cada request. Mesma
tese (cache reconstrutível, chave natural não-nula, upsert por conflito), outro grão -- e com
duas colunas JSONB, porque o insumo do bloco D3 é feature do PAR e não cabe no vetor por
lutador (ver ``StyleHistory``).
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

StyleHistory = list[FeaturePayload]
"""Histórico de estilos enfrentados: uma entrada por luta passada do lutador (D-15).

Reusa ``FeaturePayload`` como grão da entrada -- fronteira JSONB tipada, nunca ``Any``.
Vive em coluna PRÓPRIA, e não como chaves extras do vetor de features, para que ninguém a
alinhe a ``feature_names`` e quebre o serving: ``similar_style_win_rate_prior`` (o D3) é
feature do PAR -- depende de quem é o adversário --, não do lutador.
"""


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


class FighterAsOfFeatures(Base):
    """Cache as-of por lutador: o vetor de features "agora", uma linha por lutador.

    Cache reconstrutível, nunca fonte da verdade (ADR 0001): o granular
    (``bouts``/``bout_fighters``) permanece a fonte e esta tabela pode ser dropada e
    reconstruída a qualquer momento por
    ``python -m ingestion.features build --stage materialize-asof``. Irmã de
    ``BoutFeatures`` no grão **lutador** (aquela é no grão luta), e é o que transforma a
    predição de confronto de uma reconstrução completa da pipeline num ``SELECT`` de duas
    linhas (RF-04 da SPEC 011).

    ``n_bouts_source`` e ``materialized_at`` são o insumo da guarda **barata** de defasagem
    (RF-05): a API compara a contagem gravada aqui com o ``count(*)`` real e reporta a
    diferença em toda resposta que lê o cache -- é a cicatriz do ``bout_features`` (21
    features descartadas por meses em silêncio) prevenida no grão lutador.
    """

    __tablename__ = "fighter_asof_features"

    fighter_id: Mapped[int] = mapped_column(ForeignKey("fighters.id"), primary_key=True)
    # Vetor as-of alinhável a ``feature_names``; a feature do PAR (D3) NÃO entra aqui.
    features: Mapped[FeaturePayload] = mapped_column(JSONB)
    # Insumo do D3, em coluna separada de propósito (D-15) -- ver ``StyleHistory``.
    style_history: Mapped[StyleHistory] = mapped_column(JSONB)
    # Lutas que a pipeline as-of consumiu nesta materialização (denominador da guarda).
    n_bouts_source: Mapped[int]
    source: Mapped[str] = mapped_column(String(32))
    materialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
