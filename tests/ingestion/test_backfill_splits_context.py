"""Testes do entrypoint fino do backfill M5 (``ingestion.backfill_splits_context``).

O backfill em si (``backfill_splits_and_context``) é coberto em
``tests/ingestion/test_seed_bouts_backfill.py`` contra o Postgres real. Aqui só se cobre a
camada de CLI que foi extraída de ``ingestion.seed_bouts`` para este entrypoint dedicado:
o parser de argumentos (``--dataset-dir``), que é a única lógica testável barata do módulo --
``main`` abre uma sessão real e commita, fora do escopo de teste transacional (paridade com
``ingestion.seed.main``, também não testado diretamente).
"""

from __future__ import annotations

from pathlib import Path

from ingestion.backfill_splits_context import _parse_backfill_args


def test_parse_backfill_args_le_dataset_dir() -> None:
    """``--dataset-dir`` é parseado como ``Path``."""
    args = _parse_backfill_args(["--dataset-dir", "/data/ufc"])
    assert args.dataset_dir == Path("/data/ufc")


def test_parse_backfill_args_dataset_dir_default_none() -> None:
    """Sem ``--dataset-dir`` o valor é ``None`` (sinaliza aquisição via kagglehub)."""
    args = _parse_backfill_args([])
    assert args.dataset_dir is None
