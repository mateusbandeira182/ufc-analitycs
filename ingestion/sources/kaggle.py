"""Aquisição do dataset-seed do Kaggle (ADR 0002: ``neelagiriaditya/ufc-datasets-1994-2025``).

Suporta dois caminhos: um caminho local de CSV (determinístico, sem credencial --
usado no CI e nos testes) e o download programático via ``kagglehub`` quando o
caminho local é omitido. O CSV é lido inteiro como texto (``dtype=str``, sem
conversão de vazios em ``NaN``) para que a tipagem aconteça na borda da entity
resolution, e não no Pandas.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

DATASET = "neelagiriaditya/ufc-datasets-1994-2025"
FIGHTER_DETAILS_FILE = "fighter_details.csv"


def load_fighter_details(csv_path: Path | None = None) -> pd.DataFrame:
    """Retorna o ``fighter_details.csv`` como ``DataFrame`` de strings.

    Usa ``csv_path`` local quando fornecido (determinístico, para CI/teste); quando
    omitido, baixa o dataset via ``kagglehub`` e lê o arquivo baixado. Vazios são
    preservados como string vazia (``keep_default_na=False``) -- a conversão de tipos
    e o tratamento de ausentes ficam na entity resolution.
    """
    resolved_path = csv_path if csv_path is not None else _download_fighter_details()
    logger.info("Lendo fighter_details de %s", resolved_path)
    return pd.read_csv(resolved_path, dtype=str, keep_default_na=False)


def _download_fighter_details() -> Path:
    """Baixa o dataset via ``kagglehub`` e devolve o caminho do ``fighter_details.csv``."""
    import kagglehub

    dataset_dir = Path(kagglehub.dataset_download(DATASET))
    logger.info("Dataset %s baixado em %s", DATASET, dataset_dir)
    return dataset_dir / FIGHTER_DETAILS_FILE
