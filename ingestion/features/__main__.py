"""Habilita ``python -m ingestion.features`` delegando para o CLI de features."""

from __future__ import annotations

from ingestion.features.cli import main

if __name__ == "__main__":
    main()
