"""Testes de metadados do schema do M5 (não tocam o banco).

Afirmam a forma de ``Base.metadata`` diretamente dos models para o enriquecimento
granular (ADR 0004): as 15 colunas wide de split em ``bout_fighters``, o contexto
de luta em ``bouts`` (``title_bout``/``scheduled_rounds``/``referee``),
``fighters.weight_kg`` e a tabela nova ``bout_fighter_rounds`` (conjunto completo
de stats por canto por round, sem coluna ``corner`` -- o canto vem do
``bout_fighter_id`` pai).

Isolado de ``tests/test_schema.py`` (fundacional) para manter o escopo M5 separado.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint
from sqlalchemy.sql.schema import Table

# Importa os models (só side-effect) para registrar as tabelas em Base.metadata.
from apps.bouts import models as _bouts_models  # noqa: F401
from apps.fighters import models as _fighters_models  # noqa: F401
from mma_analytics.db import Base

# RF-01: 15 colunas wide de split (7 pares landed/attempted + reversals).
SPLITS = {
    "total_strikes_landed",
    "total_strikes_attempted",
    "head_landed",
    "head_attempted",
    "body_landed",
    "body_attempted",
    "leg_landed",
    "leg_attempted",
    "distance_landed",
    "distance_attempted",
    "clinch_landed",
    "clinch_attempted",
    "ground_landed",
    "ground_attempted",
    "reversals",
}

# 7 stats base por round (já existentes em bout_fighters, agora também por round).
BASE_STATS = {
    "knockdowns",
    "sig_strikes_landed",
    "sig_strikes_attempted",
    "takedowns_landed",
    "takedowns_attempted",
    "submission_attempts",
    "control_time_seconds",
}

# RF-04: conjunto completo por round = 7 base + 15 splits = 22 colunas de stat.
ROUND_STATS = BASE_STATS | SPLITS


def _tabela(nome: str) -> Table:
    return Base.metadata.tables[nome]


def _tem_indice_na_coluna(tabela: Table, coluna: str) -> bool:
    return any(coluna in {c.name for c in idx.columns} for idx in tabela.indexes)


def _tem_unique(tabela: Table, colunas: set[str]) -> bool:
    return any(
        isinstance(c, UniqueConstraint) and {col.name for col in c.columns} == colunas
        for c in tabela.constraints
    )


def test_bout_fighters_ganha_15_splits_nullable() -> None:
    """``bout_fighters`` ganha as 15 colunas wide de split, todas nullable (RF-01)."""
    colunas = _tabela("bout_fighters").columns
    assert len(SPLITS) == 15
    assert set(colunas.keys()) >= SPLITS
    assert all(colunas[c].nullable for c in SPLITS)


def test_bouts_ganha_contexto_nullable() -> None:
    """``bouts`` ganha ``title_bout``/``scheduled_rounds``/``referee`` nullable (RF-02)."""
    colunas = _tabela("bouts").columns
    contexto = {"title_bout", "scheduled_rounds", "referee"}
    assert contexto <= set(colunas.keys())
    assert all(colunas[c].nullable for c in contexto)
    assert colunas["title_bout"].type.python_type is bool
    assert colunas["scheduled_rounds"].type.python_type is int


def test_fighters_ganha_weight_kg_nullable_float() -> None:
    """``fighters.weight_kg`` existe, é nullable e é float (atributo físico, RF-03)."""
    coluna = _tabela("fighters").columns["weight_kg"]
    assert coluna.nullable is True
    assert coluna.type.python_type is float


def test_bout_fighter_rounds_existe_com_pk_e_source() -> None:
    """``bout_fighter_rounds`` existe com PK ``id``, ``round`` e ``source`` NOT NULL (RF-04)."""
    tabela = _tabela("bout_fighter_rounds")
    assert tabela.columns["id"].primary_key is True
    assert tabela.columns["round"].nullable is False
    assert tabela.columns["source"].nullable is False


def test_bout_fighter_rounds_fk_indexada_para_bout_fighters() -> None:
    """A FK ``bout_fighter_id -> bout_fighters.id`` existe e é indexada (RF-04)."""
    tabela = _tabela("bout_fighter_rounds")
    alvos = {fk.target_fullname for fk in tabela.columns["bout_fighter_id"].foreign_keys}
    assert alvos == {"bout_fighters.id"}
    assert tabela.columns["bout_fighter_id"].nullable is False
    assert _tem_indice_na_coluna(tabela, "bout_fighter_id")


def test_bout_fighter_rounds_unicidade_composta_nomeada() -> None:
    """``bout_fighter_rounds`` é unique por ``(bout_fighter_id, round)`` (RF-04)."""
    tabela = _tabela("bout_fighter_rounds")
    assert _tem_unique(tabela, {"bout_fighter_id", "round"})
    nomes = {c.name for c in tabela.constraints if isinstance(c, UniqueConstraint)}
    assert "uq_bout_fighter_round" in nomes


def test_bout_fighter_rounds_tem_conjunto_completo_de_stats_nullable() -> None:
    """``bout_fighter_rounds`` guarda o conjunto completo (7 base + 15 splits) nullable."""
    colunas = _tabela("bout_fighter_rounds").columns
    assert len(ROUND_STATS) == 22
    assert set(colunas.keys()) >= ROUND_STATS
    assert all(colunas[c].nullable for c in ROUND_STATS)


def test_bout_fighter_rounds_nao_tem_coluna_corner() -> None:
    """``bout_fighter_rounds`` não duplica o canto: sem coluna ``corner`` (vem do pai)."""
    assert "corner" not in _tabela("bout_fighter_rounds").columns
