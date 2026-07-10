"""Model do evento do UFC.

Chave natural: ``(name, date)`` -- garantida por unique constraint, é a chave de
dedup do seed de eventos (slice 03).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mma_analytics.db import Base


class Event(Base):
    """Evento do UFC (uma linha por evento único)."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("name", "date", name="uq_event_name_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    date: Mapped[date]  # data de calendário do evento
    location: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(32))
