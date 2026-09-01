"""Carga idempotente de ``fighters`` a partir do ``fighter_details.csv`` (seed Kaggle).

Fluxo: aquisição (``sources.kaggle``) -> tipagem da borda do Pandas em ``FighterRow``
-> entity resolution (dedup por ``(name_normalized, date_of_birth)``) -> get-or-create
no nível da aplicação. Grava ``source="kaggle"`` em toda escrita.

A idempotência é garantida por get-or-create na chave natural, e **não** por
``INSERT ... ON CONFLICT``: ``date_of_birth`` é nullable e o Postgres trata cada
``NULL`` como distinto em índice único, o que reinseriria lutadores sem DOB. Carregar
o conjunto de chaves já presentes e inserir apenas as ausentes cobre corretamente o
caso ``NULL`` e mantém a contagem estável na reexecução.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.fighters.models import Fighter
from ingestion.entity_resolution import FighterRow, ResolvedFighter, resolve_fighters
from ingestion.sources.kaggle import load_fighter_details

logger = logging.getLogger(__name__)

SOURCE = "kaggle"

# Colunas do ``fighter_details.csv`` que o domínio consome (ver ADR 0002).
_COLUMNS = (
    "name",
    "nick_name",
    "dob",
    "height",
    "weight",
    "reach",
    "stance",
    "wins",
    "losses",
    "draws",
)


def seed_fighters(session: Session, csv_path: Path | None = None) -> int:
    """Carrega ``fighters`` idempotentemente e retorna quantos foram inseridos nesta execução.

    Deduplica por ``(name_normalized, date_of_birth)`` (``source="kaggle"``). A segunda
    execução não insere nada nem altera a contagem.
    """
    frame = load_fighter_details(csv_path)
    resolved = resolve_fighters(_rows_from_frame(frame))

    existing_keys: set[tuple[str, object]] = {
        (name_normalized, dob)
        for name_normalized, dob in session.execute(
            select(Fighter.name_normalized, Fighter.date_of_birth)
        )
    }

    inserted = 0
    for fighter in resolved:
        key = (fighter.name_normalized, fighter.date_of_birth)
        if key in existing_keys:
            continue
        session.add(_to_model(fighter))
        existing_keys.add(key)
        inserted += 1

    session.flush()
    logger.info("Seed de fighters: %d inseridos (%d resolvidos)", inserted, len(resolved))
    return inserted


def _rows_from_frame(frame: pd.DataFrame) -> list[FighterRow]:
    """Tipa a borda dinâmica do Pandas: cada linha do ``DataFrame`` vira um ``FighterRow``.

    Nenhum ``Any`` do ``DataFrame`` propaga para o domínio -- cada célula é forçada a texto.
    """
    return [
        FighterRow(
            name=str(record["name"]),
            nick_name=str(record["nick_name"]),
            dob=str(record["dob"]),
            height=str(record["height"]),
            weight=str(record["weight"]),
            reach=str(record["reach"]),
            stance=str(record["stance"]),
            wins=str(record["wins"]),
            losses=str(record["losses"]),
            draws=str(record["draws"]),
        )
        for record in frame[list(_COLUMNS)].to_dict(orient="records")
    ]


@dataclass(frozen=True)
class WeightBackfillResult:
    """Resumo observável do backfill de peso (idempotência, cobertura e taxa de skip)."""

    updated: int  # linhas de ``fighters`` que receberam ``weight_kg``
    without_weight: int  # linhas do CSV sem peso -- permanecem nulas, nunca zero
    skipped: int  # linhas do CSV sem fighter persistido correspondente (nunca INSERT)


def backfill_fighter_weight(
    session: Session, fighter_details: pd.DataFrame
) -> WeightBackfillResult:
    """Preenche ``fighters.weight_kg`` a partir do CSV do seed (UPDATE idempotente, 0 quota Cito).

    Pressupõe o banco já semeado: atualiza apenas linhas **existentes** com ``weight_kg`` nulo e
    ``source="kaggle"``, casadas pela chave natural ``(name_normalized, date_of_birth)``. O filtro
    por ``weight_kg`` nulo torna a idempotência estrutural (a segunda execução não encontra nada a
    fazer e devolve ``updated=0``) e impede que o peso do Kaggle sobrescreva valor gravado por
    outra fonte. Nunca insere: linha do CSV sem fighter correspondente é contada em ``skipped``.
    """
    persisted: dict[tuple[str, object], Fighter] = {
        (fighter.name_normalized, fighter.date_of_birth): fighter
        for fighter in session.scalars(select(Fighter).where(Fighter.source == SOURCE))
    }

    updated = 0
    without_weight = 0
    skipped = 0

    for resolved in resolve_fighters(_rows_from_frame(fighter_details)):
        if resolved.weight_kg is None:
            without_weight += 1
            continue

        fighter = persisted.get((resolved.name_normalized, resolved.date_of_birth))
        if fighter is None:
            skipped += 1
            logger.warning(
                "Backfill: lutador %r do CSV não está persistido; pulado (não cria linha)",
                resolved.name,
            )
            continue

        # Só preenche o que está nulo: a idempotência é estrutural e o peso do Kaggle
        # nunca sobrescreve valor já gravado (possivelmente por outra fonte).
        if fighter.weight_kg is not None:
            continue

        fighter.weight_kg = resolved.weight_kg
        updated += 1

    session.flush()
    logger.info(
        "Backfill de weight_kg: %d atualizados, %d sem peso no CSV, %d pulados",
        updated,
        without_weight,
        skipped,
    )
    return WeightBackfillResult(updated=updated, without_weight=without_weight, skipped=skipped)


def _to_model(fighter: ResolvedFighter) -> Fighter:
    """Materializa um ``ResolvedFighter`` em um model ``Fighter`` com ``source="kaggle"``."""
    return Fighter(
        name=fighter.name,
        name_normalized=fighter.name_normalized,
        nickname=fighter.nickname,
        date_of_birth=fighter.date_of_birth,
        height_cm=fighter.height_cm,
        weight_kg=fighter.weight_kg,
        reach_cm=fighter.reach_cm,
        stance=fighter.stance,
        wins=fighter.wins,
        losses=fighter.losses,
        draws=fighter.draws,
        source=SOURCE,
    )
