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
EVENT_DETAILS_FILE = "event_details.csv"
FIGHT_DETAILS_FILE = "fight_details.csv"


def load_fighter_details(csv_path: Path | None = None) -> pd.DataFrame:
    """Retorna o ``fighter_details.csv`` como ``DataFrame`` de strings (seed de fighters)."""
    return _read_dataset_csv(FIGHTER_DETAILS_FILE, csv_path)


def load_event_details(csv_path: Path | None = None) -> pd.DataFrame:
    """Retorna o ``event_details.csv`` (``event_id``, ``date``, ``location``) como strings."""
    return _read_dataset_csv(EVENT_DETAILS_FILE, csv_path)


def load_fight_details(csv_path: Path | None = None) -> pd.DataFrame:
    """Retorna o ``fight_details.csv`` (traz ``event_name`` real por ``event_id``) como strings."""
    return _read_dataset_csv(FIGHT_DETAILS_FILE, csv_path)


def _read_dataset_csv(filename: str, csv_path: Path | None) -> pd.DataFrame:
    """Lê um CSV do dataset como ``DataFrame`` de strings.

    Usa ``csv_path`` local quando fornecido (determinístico, para CI/teste); quando
    omitido, baixa o dataset via ``kagglehub`` e lê o arquivo baixado. Vazios são
    preservados como string vazia (``keep_default_na=False``) -- a conversão de tipos
    e o tratamento de ausentes ficam na borda tipada de cada seed.
    """
    resolved_path = csv_path if csv_path is not None else _download_dataset_file(filename)
    logger.info("Lendo %s de %s", filename, resolved_path)
    return pd.read_csv(resolved_path, dtype=str, keep_default_na=False)


def _download_dataset_file(filename: str) -> Path:
    """Baixa o dataset via ``kagglehub`` e devolve o caminho de ``filename`` nele."""
    import kagglehub

    dataset_dir = Path(kagglehub.dataset_download(DATASET))
    logger.info("Dataset %s baixado em %s", DATASET, dataset_dir)
    return dataset_dir / filename
