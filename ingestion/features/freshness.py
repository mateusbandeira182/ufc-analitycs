"""Guarda de defasagem do cache derivado ``bout_features`` (Slice 01 da SPEC 007).

``bout_features`` é **cache reconstrutível**, nunca fonte da verdade (ADR 0001): o granular
(``bouts``/``bout_fighters``) é a fonte, e o cache só é reescrito por
``python -m ingestion.features build --stage materialize``. Nada aciona essa
re-materialização automaticamente -- foi assim que o backfill do M5 escreveu os splits de
golpe no granular e o cache ficou um ano para trás, com as seis features de share de
striking 100% nulas. O único sinal era o ``logger.warning`` de
``analysis.dataset._drop_all_nan_feature_columns``, que **avisa e segue**.

Este módulo transforma esse aviso silencioso em falha alto. A guarda **recompõe a pipeline
read-only** e confronta o resultado com o cache persistido -- o mesmo trabalho do estágio
``materialize``, menos a escrita. Comparar ``generated_at`` contra a escrita mais recente do
granular foi descartado: ``bouts``/``bout_fighters`` não têm coluna de timestamp de escrita,
e criá-la exigiria migration.

Três sinais de defasagem, todos read-only:

1. ``bout_id_diff`` -- lutas presentes só no granular ou só no cache (ingestão nova sem
   re-materialização, ou cache órfão).
2. ``column_diff`` -- chave de feature presente só de um lado (a pipeline ganhou ou perdeu
   uma feature depois da última materialização).
3. ``column_drift`` -- coluna presente nos dois lados com **contagem de não-nulos
   divergente**. É o sintoma real: ``share_head_r3_diff`` com 0 não-nulos no cache e
   milhares no recomputo.

Valores de feature **não** são comparados por igualdade: float round-trip por JSONB não é
confiável, e a contagem de não-nulos já captura o caso real com diagnóstico melhor.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.features.models import BoutFeatures, FeaturePayload
from ingestion.features.matchup import COL_BOUT_ID, MatchupMatrix


class StaleFeatureCacheError(RuntimeError):
    """``bout_features`` está defasado em relação ao granular; re-materialize antes de treinar."""


@dataclass(frozen=True)
class ColumnDrift:
    """Coluna presente nos dois lados com contagem de não-nulos divergente."""

    column: str
    cached_non_null: int
    recomputed_non_null: int


@dataclass(frozen=True)
class FreshnessReport:
    """Diagnóstico da comparação entre o cache persistido e o recomputo da pipeline.

    ``bout_id_diff`` e ``column_diff`` são diferenças **simétricas** (ordenadas, para
    diagnóstico determinístico): não distinguem de que lado falta, porque qualquer um dos
    casos significa a mesma coisa -- o cache não reflete o granular vivo.
    """

    cached_bouts: int
    recomputed_bouts: int
    bout_id_diff: tuple[int, ...]
    column_diff: tuple[str, ...]
    column_drift: tuple[ColumnDrift, ...]

    @property
    def is_stale(self) -> bool:
        """``True`` quando qualquer um dos três sinais de defasagem aparece."""
        return bool(self.bout_id_diff or self.column_diff or self.column_drift)


def read_cached_payloads(session: Session) -> dict[int, FeaturePayload]:
    """Lê o payload de features persistido em ``bout_features``, indexado por ``bout_id``."""
    stmt = select(BoutFeatures.bout_id, BoutFeatures.features)
    return {int(bout_id): dict(features) for bout_id, features in session.execute(stmt).all()}


def _cached_non_null_counts(payloads: dict[int, FeaturePayload]) -> dict[str, int]:
    """Não-nulos por chave de feature no cache; uma chave ausente conta como nula."""
    counts: dict[str, int] = {}
    for payload in payloads.values():
        for column, value in payload.items():
            counts[column] = counts.get(column, 0) + (value is not None)
    return counts


def _recomputed_non_null_counts(matrix: MatchupMatrix) -> dict[str, int]:
    """Não-nulos por coluna de feature no recomputo (``notna`` do Pandas na borda).

    ``notna`` casa com a semântica de ausência da materialização (``NaN``/``NA`` -> ``None``),
    então as duas contagens são comparáveis.
    """
    frame = matrix.frame
    return {column: int(frame[column].notna().sum()) for column in matrix.feature_columns}


def check_cache_freshness(session: Session, matrix: MatchupMatrix) -> FreshnessReport:
    """Confronta o cache persistido com a matriz recomputada e devolve o diagnóstico.

    Read-only: não escreve nem commita nada. A matriz vem do chamador (o CLI recompõe a
    pipeline), o que mantém este módulo testável sem tocar o Postgres duas vezes.
    """
    payloads = read_cached_payloads(session)
    cached_bout_ids = set(payloads)
    recomputed_bout_ids = {int(bout_id) for bout_id in matrix.frame[COL_BOUT_ID]}

    cached_counts = _cached_non_null_counts(payloads)
    recomputed_counts = _recomputed_non_null_counts(matrix)
    shared_columns = sorted(set(cached_counts) & set(recomputed_counts))

    return FreshnessReport(
        cached_bouts=len(cached_bout_ids),
        recomputed_bouts=len(recomputed_bout_ids),
        bout_id_diff=tuple(sorted(cached_bout_ids ^ recomputed_bout_ids)),
        column_diff=tuple(sorted(set(cached_counts) ^ set(recomputed_counts))),
        column_drift=tuple(
            ColumnDrift(
                column=column,
                cached_non_null=cached_counts[column],
                recomputed_non_null=recomputed_counts[column],
            )
            for column in shared_columns
            if cached_counts[column] != recomputed_counts[column]
        ),
    )
