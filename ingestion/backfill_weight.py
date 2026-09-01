"""Backfill M6 (Slice 02) de ``fighters.weight_kg`` a partir do CSV do seed (UPDATE idempotente).

Entrypoint fino e dedicado: ``python -m ingestion.backfill_weight``. A lógica
(``backfill_fighter_weight``) mora em ``ingestion.seed_fighters``, junto do seed, porque reusa os
internos dele (projeção tipada da borda, entity resolution e a chave natural
``(name_normalized, date_of_birth)``); aqui vive apenas o CLI. Mesmo padrão de
``ingestion.backfill_splits_context``.

Fecha a ponta solta do M5, que criou a coluna, o schema Pydantic e os testes de API de
``weight_kg`` sem nunca gravar o dado -- a coluna ``weight`` do CSV nunca havia entrado na
projeção do seed. Custo zero de quota: opera só sobre o ``fighter_details.csv``
(``source="kaggle"``, 0 chamadas à Cito).

**Pressupõe o banco já semeado (M0)**: faz UPDATE nas linhas existentes, nunca INSERT. Lutador do
CSV ainda não semeado é pulado (contado em ``skipped``), nunca criado. Peso ausente no CSV
permanece nulo -- nunca zero, nunca sentinela. Rodar de novo devolve ``updated=0``.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from ingestion.seed import resolve_dataset_dir
from ingestion.seed_fighters import backfill_fighter_weight
from ingestion.sources.kaggle import FIGHTER_DETAILS_FILE, load_fighter_details
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)


def _parse_backfill_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do backfill de peso."""
    parser = argparse.ArgumentParser(
        description=(
            "Backfill de fighters.weight_kg a partir do CSV do seed "
            "(UPDATE idempotente, source=kaggle, 0 quota Cito)."
        ),
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help=(
            "Diretório local com fighter_details.csv. Omitido: baixa o dataset via "
            "kagglehub. Alternativa: variável SEED_DATASET_DIR."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint de ``python -m ingestion.backfill_weight``: backfill e **commit**.

    A lógica testável (``backfill_fighter_weight``) é isolada em ``ingestion.seed_fighters``;
    ``main`` é fino e escapa à transação de teste (por isso commita). Reusa
    ``resolve_dataset_dir`` do seed (CLI vence ``SEED_DATASET_DIR``, que vence a aquisição via
    kagglehub). Reporta via ``logging`` (``print`` é proibido).
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_backfill_args(argv)
    dataset_dir = resolve_dataset_dir(args.dataset_dir, os.environ)
    fighter_path = dataset_dir / FIGHTER_DETAILS_FILE if dataset_dir is not None else None

    fighter_details = load_fighter_details(fighter_path)
    with SessionLocal() as session:
        result = backfill_fighter_weight(session, fighter_details)
        session.commit()

    logger.info("Backfill de weight_kg concluído: %s", result)


if __name__ == "__main__":
    main()
