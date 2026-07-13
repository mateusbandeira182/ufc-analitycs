"""Models da luta (``bouts``) e da representação long por canto (``bout_fighters``).

Decisão load-bearing (ADR 0001): as estatísticas vivem em ``bout_fighters``,
**uma linha por lutador-por-luta** (long/normalizado), não em colunas wide
espelhando o CSV. Isso torna trivial o histórico do lutador ao longo do tempo
(base do preditivo, fase 2) e preserva a granularidade por luta -- nenhuma média
é pré-agregada de forma destrutiva.

Decisões de schema desta slice:
- Decisão #3: a duração de encerramento é ``ending_time_seconds`` (inteiro em
  segundos), não string ``mm:ss``; formatação é preocupação de apresentação (M2).
- Decisão #4: ``winner_id`` é nullable -- empate/no contest têm vencedor nulo.
"""

from __future__ import annotations

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.bouts.enums import BoutMethod, Corner
from apps.fighters.models import Fighter
from mma_analytics.db import Base


class Bout(Base):
    """Uma luta: liga o evento aos dois lutadores, com resultado e método.

    Chave natural (ingestão, slice 04): ``(event_id, par não-ordenado de
    fighter_ids)`` -- a implementação do desempate mora no seed, não no schema.
    """

    __tablename__ = "bouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"))
    winner_id: Mapped[int | None] = mapped_column(
        ForeignKey("fighters.id")
    )  # nulo em empate / no contest
    method: Mapped[BoutMethod] = mapped_column(
        Enum(BoutMethod, name="bout_method", values_callable=lambda enum: [m.value for m in enum])
    )
    round: Mapped[int | None]
    ending_time_seconds: Mapped[int | None]  # Decisão #3: segundos, não mm:ss
    weight_class: Mapped[str | None] = mapped_column(String(64))
    # Contexto de luta (M5 -- ADR 0004). Aditivo/nullable: preenchido pelo backfill
    # do CSV do seed (Slice 02), com origem ``kaggle``.
    title_bout: Mapped[bool | None]
    scheduled_rounds: Mapped[int | None]
    referee: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(32))

    # Relationship ORM (só leitura, sem migration): os cantos desta luta, ordenados
    # por ``corner`` para um card estável. Habilita o eager-load dos participantes
    # (identidade do lutador) no card do evento sem N+1.
    bout_fighters: Mapped[list[BoutFighter]] = relationship(order_by="BoutFighter.corner")


class BoutFighter(Base):
    """Uma linha por lutador-por-luta com as stats granulares daquele canto.

    Unicidade ``(bout_id, fighter_id)``: um lutador aparece uma única vez por
    luta. ``fighter_id`` é indexado para a série temporal por lutador.
    """

    __tablename__ = "bout_fighters"
    __table_args__ = (UniqueConstraint("bout_id", "fighter_id", name="uq_bout_fighter"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bout_id: Mapped[int] = mapped_column(ForeignKey("bouts.id"))
    fighter_id: Mapped[int] = mapped_column(ForeignKey("fighters.id"), index=True)
    corner: Mapped[Corner] = mapped_column(
        Enum(Corner, name="corner", values_callable=lambda enum: [m.value for m in enum])
    )
    # Stats granulares daquela luta (nunca médias):
    knockdowns: Mapped[int | None]
    sig_strikes_landed: Mapped[int | None]
    sig_strikes_attempted: Mapped[int | None]
    takedowns_landed: Mapped[int | None]
    takedowns_attempted: Mapped[int | None]
    submission_attempts: Mapped[int | None]
    control_time_seconds: Mapped[int | None]
    # Splits de golpe (WIDE, atributo 1:1 do canto -- ADR 0004). Nullable: só serão
    # preenchidos pelo backfill do CSV do seed (Slice 02). São contagens granulares
    # POR LUTA (não médias): acurácia/distribuição derivam-se on demand.
    total_strikes_landed: Mapped[int | None]
    total_strikes_attempted: Mapped[int | None]
    head_landed: Mapped[int | None]
    head_attempted: Mapped[int | None]
    body_landed: Mapped[int | None]
    body_attempted: Mapped[int | None]
    leg_landed: Mapped[int | None]
    leg_attempted: Mapped[int | None]
    distance_landed: Mapped[int | None]
    distance_attempted: Mapped[int | None]
    clinch_landed: Mapped[int | None]
    clinch_attempted: Mapped[int | None]
    ground_landed: Mapped[int | None]
    ground_attempted: Mapped[int | None]
    reversals: Mapped[int | None]
    source: Mapped[str] = mapped_column(String(32))

    # Relationship ORM (só leitura, sem migration): a identidade do lutador daquele
    # canto. Carregado via ``selectinload`` nos selectors que expõem o nome (detalhe
    # da luta, head-to-head, histórico, card do evento), evitando N+1.
    fighter: Mapped[Fighter] = relationship()


class BoutFighterRound(Base):
    """Stats por canto POR ROUND (granularidade nova -- ADR 0004).

    Uma linha por lutador-por-luta-por-round, com o **conjunto completo** de stats
    (os 7 base + os 15 splits do RF-01), que o ``roundStats`` da Cito expõe
    (confirmado no piloto). Populada pelo backfill da Cito (Slice 05), origem
    ``cito``.

    Sem coluna ``corner``: o canto é o de ``bout_fighter_id`` (não se duplica o
    enum ``corner``, dono: ``bout_fighters``). Unicidade ``(bout_fighter_id, round)``
    garante idempotência do backfill.
    """

    __tablename__ = "bout_fighter_rounds"
    __table_args__ = (UniqueConstraint("bout_fighter_id", "round", name="uq_bout_fighter_round"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bout_fighter_id: Mapped[int] = mapped_column(ForeignKey("bout_fighters.id"), index=True)
    round: Mapped[int]
    # Conjunto completo por round: 7 base + 15 splits (todas nullable).
    knockdowns: Mapped[int | None]
    sig_strikes_landed: Mapped[int | None]
    sig_strikes_attempted: Mapped[int | None]
    takedowns_landed: Mapped[int | None]
    takedowns_attempted: Mapped[int | None]
    submission_attempts: Mapped[int | None]
    control_time_seconds: Mapped[int | None]
    total_strikes_landed: Mapped[int | None]
    total_strikes_attempted: Mapped[int | None]
    head_landed: Mapped[int | None]
    head_attempted: Mapped[int | None]
    body_landed: Mapped[int | None]
    body_attempted: Mapped[int | None]
    leg_landed: Mapped[int | None]
    leg_attempted: Mapped[int | None]
    distance_landed: Mapped[int | None]
    distance_attempted: Mapped[int | None]
    clinch_landed: Mapped[int | None]
    clinch_attempted: Mapped[int | None]
    ground_landed: Mapped[int | None]
    ground_attempted: Mapped[int | None]
    reversals: Mapped[int | None]
    source: Mapped[str] = mapped_column(String(32))
