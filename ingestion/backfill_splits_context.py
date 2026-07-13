"""Backfill M5 (Slice 02) dos splits totais + contexto do CSV (UPDATE idempotente).

Entrypoint fino e dedicado do backfill: ``python -m ingestion.backfill_splits_context``. A lógica
(``backfill_splits_and_context``) mora em ``ingestion.seed_bouts``, junto do seed, porque reusa
pesadamente os internos dele (resolução de FKs, chave natural, índices em memória); aqui vive
apenas o CLI. Faz UPDATE idempotente dos splits totais (``bout_fighters``) e do contexto da luta
(``bouts``) a partir do ``fight_details.csv`` do seed (``source="kaggle"``, 0 quota Cito).

**Pressupõe o banco já semeado (M0)**: faz UPDATE nas linhas existentes, nunca INSERT. Lutas ainda
não semeadas são puladas (contadas em ``skipped``), nunca criadas. Rodar de novo mantém contagem e
conteúdo (o UPDATE por atributo é naturalmente idempotente).
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from ingestion.seed import resolve_dataset_dir
from ingestion.seed_bouts import backfill_splits_and_context
from ingestion.sources.kaggle import (
    EVENT_DETAILS_FILE,
    FIGHT_DETAILS_FILE,
    load_event_details,
    load_fight_details,
)
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)


def _parse_backfill_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do backfill."""
    parser = argparse.ArgumentParser(
        description=(
            "Backfill dos splits totais + contexto da luta a partir do CSV do seed "
            "(UPDATE idempotente, source=kaggle, 0 quota Cito)."
        ),
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help=(
            "Diretório local com fight_details.csv e event_details.csv. Omitido: baixa o "
            "dataset via kagglehub. Alternativa: variável SEED_DATASET_DIR."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint de ``python -m ingestion.backfill_splits_context``: backfill e **commit**.

    A lógica testável (``backfill_splits_and_context``) é isolada em ``ingestion.seed_bouts``;
    ``main`` é fino e escapa à transação de teste (por isso commita). Pressupõe o banco já
    semeado (M0): faz UPDATE nas linhas existentes, nunca INSERT. Reusa ``resolve_dataset_dir``
    do seed (CLI vence ``SEED_DATASET_DIR``, que vence a aquisição via kagglehub). Reporta via
    ``logging`` (``print`` é proibido).
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_backfill_args(argv)
    dataset_dir = resolve_dataset_dir(args.dataset_dir, os.environ)
    fight_path = dataset_dir / FIGHT_DETAILS_FILE if dataset_dir is not None else None
    event_path = dataset_dir / EVENT_DETAILS_FILE if dataset_dir is not None else None

    fight_details = load_fight_details(fight_path)
    event_details = load_event_details(event_path)
    with SessionLocal() as session:
        result = backfill_splits_and_context(session, fight_details, event_details)
        session.commit()

    logger.info("Backfill de splits + contexto concluído: %s", result)


if __name__ == "__main__":
    main()
