"""Model do lutador: identidade, atributos lentos e cartel snapshot.

Chave de dedup do seed: ``(name_normalized, date_of_birth)`` -- a entity
resolution (slice 02) usa o nome normalizado como chave e a data de nascimento
como desempate. Por isso ``name_normalized`` é indexado.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from apps.fighters.enums import Stance
from mma_analytics.db import Base


class Fighter(Base):
    """Lutador do UFC (uma linha por identidade deduplicada)."""

    __tablename__ = "fighters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    # Chave de dedup (slice 02): nome normalizado (caixa/acentos/espaços/sufixos).
    name_normalized: Mapped[str] = mapped_column(String(255), index=True)
    nickname: Mapped[str | None] = mapped_column(String(255))
    date_of_birth: Mapped[date | None]  # desempate da dedup
    height_cm: Mapped[int | None]
    reach_cm: Mapped[int | None]
    stance: Mapped[Stance | None] = mapped_column(
        Enum(Stance, name="stance", values_callable=lambda enum: [m.value for m in enum])
    )
    # Atributo físico estático opcional (M5 -- ADR 0004): peso em quilogramas.
    weight_kg: Mapped[float | None]
    # Identificadores do lutador na API oficial da UFC (M7 -- SPEC 008, RF-07). Guardados como
    # texto opaco porque identificador externo nunca entra em aritmética, no mesmo padrão de
    # ``events.ufc_event_id``.
    #
    # Nullable, e a ausência é o **estado normal**, nunca falha: lutador ativo apenas antes de
    # 2010-03-21 está fora da janela da fonte por decisão (RF-03) e nunca receberá id, e a Cito
    # não publica este identificador. Por isso o id **complementa** a chave de dedup
    # ``(name_normalized, date_of_birth)`` com precedência, em vez de substituí-la.
    #
    # Só ``ufc_fighter_id`` é indexado: é por ele que a entity resolution resolve. O
    # ``ufc_mma_id`` acompanha o dado (identificador do lutador no cadastro de MMA da fonte) e
    # não resolve nada -- índice sem consulta é custo de escrita sem contrapartida.
    ufc_fighter_id: Mapped[str | None] = mapped_column(String(32), index=True)
    ufc_mma_id: Mapped[str | None] = mapped_column(String(32))
    # Cartel snapshot do dataset (recálculo a partir de bouts é do M2).
    wins: Mapped[int]
    losses: Mapped[int]
    draws: Mapped[int]
    source: Mapped[str] = mapped_column(String(32))
