"""CLI do feature engineering: ``python -m ingestion.features build --stage long``.

``run_build`` é a lógica testável -- despacha por estágio e devolve a frame, reportando
a contagem e um preview via ``logging`` (``print`` é proibido, regra ``T20``). ``main`` é
fino: abre a ``Session`` real, chama ``run_build`` e **não commita** (a slice só lê -- é
código de análise sobre o granular, sem escrita). Espelha o padrão de ``ingestion/seed.py``.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

import pandas as pd
from sqlalchemy.orm import Session

from ingestion.features.long_frame import build_long_frame, read_granular
from ingestion.features.matchup import build_matchup_matrix
from ingestion.features.materialize import SOURCE, materialize_features
from ingestion.features.rolling import (
    COL_FIGHTER_ID,
    RECENT_FORM_FEATURES,
    add_recent_form_features,
    add_round_dynamics_features,
)
from ingestion.features.trajectory import (
    TRAJECTORY_FEATURES,
    add_trajectory_features,
    load_fighters_bio,
    load_round_stats,
)
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

# Estágios de leitura (read-only): constroem uma frame em memória, não persistem.
_STAGE_LONG = "long"
_STAGE_ROLLING = "rolling"
_STAGE_TRAJECTORY = "trajectory"
_STAGE_MATCHUP = "matchup"
_STAGES: tuple[str, ...] = (_STAGE_LONG, _STAGE_ROLLING, _STAGE_TRAJECTORY, _STAGE_MATCHUP)

# Estágio de escrita (Slice 05): roda a pipeline completa e persiste ``bout_features``.
_STAGE_MATERIALIZE = "materialize"
_CLI_STAGES: tuple[str, ...] = (*_STAGES, _STAGE_MATERIALIZE)


def _enriched_long_frame(session: Session) -> pd.DataFrame:
    """Frame longa enriquecida por rolling **e** trajetória (insumo do estágio matchup).

    Encadeia a leitura do granular, a frame longa (Slice 01), as features de forma
    recente e perfil de striking (Slice 02/M5), as de trajetória/contexto físico (Slice
    03) e a dinâmica por round (M5, a partir de ``bout_fighter_rounds``). Isolada para ser
    o ponto único de injeção nos testes do estágio matchup (sem tocar o Postgres).
    """
    frames = read_granular(session)
    df = build_long_frame(frames)
    df = add_recent_form_features(df)
    fighters = load_fighters_bio(session.connection())
    df = add_trajectory_features(df, fighters)
    round_stats = load_round_stats(session.connection())
    return add_round_dynamics_features(df, round_stats)


def _run_matchup_stage(session: Session) -> pd.DataFrame:
    """Constrói a matriz de confronto bout-level e reporta o baseline via ``logging``.

    Estatística descritiva: nenhum treino ou split. Loga o baseline do corner vermelho,
    a contagem de exclusões NC/draw e o shape (uma linha por bout). Devolve a frame.
    """
    result = build_matchup_matrix(_enriched_long_frame(session))
    logger.info(
        "Matchup: %d bouts, %d exclusões NC/draw, baseline red=%.4f",
        len(result.frame),
        result.excluded_no_result,
        result.red_corner_win_rate,
    )
    logger.info("Preview:\n%s", result.frame.head().to_string())
    return result.frame


def run_materialize(session: Session, source: str = SOURCE) -> int:
    """Roda a pipeline completa e materializa ``bout_features`` (Slice 05), sem commitar.

    Encadeia a frame longa enriquecida (rolling + trajetória) -> matriz de confronto
    (Slice 04) -> upsert idempotente por ``bout_id`` em ``bout_features``. **Não** commita --
    o ``main`` (produção) abre a transação e commita no sucesso; nos testes a sessão
    transacional assere e faz rollback. Devolve o número de linhas materializadas.
    """
    matrix = build_matchup_matrix(_enriched_long_frame(session))
    count = materialize_features(session, matrix, source=source)
    logger.info(
        "Materialização: %d linhas em bout_features (source=%s), %d exclusões NC/draw.",
        count,
        source,
        matrix.excluded_no_result,
    )
    return count


def run_build(session: Session, stage: str) -> pd.DataFrame:
    """Constrói a frame do estágio pedido sobre o granular lido na sessão.

    ``stage="long"`` produz a frame longa por lutador-luta; ``stage="rolling"`` a enriquece
    com as features de forma recente point-in-time; ``stage="trajectory"`` a enriquece com
    idade, layoff, experiência e atributos físicos; ``stage="matchup"`` pivota a frame
    longa enriquecida (rolling + trajetória) para uma linha por bout com diferenciais,
    alvo separado e baseline. Um estágio não suportado levanta ``ValueError`` claro (nada
    é escrito -- a operação é read-only). Reporta contagens e um preview via ``logging``.
    """
    if stage == _STAGE_MATCHUP:
        return _run_matchup_stage(session)
    if stage not in _STAGES:
        raise ValueError(f"Estágio desconhecido: {stage!r} (suportados: {list(_STAGES)})")
    frames = read_granular(session)
    df = build_long_frame(frames)
    logger.info(
        "Frame longa (%s): %d linhas a partir de %d bouts", stage, len(df), len(frames.bouts)
    )
    if stage == _STAGE_ROLLING:
        df = add_recent_form_features(df)
        n_estreias = int(df[COL_FIGHTER_ID].nunique())
        logger.info(
            "Features de forma recente: +%d colunas, %d estreias com NaN explícito",
            len(RECENT_FORM_FEATURES),
            n_estreias,
        )
    elif stage == _STAGE_TRAJECTORY:
        fighters = load_fighters_bio(session.connection())
        df = add_trajectory_features(df, fighters)
        logger.info(
            "Features de trajetória: +%d colunas (idade/layoff/experiência/físico)",
            len(TRAJECTORY_FEATURES),
        )
    logger.info("Preview:\n%s", df.head().to_string())
    return df


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta ``build --stage <estágio>`` do entrypoint de features."""
    parser = argparse.ArgumentParser(
        description="Feature engineering sobre o granular do UFC (leitura read-only via Pandas).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Constrói uma frame de features (em memória).")
    build.add_argument(
        "--stage",
        choices=list(_CLI_STAGES),
        default=_STAGE_LONG,
        help=(
            "Estágio da pipeline: 'long' (base), 'rolling' (forma recente), 'trajectory' "
            "(idade/layoff/experiência/físico), 'matchup' (matriz de confronto bout-level) ou "
            "'materialize' (roda a pipeline completa e persiste bout_features)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint de ``python -m ingestion.features build --stage <estágio>``.

    Abre a ``Session`` real. Estágios de leitura (long/rolling/trajectory/matchup) só
    constroem a frame em memória e **não commitam**. O estágio ``materialize`` roda a
    pipeline completa, persiste ``bout_features`` e **commita no sucesso** -- a idempotência
    é observável reexecutando (mesma contagem). A frame/resumo é reportada via ``logging``.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    with SessionLocal() as session:
        if args.stage == _STAGE_MATERIALIZE:
            run_materialize(session)
            session.commit()
        else:
            run_build(session, args.stage)


if __name__ == "__main__":
    main()
