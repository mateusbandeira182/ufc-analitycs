"""Testes da carga idempotente de events -- CA-01 a CA-04.

Cobrem a extração/dedup dos eventos únicos combinando ``event_details.csv`` (data,
local) e ``fight_details.csv`` (nome real do evento) por ``event_id`` (ADR 0002), o
parsing da data (``"September 06, 2025"`` -> ``date``), o mapeamento de local ausente
para ``None``, a gravação de ``source="kaggle"`` e a idempotência (rodar a carga duas
vezes mantém a contagem). Os testes de carga rodam contra o Postgres de teste
``ufc_bum_test`` na sessão transacional (rollback ao final).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.events.models import Event
from ingestion.seed_events import EventRecord, extract_events, load_events, seed_events
from ingestion.sources.kaggle import load_event_details, load_fight_details

_FIXTURES = Path(__file__).parent / "fixtures"
_EVENT_DETAILS = _FIXTURES / "event_details_sample.csv"
_FIGHT_DETAILS = _FIXTURES / "fight_details_sample.csv"

# A fixture tem 4 lutas em 2 eventos; o evento "UFC 319" agrupa 3 lutas -> 2 eventos únicos.
_UNIQUE_EVENTS = 2


def _extract_from_fixture() -> list[EventRecord]:
    return extract_events(load_event_details(_EVENT_DETAILS), load_fight_details(_FIGHT_DETAILS))


def test_extract_events_deduplica_por_event_id() -> None:
    """CA-01: várias lutas do mesmo evento colapsam em um único ``EventRecord``."""
    records = _extract_from_fixture()
    assert len(records) == _UNIQUE_EVENTS

    names = {record.name for record in records}
    assert names == {
        "UFC 319: Du Plessis vs. Chimaev",
        "UFC Fight Night: Imavov vs. Borralho",
    }


def test_extract_events_faz_parsing_de_data() -> None:
    """CA-01: a data ``"August 16, 2025"`` vira uma ``date`` de calendário."""
    by_name = {record.name: record for record in _extract_from_fixture()}
    assert by_name["UFC 319: Du Plessis vs. Chimaev"].date == date(2025, 8, 16)
    assert by_name["UFC Fight Night: Imavov vs. Borralho"].date == date(2025, 9, 6)


def test_extract_events_mapeia_location_ausente_para_none() -> None:
    """CA-04: local preenchido é preservado; local ausente vira ``None``."""
    by_name = {record.name: record for record in _extract_from_fixture()}
    assert by_name["UFC 319: Du Plessis vs. Chimaev"].location == "Chicago, Illinois, USA"
    assert by_name["UFC Fight Night: Imavov vs. Borralho"].location is None


def test_carga_popula_events_com_source(db_session: Session) -> None:
    """CA-02: a carga insere um evento por chave natural gravando ``source="kaggle"``."""
    inserted = load_events(db_session, _extract_from_fixture())
    assert inserted == _UNIQUE_EVENTS

    total = db_session.scalar(select(func.count()).select_from(Event))
    assert total == _UNIQUE_EVENTS

    sources = set(db_session.scalars(select(Event.source)).all())
    assert sources == {"kaggle"}

    (imavov,) = db_session.scalars(
        select(Event).where(Event.name == "UFC Fight Night: Imavov vs. Borralho")
    ).all()
    assert imavov.date == date(2025, 9, 6)
    assert imavov.location is None


def test_carga_e_idempotente(db_session: Session) -> None:
    """CA-03: a segunda execução não insere nada nem altera a contagem."""
    first = seed_events(db_session, _EVENT_DETAILS, _FIGHT_DETAILS)
    total_after_first = db_session.scalar(select(func.count()).select_from(Event))

    second = seed_events(db_session, _EVENT_DETAILS, _FIGHT_DETAILS)
    total_after_second = db_session.scalar(select(func.count()).select_from(Event))

    assert first == _UNIQUE_EVENTS
    assert second == 0
    assert total_after_first == total_after_second == _UNIQUE_EVENTS


def test_seed_events_ponta_a_ponta(db_session: Session) -> None:
    """CA-01/CA-03: ``seed_events`` compõe extração + carga e popula sem duplicar."""
    inserted = seed_events(db_session, _EVENT_DETAILS, _FIGHT_DETAILS)
    assert inserted == _UNIQUE_EVENTS

    names = set(db_session.scalars(select(Event.name)).all())
    assert names == {
        "UFC 319: Du Plessis vs. Chimaev",
        "UFC Fight Night: Imavov vs. Borralho",
    }
