"""Testes de metadados do schema (não tocam o banco).

Afirmam a forma de ``Base.metadata`` diretamente dos models: as quatro tabelas,
a coluna ``source`` obrigatória em todas, os índices e as unique constraints das
chaves naturais, a nulabilidade de ``winner_id`` e a granularidade de
``bout_fighters`` (uma linha por lutador-por-luta, sem médias pré-agregadas).
"""

from __future__ import annotations

from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.sql.schema import Table

# Importa os models para registrar as tabelas em Base.metadata.
import apps.bouts.models
import apps.events.models
import apps.fighters.models  # noqa: F401
from mma_analytics.db import Base

TABELAS = {"fighters", "events", "bouts", "bout_fighters"}


def _tabela(nome: str) -> Table:
    return Base.metadata.tables[nome]


def _tem_indice_na_coluna(tabela: Table, coluna: str) -> bool:
    return any(coluna in {c.name for c in idx.columns} for idx in tabela.indexes)


def _tem_unique(tabela: Table, colunas: set[str]) -> bool:
    return any(
        isinstance(c, UniqueConstraint) and {col.name for col in c.columns} == colunas
        for c in tabela.constraints
    )


def test_metadata_tem_as_quatro_tabelas() -> None:
    """As quatro tabelas do domínio estão registradas em Base.metadata."""
    assert set(Base.metadata.tables) >= TABELAS


def test_toda_tabela_tem_source_not_null() -> None:
    """Toda tabela tem coluna ``source`` NOT NULL (rastreio de origem)."""
    for nome in TABELAS:
        colunas = _tabela(nome).columns
        assert "source" in colunas, f"{nome} sem coluna source"
        assert colunas["source"].nullable is False, f"{nome}.source deveria ser NOT NULL"


def test_fighters_name_normalized_indexado() -> None:
    """``fighters.name_normalized`` tem índice (chave de dedup do seed)."""
    assert _tem_indice_na_coluna(_tabela("fighters"), "name_normalized")


def test_events_tem_unique_name_date() -> None:
    """``events`` tem unique constraint na chave natural (name, date)."""
    assert _tem_unique(_tabela("events"), {"name", "date"})


def test_bout_fighters_tem_unique_bout_fighter() -> None:
    """``bout_fighters`` é unique por (bout_id, fighter_id): uma linha por lutador-por-luta."""
    assert _tem_unique(_tabela("bout_fighters"), {"bout_id", "fighter_id"})


def test_bouts_winner_id_nullable() -> None:
    """``bouts.winner_id`` é nullable (suporta empate / no contest)."""
    assert _tabela("bouts").columns["winner_id"].nullable is True


def test_bout_fighters_tem_stats_granulares_e_corner() -> None:
    """``bout_fighters`` guarda as stats granulares por luta e o canto, sem médias."""
    colunas = set(_tabela("bout_fighters").columns.keys())
    granulares = {
        "corner",
        "knockdowns",
        "sig_strikes_landed",
        "sig_strikes_attempted",
        "takedowns_landed",
        "takedowns_attempted",
        "submission_attempts",
        "control_time_seconds",
    }
    assert granulares <= colunas
    # Nenhuma coluna de média/agregação destrutiva é modelada.
    assert not {c for c in colunas if c.startswith(("avg_", "mean_", "total_"))}


def test_bouts_ending_time_em_segundos() -> None:
    """A duração de encerramento da luta é inteiro em segundos (Decisão #3), não string."""
    coluna = _tabela("bouts").columns["ending_time_seconds"]
    assert coluna.type.python_type is int
    assert coluna.nullable is True


def test_bout_fighters_indice_por_fighter_para_historico() -> None:
    """Há índice em ``bout_fighters.fighter_id`` (base da série temporal por lutador)."""
    assert _tem_indice_na_coluna(_tabela("bout_fighters"), "fighter_id")


def test_todas_as_indexes_declaradas() -> None:
    """Smoke: cada tabela expõe seu objeto Table (guarda contra regressão de import)."""
    for nome in TABELAS:
        assert isinstance(_tabela(nome), Table)
        assert isinstance(next(iter(_tabela("fighters").indexes), Index("x")), Index)
