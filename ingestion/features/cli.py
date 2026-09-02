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

from ingestion.features.freshness import (
    FreshnessReport,
    StaleFeatureCacheError,
    check_cache_freshness,
)
from ingestion.features.long_frame import build_long_frame, read_granular
from ingestion.features.matchup import (
    BOUT_LEVEL_FEATURE_COLUMNS,
    MatchupMatrix,
    build_matchup_matrix,
)
from ingestion.features.materialize import SOURCE, materialize_features
from ingestion.features.rolling import (
    COL_FIGHTER_ID,
    RECENT_FORM_FEATURES,
    add_recent_form_features,
    add_round_dynamics_features,
    add_stance_history_features,
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

# Subcomandos do CLI. ``check`` (SPEC 007) é read-only e não tem ``--stage``, por isso o
# dispatch do ``main`` olha ``args.command`` antes de ``args.stage``.
_COMMAND_BUILD = "build"
_COMMAND_CHECK = "check"


def _enriched_long_frame(session: Session) -> pd.DataFrame:
    """Frame longa enriquecida por rolling **e** trajetória (insumo do estágio matchup).

    Encadeia a leitura do granular, a frame longa (Slice 01), as features de forma
    recente e perfil de striking (Slice 02/M5), as de trajetória/contexto físico (Slice
    03), o histórico de bases (SPEC 009, bloco D1) e a dinâmica por round (M5, a partir de
    ``bout_fighter_rounds``). Isolada para ser o ponto único de injeção nos testes do estágio
    matchup (sem tocar o Postgres).

    A posição de ``add_stance_history_features`` é **load-bearing**: ela lê ``stance``, que só
    entra na frame em ``add_trajectory_features`` (``add_physical_attributes``). Adiantá-la
    para junto das demais features de ``rolling`` produziria ``KeyError`` -- é exatamente por
    isso que ela é função pública própria, e não mais um passo interno de
    ``add_recent_form_features``.
    """
    frames = read_granular(session)
    df = build_long_frame(frames)
    df = add_recent_form_features(df)
    fighters = load_fighters_bio(session.connection())
    df = add_trajectory_features(df, fighters)
    df = add_stance_history_features(df)
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
    _log_bout_level_columns(result)
    logger.info("Preview:\n%s", result.frame.head().to_string())
    return result.frame


def _log_bout_level_columns(result: MatchupMatrix) -> None:
    """Nomeia as features bout-level presentes na matriz, com a cobertura de cada uma.

    Linha de log **dedicada** de propósito: o preview usa ``head().to_string()`` e trunca com
    60+ colunas, então sem ela o valor demonstrável da slice ("as bout-level entram como
    coluna única, sem ``_a``/``_b`` e sem ``_diff`` degenerado") ficaria invisível na execução
    real. A cobertura vem junto porque é o que a RF-09 quer ler antes de qualquer ganho.

    A lista esperada vem da allowlist de ``matchup``, nunca redigitada: é a mesma fonte que
    dá precedência a essas colunas em ``_feature_columns``, e duas listas do mesmo fato
    divergiriam na próxima slice -- com o log jurando cobertura de um conjunto e o payload
    carregando outro.
    """
    esperadas = list(BOUT_LEVEL_FEATURE_COLUMNS)
    presentes = [coluna for coluna in esperadas if coluna in result.feature_columns]
    total = len(result.frame)
    resumo = ", ".join(
        f"{coluna} ({int(result.frame[coluna].notna().sum())}/{total} não-nulas)"
        for coluna in presentes
    )
    logger.info(
        "Features bout-level (coluna única, sem sufixo de canto): %d de %d -- %s",
        len(presentes),
        len(esperadas),
        resumo or "nenhuma",
    )


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


def run_check(session: Session) -> FreshnessReport:
    """Recomputa a pipeline e falha alto se ``bout_features`` estiver defasado.

    Read-only: não escreve nem commita nada. Reporta o diagnóstico via ``logging`` e, quando
    há defasagem, levanta ``StaleFeatureCacheError`` com o comando de correção -- o cache
    derivado nunca mais envelhece em silêncio (o sintoma que motivou a SPEC 007).
    """
    matrix = build_matchup_matrix(_enriched_long_frame(session))
    report = check_cache_freshness(session, matrix)
    logger.info(
        "Freshness: %d linhas no cache, %d recomputadas; %d divergência(s) de bout, "
        "%d coluna(s) só de um lado, %d coluna(s) com não-nulos divergentes.",
        report.cached_bouts,
        report.recomputed_bouts,
        len(report.bout_id_diff),
        len(report.column_diff),
        len(report.column_drift),
    )
    for drift in report.column_drift:
        logger.warning(
            "Defasagem em %s: %d não-nulos no cache, %d no recomputo.",
            drift.column,
            drift.cached_non_null,
            drift.recomputed_non_null,
        )
    if report.is_stale:
        # Nomear as colunas, não só contá-las: sem os nomes, descobrir o que envelheceu exige
        # reproduzir a pipeline à mão. ``column_diff`` já vem ordenado, então a mensagem é
        # determinística. A lógica de frescor (``check_cache_freshness``) não muda aqui.
        detalhe = (
            f" Colunas presentes só de um lado: {', '.join(report.column_diff)}."
            if report.column_diff
            else ""
        )
        raise StaleFeatureCacheError(
            f"bout_features defasado em relação ao granular.{detalhe} "
            "Rode: python -m ingestion.features build --stage materialize"
        )
    return report


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
    """Interpreta ``build --stage <estágio>`` e ``check`` do entrypoint de features."""
    parser = argparse.ArgumentParser(
        description="Feature engineering sobre o granular do UFC (leitura read-only via Pandas).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        _COMMAND_CHECK,
        help="Falha se bout_features estiver defasado em relação ao granular (read-only).",
    )
    build = subparsers.add_parser(
        _COMMAND_BUILD, help="Constrói uma frame de features (em memória)."
    )
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
    """Entrypoint de ``python -m ingestion.features {build --stage <estágio> | check}``.

    Abre a ``Session`` real. O subcomando ``check`` só lê (levanta ``StaleFeatureCacheError``
    quando o cache está defasado, saindo com código diferente de zero). Em ``build``, os
    estágios de leitura (long/rolling/trajectory/matchup) constroem a frame em memória e
    **não commitam**; o estágio ``materialize`` roda a pipeline completa, persiste
    ``bout_features`` e **commita no sucesso** -- a idempotência é observável reexecutando
    (mesma contagem). A frame/resumo é reportada via ``logging``.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    with SessionLocal() as session:
        if args.command == _COMMAND_CHECK:
            run_check(session)
        elif args.stage == _STAGE_MATERIALIZE:
            run_materialize(session)
            session.commit()
        else:
            run_build(session, args.stage)


if __name__ == "__main__":
    main()
