"""Testes de Selector de fighters contra o Postgres de teste transacional.

Cobrem ``list_fighters`` (sem filtro, filtro por nome case-insensitive substring,
paginação por limit/offset com ordem estável), ``get_fighter_by_id`` (encontrado
e inexistente), ``get_fighter_history`` (série cronológica, stats do canto
consultado, lutador sem lutas) e ``get_fighter_stats`` (médias e vitórias por
método computadas on demand, ignorando NULL, sem persistir agregado). Semeiam via
factories e exercitam o selector direto, sem subir a API.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.fighters.models import Fighter
from apps.fighters.selectors import (
    get_fighter_by_id,
    get_fighter_history,
    get_fighter_stats,
    list_fighters,
)
from apps.fighters.tests.factories import FighterFactory
from mma_analytics.db import Base


def _add(session: Session, name: str) -> Fighter:
    """Semeia um fighter com o ``name`` dado e devolve o model persistido."""
    fighter = FighterFactory.build(name=name)
    session.add(fighter)
    session.flush()
    return fighter


def _add_history_bout(
    session: Session,
    fighter: Fighter,
    *,
    event_name: str,
    event_date: date,
    won: bool,
) -> Bout:
    """Semeia um evento, uma luta e o canto do ``fighter`` naquela luta."""
    event = EventFactory.build(name=event_name, date=event_date)
    session.add(event)
    session.flush()
    bout = BoutFactory.build(
        event_id=event.id,
        winner_id=fighter.id if won else None,
        method=BoutMethod.DECISION,
    )
    session.add(bout)
    session.flush()
    session.add(BoutFighterFactory.build(bout_id=bout.id, fighter_id=fighter.id, corner=Corner.RED))
    session.flush()
    return bout


def test_list_fighters_sem_filtro_devolve_todos(db_session: Session) -> None:
    """Sem filtro, devolve todos os fighters e o ``total`` reflete o conjunto."""
    for name in ("Jon Jones", "Alex Pereira", "Israel Adesanya"):
        _add(db_session, name)

    rows, total = list_fighters(db_session, name=None, limit=50, offset=0)

    assert total == 3
    assert {row.name for row in rows} == {"Jon Jones", "Alex Pereira", "Israel Adesanya"}


def test_list_fighters_filtra_por_nome_case_insensitive(db_session: Session) -> None:
    """``name`` filtra por substring ignorando caixa; ``total`` reflete o filtro."""
    _add(db_session, "Alexander Volkanovski")
    _add(db_session, "Alex Pereira")
    _add(db_session, "Jon Jones")

    rows, total = list_fighters(db_session, name="alex", limit=50, offset=0)

    assert total == 2
    assert {row.name for row in rows} == {"Alexander Volkanovski", "Alex Pereira"}


def test_list_fighters_pagina_com_ordem_estavel(db_session: Session) -> None:
    """``limit``/``offset`` paginam sobre uma ordem determinística por ``name``."""
    for name in ("Charlie", "Alpha", "Bravo", "Delta"):
        _add(db_session, name)

    primeira, total = list_fighters(db_session, name=None, limit=2, offset=0)
    segunda, _ = list_fighters(db_session, name=None, limit=2, offset=2)

    assert total == 4
    assert [row.name for row in primeira] == ["Alpha", "Bravo"]
    assert [row.name for row in segunda] == ["Charlie", "Delta"]


def test_list_fighters_nome_sem_match_devolve_vazio(db_session: Session) -> None:
    """Filtro sem correspondência devolve lista vazia e ``total`` zero."""
    _add(db_session, "Jon Jones")

    rows, total = list_fighters(db_session, name="inexistente", limit=50, offset=0)

    assert rows == []
    assert total == 0


def test_list_fighters_offset_alem_do_total_devolve_vazio(db_session: Session) -> None:
    """``offset`` além do total devolve página vazia, mas ``total`` cheio."""
    _add(db_session, "Jon Jones")
    _add(db_session, "Alex Pereira")

    rows, total = list_fighters(db_session, name=None, limit=50, offset=10)

    assert rows == []
    assert total == 2


def test_get_fighter_by_id_encontrado_e_inexistente(db_session: Session) -> None:
    """Devolve o fighter pelo id e ``None`` quando o id não existe."""
    fighter = _add(db_session, "Jon Jones")

    assert get_fighter_by_id(db_session, fighter.id) is fighter
    assert get_fighter_by_id(db_session, 999_999) is None


def test_get_fighter_history_ordem_cronologica_ascendente(db_session: Session) -> None:
    """Lutas semeadas fora de ordem saem por ``events.date`` ascendente."""
    fighter = _add(db_session, "Max Holloway")
    _add_history_bout(
        db_session, fighter, event_name="UFC 300", event_date=date(2024, 4, 13), won=True
    )
    _add_history_bout(
        db_session, fighter, event_name="UFC 200", event_date=date(2016, 7, 9), won=False
    )
    _add_history_bout(
        db_session, fighter, event_name="UFC 250", event_date=date(2020, 6, 6), won=True
    )

    history = get_fighter_history(db_session, fighter.id)

    assert [row.event.date for row in history] == [
        date(2016, 7, 9),
        date(2020, 6, 6),
        date(2024, 4, 13),
    ]
    assert [row.event.name for row in history] == ["UFC 200", "UFC 250", "UFC 300"]


def test_get_fighter_history_traz_stats_do_canto_consultado(db_session: Session) -> None:
    """Numa luta de dois cantos, devolve as stats do lutador consultado, não do oponente."""
    fighter = _add(db_session, "Charles Oliveira")
    opponent = _add(db_session, "Justin Gaethje")
    event = EventFactory.build(name="UFC 274", date=date(2022, 5, 7))
    db_session.add(event)
    db_session.flush()
    bout = BoutFactory.build(event_id=event.id, winner_id=fighter.id, method=BoutMethod.SUBMISSION)
    db_session.add(bout)
    db_session.flush()
    db_session.add_all(
        [
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=fighter.id,
                corner=Corner.RED,
                sig_strikes_landed=30,
                control_time_seconds=120,
            ),
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=opponent.id,
                corner=Corner.BLUE,
                sig_strikes_landed=8,
                control_time_seconds=10,
            ),
        ]
    )
    db_session.flush()

    history = get_fighter_history(db_session, fighter.id)

    assert len(history) == 1
    row = history[0]
    assert row.stats.fighter_id == fighter.id
    assert row.stats.corner == Corner.RED
    assert row.stats.sig_strikes_landed == 30
    assert row.stats.control_time_seconds == 120
    assert row.bout.id == bout.id


def test_get_fighter_history_lutador_sem_lutas_devolve_vazio(db_session: Session) -> None:
    """Lutador existente sem lutas devolve sequência vazia (distinto de not-found)."""
    fighter = _add(db_session, "Ilia Topuria")

    assert get_fighter_history(db_session, fighter.id) == []


def _add_stats_bout(
    session: Session,
    fighter: Fighter,
    *,
    method: BoutMethod,
    winner: Fighter | None,
    sig_strikes_landed: int | None = None,
    takedowns_landed: int | None = None,
    control_time_seconds: int | None = None,
) -> Bout:
    """Semeia um evento, uma luta e o canto do ``fighter`` com stats conhecidas.

    ``winner`` é o lutador vencedor (``None`` em empate/no contest -> ``winner_id``
    nulo). As stats são as do canto do ``fighter`` consultado naquela luta.
    """
    event = EventFactory.build()
    session.add(event)
    session.flush()
    bout = BoutFactory.build(
        event_id=event.id,
        winner_id=winner.id if winner is not None else None,
        method=method,
    )
    session.add(bout)
    session.flush()
    session.add(
        BoutFighterFactory.build(
            bout_id=bout.id,
            fighter_id=fighter.id,
            corner=Corner.RED,
            sig_strikes_landed=sig_strikes_landed,
            takedowns_landed=takedowns_landed,
            control_time_seconds=control_time_seconds,
        )
    )
    session.flush()
    return bout


def _table_counts(session: Session) -> dict[str, int]:
    """Contagem de linhas das tabelas do domínio, para provar leitura sem escrita."""
    return {
        "fighters": session.scalar(select(func.count()).select_from(Fighter)) or 0,
        "bouts": session.scalar(select(func.count()).select_from(Bout)) or 0,
        "bout_fighters": session.scalar(select(func.count()).select_from(BoutFighter)) or 0,
    }


def test_get_fighter_stats_computes_averages_and_wins(db_session: Session) -> None:
    """Médias e vitórias por método computadas on demand a partir de ``bout_fighters``."""
    target = _add(db_session, "Alexander Volkanovski")
    _add_stats_bout(
        db_session,
        target,
        method=BoutMethod.KO_TKO,
        winner=target,
        sig_strikes_landed=10,
        takedowns_landed=2,
        control_time_seconds=60,
    )
    _add_stats_bout(
        db_session,
        target,
        method=BoutMethod.DECISION,
        winner=target,
        sig_strikes_landed=20,
        takedowns_landed=4,
        control_time_seconds=120,
    )

    stats = get_fighter_stats(db_session, target.id)

    assert stats is not None
    assert stats.fighter_id == target.id
    assert stats.bouts_counted == 2
    assert stats.avg_sig_strikes_landed == 15.0
    assert stats.avg_takedowns_landed == 3.0
    assert stats.avg_control_time_seconds == 90.0
    assert stats.wins_by_method == {"ko_tko": 1, "decision": 1}


def test_get_fighter_stats_avg_ignora_null(db_session: Session) -> None:
    """``func.avg`` ignora NULL; a luta com stat nula ainda conta em ``bouts_counted``."""
    target = _add(db_session, "Islam Makhachev")
    _add_stats_bout(
        db_session, target, method=BoutMethod.DECISION, winner=target, sig_strikes_landed=10
    )
    _add_stats_bout(
        db_session, target, method=BoutMethod.DECISION, winner=target, sig_strikes_landed=None
    )

    stats = get_fighter_stats(db_session, target.id)

    assert stats is not None
    assert stats.bouts_counted == 2  # ambas as lutas contam
    assert stats.avg_sig_strikes_landed == 10.0  # média só sobre o valor não-nulo


def test_get_fighter_stats_vitorias_excluem_derrota_e_empate(db_session: Session) -> None:
    """Só ``winner_id == fighter_id`` conta como vitória; derrota e empate não entram."""
    target = _add(db_session, "Max Holloway")
    opponent = _add(db_session, "Dustin Poirier")
    _add_stats_bout(db_session, target, method=BoutMethod.KO_TKO, winner=target)
    _add_stats_bout(db_session, target, method=BoutMethod.DECISION, winner=opponent)
    _add_stats_bout(db_session, target, method=BoutMethod.DECISION, winner=None)

    stats = get_fighter_stats(db_session, target.id)

    assert stats is not None
    assert stats.bouts_counted == 3
    assert stats.wins_by_method == {"ko_tko": 1}


def test_get_fighter_stats_lutador_inexistente_devolve_none(db_session: Session) -> None:
    """Lutador inexistente devolve ``None`` (habilita o 404, distinto de sem lutas)."""
    assert get_fighter_stats(db_session, 999_999) is None


def test_get_fighter_stats_lutador_sem_lutas(db_session: Session) -> None:
    """Lutador existente sem lutas: contagem zero, médias ``None`` e vitórias vazias."""
    target = _add(db_session, "Ilia Topuria")

    stats = get_fighter_stats(db_session, target.id)

    assert stats is not None
    assert stats.fighter_id == target.id
    assert stats.bouts_counted == 0
    assert stats.avg_sig_strikes_landed is None
    assert stats.avg_takedowns_landed is None
    assert stats.avg_control_time_seconds is None
    assert stats.wins_by_method == {}


def test_get_fighter_stats_on_demand_sem_persistencia_de_agregado(db_session: Session) -> None:
    """Invariante (RF-08): agregação recalculada ao vivo, sem escrita nem tabela de agregado."""
    target = _add(db_session, "Jon Jones")
    _add_stats_bout(
        db_session, target, method=BoutMethod.DECISION, winner=target, sig_strikes_landed=10
    )

    # (1) Recálculo ao vivo: mutar o dado granular muda a média.
    antes = get_fighter_stats(db_session, target.id)
    assert antes is not None
    assert antes.avg_sig_strikes_landed == 10.0
    _add_stats_bout(
        db_session, target, method=BoutMethod.DECISION, winner=target, sig_strikes_landed=90
    )
    depois = get_fighter_stats(db_session, target.id)
    assert depois is not None
    assert depois.avg_sig_strikes_landed == 50.0
    assert depois.avg_sig_strikes_landed != antes.avg_sig_strikes_landed

    # (2) Sem efeito de escrita: consultar o selector não altera a contagem de linhas.
    contagens_antes = _table_counts(db_session)
    get_fighter_stats(db_session, target.id)
    assert _table_counts(db_session) == contagens_antes

    # (3) Sem tabela/coluna de agregado no schema.
    assert not any("stat" in table for table in Base.metadata.tables)
    fighter_columns = {column.key for column in Base.metadata.tables["fighters"].columns}
    assert not any(column.startswith(("avg_", "mean_", "total_")) for column in fighter_columns)
