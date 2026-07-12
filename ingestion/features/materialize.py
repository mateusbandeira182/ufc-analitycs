"""Materialização idempotente da matriz de confronto em ``bout_features``.

Núcleo da Slice 05 da SPEC 005 (M4 -- prontidão preditiva). ``materialize_features``
persiste a ``MatchupMatrix`` (saída da Slice 04) na tabela derivada ``bout_features`` --
um **cache reconstrutível**, uma linha por luta, chaveada por ``bout_id``. O granular
(``bouts``/``bout_fighters``) permanece a fonte de verdade e nunca é tocado aqui.

Idempotência por chave natural via ``INSERT ... ON CONFLICT (bout_id) DO UPDATE`` nativo
do Postgres. Diferente do seed de ``fighters`` (get-or-create de aplicação, porque
``date_of_birth`` é nullable e o unique index trata cada ``NULL`` como distinto), aqui
``bout_id`` é PK **não-nula**: o upsert por conflito é limpo e determinístico. Reprocessar
o mesmo granular mantém contagem e conteúdo (features/alvo/source); ``generated_at`` é
metadado de geração e pode ser refrescado.

Borda dinâmica: o ``frame`` do Pandas é fronteira dinâmica (``pyproject.toml`` marca
``pandas.*`` como ``follow_imports=skip``). Cada valor de feature é convertido para tipo
JSON nativo na borda (``NaN``/``NA`` -> ``None``; numpy -> ``int``/``float`` nativos) --
nenhum ``Any`` propaga e ``NaN`` (que não é JSON válido) nunca chega ao JSONB. O alvo
``winner_corner`` chega como ``"R"``/``"B"`` (convenção do matchup) e é mapeado para o enum
``Corner`` (``red``/``blue``).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import SupportsInt

import pandas as pd
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from apps.bouts.enums import Corner
from apps.features.models import BoutFeatures, FeaturePayload, FeatureValue
from ingestion.features.matchup import COL_BOUT_ID, MatchupMatrix

logger = logging.getLogger(__name__)

# Rastreio de origem do artefato derivado (RNF de rastreio da SPEC): toda linha carimba
# de qual pipeline veio. É reconstrutível a partir do granular a qualquer momento.
SOURCE = "feature-engineering"

# Alvo do matchup (``"R"``/``"B"``) -> enum ``Corner`` (``red``/``blue``) do banco.
_TARGET_TO_CORNER: dict[str, Corner] = {"R": Corner.RED, "B": Corner.BLUE}


def _is_missing(value: object) -> bool:
    """Ausência explícita na borda do DataFrame: ``None``, ``pandas.NA`` ou ``float('nan')``.

    Evita ``pandas.isna`` (cujos overloads tipados não aceitam ``object``): a checagem é
    estrutural e totalmente tipada, cobrindo o ``NaN`` de colunas float (``numpy.float64``
    subclassa ``float``) e o ``pandas.NA`` das colunas nullable (``Int64``/``string``).
    """
    if value is None or value is pd.NA:
        return True
    return isinstance(value, float) and math.isnan(value)


def _to_json_value(value: object) -> FeatureValue:
    """Converte um valor de feature do DataFrame para tipo JSON nativo na borda.

    ``NaN``/``NA``/``None`` -> ``None`` (nulo explícito, sem sentinela); ``str`` preservado;
    ``float`` (inclui ``numpy.float64``) e ``int`` nativos preservados; inteiros numpy
    (``numpy.int64``, que não subclassa ``int``) convertidos para ``int``. Um tipo
    inesperado falha visível em vez de vazar objeto não serializável para o JSONB.
    """
    if _is_missing(value):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, float):  # inclui numpy.float64 (subclasse de float)
        return float(value)
    if isinstance(value, int):  # inclui bool e int nativos
        return int(value)
    if isinstance(value, SupportsInt):  # numpy.int64 e afins
        return int(value)
    raise TypeError(f"Valor de feature não serializável em JSON: {value!r} ({type(value)!r})")


def _to_corner(value: object) -> Corner | None:
    """Mapeia o alvo ``"R"``/``"B"`` para o enum ``Corner``; ``NA`` (NC/draw) -> ``None``."""
    if _is_missing(value):
        return None
    return _TARGET_TO_CORNER[str(value)]


def materialize_features(session: Session, matrix: MatchupMatrix, source: str = SOURCE) -> int:
    """Persiste a matriz de confronto em ``bout_features`` via upsert por ``bout_id``.

    Uma linha por bout: ``features`` recebe só as ``feature_columns`` (``*_a``/``*_b``/
    ``*_diff``) como payload JSONB nativo; ``target_winner_corner`` fica separado do payload;
    ``source`` e ``generated_at`` (tz-aware UTC) carimbam a geração. Idempotente: reexecutar
    com o mesmo granular mantém contagem e conteúdo (o ``generated_at`` é refrescado). Escreve
    **apenas** em ``bout_features`` -- o granular permanece intocado. Devolve o número de
    linhas materializadas nesta execução.
    """
    frame = matrix.frame
    if frame.empty:
        logger.info("Materialização: matriz vazia, nada a persistir.")
        return 0

    generated_at = datetime.now(UTC)
    records: list[dict[str, object]] = []
    for row in frame.to_dict(orient="records"):
        features: FeaturePayload = {
            column: _to_json_value(row[column]) for column in matrix.feature_columns
        }
        records.append(
            {
                "bout_id": int(row[COL_BOUT_ID]),
                "features": features,
                "target_winner_corner": _to_corner(row[matrix.target_column]),
                "source": source,
                "generated_at": generated_at,
            }
        )

    stmt = insert(BoutFeatures).values(records)
    stmt = stmt.on_conflict_do_update(
        index_elements=["bout_id"],
        set_={
            "features": stmt.excluded.features,
            "target_winner_corner": stmt.excluded.target_winner_corner,
            "source": stmt.excluded.source,
            "generated_at": stmt.excluded.generated_at,
        },
    )
    session.execute(stmt)
    session.flush()

    logger.info("Materializadas %d linhas em bout_features (source=%s).", len(records), source)
    return len(records)
