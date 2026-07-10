"""Entrypoint da carga única orquestrada do histórico do UFC (seed Kaggle, ADR 0002).

Amarra as cargas por entidade já entregues pelas slices anteriores -- ``seed_fighters``
(Slice 02), ``seed_events`` (Slice 03) e ``seed_bouts`` (Slice 04) -- na ordem de
dependência **fighters -> events -> bouts**: ``bouts`` só resolve as FKs contra os
fighters e events já materializados. Cada carga é idempotente por chave natural; por
composição, o seed **inteiro** é idempotente: rodar de novo com os mesmos dados não
altera nenhuma das quatro contagens nem duplica.

A origem do dataset é configurável sem editar código: um diretório local com os três
CSVs (``fighter_details.csv``, ``event_details.csv``, ``fight_details.csv``) para o CI
e os testes determinísticos, ou a aquisição programática via ``kagglehub`` quando o
diretório é omitido. ``run_seed`` opera sobre a ``Session`` recebida (transacional nos
testes); o ``main`` abre a sessão real, chama ``run_seed`` e **commita**.

Nota de divergência do plano (registrada no relatório): a ADR 0002 troca a fonte por um
dataset de **três** CSVs, então a origem é um **diretório** (``resolve_dataset_dir`` /
``--dataset-dir``), não um único ``csv_path``. As cargas por entidade re-consultam o
banco por chave natural (não recebem/retornam índice), então a orquestração apenas as
encadeia na ordem correta -- a idempotência por chave natural preserva os CAs.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.seed_bouts import seed_bouts
from ingestion.seed_events import seed_events
from ingestion.seed_fighters import seed_fighters
from ingestion.sources.kaggle import (
    EVENT_DETAILS_FILE,
    FIGHT_DETAILS_FILE,
    FIGHTER_DETAILS_FILE,
)
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

# Variável de ambiente que aponta para o diretório local do dataset (origem sem editar código).
_ENV_DATASET_DIR = "SEED_DATASET_DIR"


@dataclass(frozen=True)
class SeedCounts:
    """Contagem total de cada tabela após a carga (base da verificação de idempotência)."""

    fighters: int
    events: int
    bouts: int
    bout_fighters: int


@dataclass(frozen=True)
class _DatasetPaths:
    """Caminhos dos três CSVs do dataset; ``None`` dispara a aquisição via ``kagglehub``."""

    fighter_details: Path | None
    event_details: Path | None
    fight_details: Path | None


def resolve_dataset_dir(cli_path: Path | None, env: Mapping[str, str]) -> Path | None:
    """Resolve o diretório do dataset: CLI vence a variável de ambiente, que vence ``None``.

    ``None`` (sem CLI nem ``SEED_DATASET_DIR``) sinaliza que a aquisição do dataset deve
    ser programática (``kagglehub``). Trocar a origem não exige edição de código.
    """
    if cli_path is not None:
        return cli_path
    raw = env.get(_ENV_DATASET_DIR)
    return Path(raw) if raw else None


def _dataset_paths(dataset_dir: Path | None) -> _DatasetPaths:
    """Deriva os caminhos dos três CSVs a partir do diretório; ``None`` -> tudo ``None``."""
    if dataset_dir is None:
        return _DatasetPaths(fighter_details=None, event_details=None, fight_details=None)
    return _DatasetPaths(
        fighter_details=dataset_dir / FIGHTER_DETAILS_FILE,
        event_details=dataset_dir / EVENT_DETAILS_FILE,
        fight_details=dataset_dir / FIGHT_DETAILS_FILE,
    )


def run_seed(session: Session, dataset_dir: Path | None = None) -> SeedCounts:
    """Executa a carga completa na ordem de dependência e devolve as contagens das quatro tabelas.

    Ordem: ``fighters`` -> ``events`` -> ``bouts``/``bout_fighters`` (esta última resolve
    as FKs contra as anteriores já materializadas). Cada etapa é idempotente por chave
    natural e grava ``source="kaggle"``; por composição, a reexecução com os mesmos dados
    não altera nenhuma contagem nem duplica. Opera sobre a ``Session`` recebida (o commit
    é responsabilidade do chamador -- ``main`` em produção, o rollback da fixture no teste).
    """
    paths = _dataset_paths(dataset_dir)
    fighters_inserted = seed_fighters(session, paths.fighter_details)
    events_inserted = seed_events(session, paths.event_details, paths.fight_details)
    bouts_result = seed_bouts(session, paths.fight_details, paths.event_details)

    logger.info(
        "Seed orquestrado: %d fighters, %d events, %d bouts e %d bout_fighters inseridos "
        "(%d lutas puladas)",
        fighters_inserted,
        events_inserted,
        bouts_result.bouts_inserted,
        bouts_result.bout_fighters_inserted,
        bouts_result.skipped,
    )
    return _count_tables(session)


def _count_tables(session: Session) -> SeedCounts:
    """Conta as linhas das quatro tabelas na ``Session`` atual."""
    return SeedCounts(
        fighters=session.scalar(select(func.count()).select_from(Fighter)) or 0,
        events=session.scalar(select(func.count()).select_from(Event)) or 0,
        bouts=session.scalar(select(func.count()).select_from(Bout)) or 0,
        bout_fighters=session.scalar(select(func.count()).select_from(BoutFighter)) or 0,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de linha de comando do entrypoint."""
    parser = argparse.ArgumentParser(
        description="Carga única do histórico do UFC (dataset Kaggle) no Postgres.",
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help=(
            "Diretório local com fighter_details.csv, event_details.csv e fight_details.csv. "
            "Omitido: baixa o dataset via kagglehub. Alternativa: variável SEED_DATASET_DIR."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint de ``python -m ingestion.seed``: abre a sessão real, semeia e **commita**.

    A lógica testável (``resolve_dataset_dir``, ``run_seed``) é isolada; ``main`` é fino e
    verificado pela dupla execução manual do DoD da sprint (o commit escapa a transação de
    teste). Reporta as contagens finais via ``logging`` (``print`` é proibido).
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    dataset_dir = resolve_dataset_dir(args.dataset_dir, os.environ)

    with SessionLocal() as session:
        counts = run_seed(session, dataset_dir)
        session.commit()

    logger.info("Seed concluído: %s", counts)


if __name__ == "__main__":
    main()
