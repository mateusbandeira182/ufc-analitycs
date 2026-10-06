"""Leitura do cache as-of e a guarda **barata** de defasagem (SPEC 011, Slice 01).

Camada de backend do M9. Duas responsabilidades, as duas sobre ``fighter_asof_features``:

- ``read_asof_features`` -- o ``SELECT`` que o serving faz em vez de reconstruir a pipeline
  point-in-time a cada request (RF-04).
- ``count_source_bouts`` / ``get_features_freshness`` -- o diagnóstico de defasagem que
  acompanha **toda** resposta que lê esse cache (RF-05). É um ``COUNT``, nunca um recomputo.

Mora em ``apps/`` e não em ``ingestion/`` porque é consulta de leitura servida por request --
o módulo de ingestão é quem **escreve** o cache (``ingestion.features.asof``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutStatus
from apps.bouts.models import Bout
from apps.features.models import FighterAsOfFeatures


@dataclass(frozen=True)
class FeaturesFreshness:
    """Diagnóstico barato de defasagem do cache as-of: um ``COUNT``, não um recomputo.

    Irmã de ``ingestion.features.freshness.FreshnessReport``, com outro orçamento e outro
    consumidor. Aquela recompõe a pipeline inteira e compara coluna a coluna -- correto no
    gate de **treino**, proibitivo num request HTTP. Esta compara ``n_bouts_source`` (gravado
    na materialização) com a contagem real de agora, e serve a toda resposta que lê o cache.
    As duas coexistem de propósito.

    ``materialized_at`` é ``None`` quando o cache **nunca** foi materializado: é o estado que
    o diagnóstico existe justamente para reportar, e exigir um valor obrigatório aqui faria os
    três endpoints falharem exatamente nele.
    """

    materialized_at: datetime | None
    n_bouts_source: int
    n_bouts_current: int

    @property
    def stale(self) -> bool:
        """``True`` quando o granular andou desde a última materialização.

        Cache vazio sobre banco vazio (``0`` contra ``0``) **não** é defasagem: é ausência de
        dado, e alarmar ali gastaria a credibilidade do aviso antes do primeiro uso real.
        """
        return self.n_bouts_current != self.n_bouts_source


def count_source_bouts(session: Session) -> int:
    """Lutas que a pipeline as-of consome -- o denominador da guarda de defasagem.

    O RECORTE TEM DE SER IDÊNTICO ao de ``ingestion.features.long_frame.read_granular``: as
    duas respondem a mesma pergunta ("quais lutas alimentam as features"), e um ``COUNT`` mais
    largo que a pipeline marcaria o cache como defasado para sempre -- um aviso permanente é
    um aviso ignorado.

    O filtro ``status='completed'`` entrou na Sprint 02 da SPEC 011, no mesmo passo em que
    ``read_granular`` passou a filtrá-lo: sem ele, a luta agendada que a Slice 04 escreve
    contaria aqui e não lá, e o cache nasceria defasado toda vez que um card futuro fosse
    ingerido. O teste-guarda de ``apps/features/tests/test_selectors.py`` falha alto se os
    dois divergirem.
    """
    return int(
        session.execute(
            select(func.count()).select_from(Bout).where(Bout.status == BoutStatus.COMPLETED)
        ).scalar_one()
    )


def get_features_freshness(session: Session) -> FeaturesFreshness:
    """Compara a contagem gravada na materialização com a contagem real de agora (RF-05).

    ``materialized_at`` e ``n_bouts_source`` são agregados (``max``) porque toda linha de uma
    mesma execução carrega o mesmo carimbo -- lê-los de uma linha arbitrária daria o mesmo
    valor, mas dependeria de qual linha o banco devolvesse primeiro.
    """
    materialized_at, n_bouts_source = session.execute(
        select(
            func.max(FighterAsOfFeatures.materialized_at),
            func.max(FighterAsOfFeatures.n_bouts_source),
        )
    ).one()
    return FeaturesFreshness(
        materialized_at=materialized_at,
        n_bouts_source=int(n_bouts_source or 0),
        n_bouts_current=count_source_bouts(session),
    )


def read_asof_features(
    session: Session, fighter_ids: Sequence[int]
) -> dict[int, FighterAsOfFeatures]:
    """Linhas do cache as-of dos lutadores pedidos, indexadas por ``fighter_id``.

    Id sem linha simplesmente não aparece no resultado -- é assim que o serving distingue
    "lutador conhecido" de "lutador sem histórico" sem ramificar em exceção. Lista vazia não
    consulta o banco (sustenta o card sem lutas).
    """
    if not fighter_ids:
        return {}
    rows = (
        session.execute(
            select(FighterAsOfFeatures).where(FighterAsOfFeatures.fighter_id.in_(list(fighter_ids)))
        )
        .scalars()
        .all()
    )
    return {int(row.fighter_id): row for row in rows}
