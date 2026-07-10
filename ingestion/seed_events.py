"""Carga idempotente de ``events`` a partir do dataset-seed do Kaggle (ADR 0002).

O nome real do evento vive em ``fight_details.csv`` (``event_name``), enquanto a data
e o local vivem em ``event_details.csv``; ambos compartilham ``event_id``. O seed
combina os dois CSVs por ``event_id``, colapsa as várias lutas de um mesmo evento em
um único registro e carrega ``events`` gravando ``source="kaggle"`` em toda escrita.

A chave natural de ``events`` é ``(name, date)`` (schema da Slice 01, ``name`` e ``date``
NOT NULL). A idempotência é garantida por get-or-create nessa chave: a reexecução não
insere nada nem altera a contagem. A borda dinâmica do Pandas é tipada em ``EventRow``
antes de virar ``EventRecord`` -- nenhum ``Any`` do ``DataFrame`` entra no domínio.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TypedDict

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.events.models import Event
from ingestion.sources.kaggle import load_event_details, load_fight_details

logger = logging.getLogger(__name__)

SOURCE = "kaggle"

# Formato da data em ``event_details.csv`` (ex.: ``"September 06, 2025"``).
_DATE_FORMAT = "%B %d, %Y"

# Colunas reais dos CSVs (ADR 0002).
_EVENT_ID_COL = "event_id"
_DATE_COL = "date"
_LOCATION_COL = "location"
_EVENT_NAME_COL = "event_name"


class EventRow(TypedDict):
    """Linha crua combinada dos CSVs de evento (todos os campos como texto)."""

    event_id: str
    event_name: str
    date: str
    location: str


@dataclass(frozen=True)
class EventRecord:
    """Evento único tipado, pronto para a carga em ``events``."""

    name: str
    date: date
    location: str | None


def seed_events(
    session: Session,
    event_details_path: Path | None = None,
    fight_details_path: Path | None = None,
) -> int:
    """Carrega ``events`` idempotentemente e retorna quantos foram inseridos nesta execução.

    Combina ``event_details.csv`` e ``fight_details.csv`` por ``event_id``, deduplica por
    evento e faz get-or-create na chave natural ``(name, date)`` com ``source="kaggle"``.
    A segunda execução não insere nada nem altera a contagem.
    """
    event_details = load_event_details(event_details_path)
    fight_details = load_fight_details(fight_details_path)
    return load_events(session, extract_events(event_details, fight_details))


def extract_events(event_details: pd.DataFrame, fight_details: pd.DataFrame) -> list[EventRecord]:
    """Extrai um ``EventRecord`` por ``event_id``, combinando nome, data e local.

    As várias lutas de um mesmo evento colapsam em um único registro (first-seen vence).
    A data vira ``date`` de calendário; local ausente (string vazia) vira ``None``.
    """
    name_by_event_id = _event_names_by_id(fight_details)
    records: list[EventRecord] = []
    seen: set[str] = set()
    for row in _event_rows(event_details, name_by_event_id):
        event_id = row["event_id"]
        if event_id in seen:
            continue
        seen.add(event_id)
        name = row["event_name"]
        if not name:
            logger.warning("Evento %s sem nome em fight_details; ignorado", event_id)
            continue
        records.append(
            EventRecord(
                name=name,
                date=_parse_event_date(row["date"]),
                location=_clean_location(row["location"]),
            )
        )
    return records


def load_events(session: Session, records: Iterable[EventRecord]) -> int:
    """Insere os eventos em ``events`` com ``source="kaggle"`` e retorna a contagem inserida.

    Get-or-create na chave natural ``(name, date)``: só insere os eventos ainda ausentes,
    mantendo a contagem estável na reexecução (idempotência).
    """
    existing_keys: set[tuple[str, date]] = {
        (name, event_date) for name, event_date in session.execute(select(Event.name, Event.date))
    }

    inserted = 0
    for record in records:
        key = (record.name, record.date)
        if key in existing_keys:
            continue
        session.add(
            Event(
                name=record.name,
                date=record.date,
                location=record.location,
                source=SOURCE,
            )
        )
        existing_keys.add(key)
        inserted += 1

    session.flush()
    logger.info("Seed de events: %d inseridos", inserted)
    return inserted


def _event_names_by_id(fight_details: pd.DataFrame) -> dict[str, str]:
    """Mapeia ``event_id`` -> ``event_name`` real (uma entrada por evento)."""
    columns = fight_details[[_EVENT_ID_COL, _EVENT_NAME_COL]].to_dict(orient="records")
    return {str(record[_EVENT_ID_COL]): str(record[_EVENT_NAME_COL]) for record in columns}


def _event_rows(
    event_details: pd.DataFrame, name_by_event_id: dict[str, str]
) -> Iterable[EventRow]:
    """Tipa a borda dinâmica do Pandas: cada linha de ``event_details`` vira um ``EventRow``."""
    for record in event_details[[_EVENT_ID_COL, _DATE_COL, _LOCATION_COL]].to_dict(
        orient="records"
    ):
        event_id = str(record[_EVENT_ID_COL])
        yield EventRow(
            event_id=event_id,
            event_name=name_by_event_id.get(event_id, ""),
            date=str(record[_DATE_COL]),
            location=str(record[_LOCATION_COL]),
        )


def _parse_event_date(value: str) -> date:
    """Data do evento no formato ``"September 06, 2025"`` -> ``date`` de calendário.

    É uma data de calendário (sem instante/timezone); por isso ``strptime`` sem ``%z``.
    """
    return datetime.strptime(value.strip(), _DATE_FORMAT).date()  # noqa: DTZ007  # data de calendário, sem timezone


def _clean_location(value: str) -> str | None:
    """Local ausente (string vazia) vira ``None``; caso contrário, texto sem espaços nas bordas."""
    stripped = value.strip()
    return stripped or None
