"""Testes de metadados do schema de ``bout_predictions`` (não tocam o banco).

Afirmam a forma de ``Base.metadata`` diretamente do model do registro de predições
(SPEC 007, M6, Slice 07 -- RF-10): a chave natural ``(bout_id, model_version)`` que
sustenta a idempotência e a série temporal por versão de modelo, o índice de acesso
por luta, o rastreio de origem (``source``) e o instante tz-aware da predição.

Guarda load-bearing: **o acerto não é coluna**. Ele deriva-se de ``bouts.winner_id``
(fonte da verdade do resultado) contra ``predicted_winner_id``, sob demanda -- persistir
"acertou" seria pré-agregação destrutiva e ficaria silenciosamente errado se o resultado
fosse corrigido depois (princípio de granularidade do ``CLAUDE.md``, ADR 0001).

Isolado de ``tests/test_schema.py`` (fundacional) e de ``tests/test_schema_m5.py``
(enriquecimento granular) para manter o escopo desta slice separado.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint
from sqlalchemy.sql.schema import Table

# Importa os models (só side-effect) para registrar as tabelas em Base.metadata.
from apps.bouts import models as _bouts_models  # noqa: F401
from apps.fighters import models as _fighters_models  # noqa: F401
from apps.predictions import models as _predictions_models  # noqa: F401
from mma_analytics.db import Base

TABELA = "bout_predictions"

COLUNAS_ESPERADAS = {
    "id",
    "bout_id",
    "predicted_winner_id",
    "prob_predicted_winner",
    "model_version",
    "n_features",
    "predicted_at",
    "source",
}

# Nomes que denunciariam a persistência do acerto (o que esta slice recusa fazer).
NOMES_DE_ACERTO = {"hit", "correct", "is_correct", "acertou", "accuracy"}


def _tabela(nome: str) -> Table:
    return Base.metadata.tables[nome]


def _tem_indice_na_coluna(tabela: Table, coluna: str) -> bool:
    return any(coluna in {c.name for c in idx.columns} for idx in tabela.indexes)


def _tem_unique(tabela: Table, colunas: set[str]) -> bool:
    return any(
        isinstance(c, UniqueConstraint) and {col.name for col in c.columns} == colunas
        for c in tabela.constraints
    )


def test_bout_predictions_tem_as_colunas_da_spec() -> None:
    """RF-10: a tabela guarda vencedor previsto, probabilidade, versão e instante."""
    assert set(_tabela(TABELA).columns.keys()) == COLUNAS_ESPERADAS


def test_bout_predictions_tem_pk_propria() -> None:
    """A PK é o serial ``id``: a chave natural é unique, não primária (uma luta tem N versões)."""
    tabela = _tabela(TABELA)
    assert tabela.columns["id"].primary_key is True
    assert {c.name for c in tabela.primary_key.columns} == {"id"}


def test_bout_predictions_unicidade_composta_nomeada() -> None:
    """CA-08: unique em ``(bout_id, model_version)`` -- a idempotência da gravação.

    Mesma luta + mesma versão de modelo não duplica; versão nova gera linha nova, e é
    isso que faz a série temporal do walk-forward existir como dado.
    """
    tabela = _tabela(TABELA)
    assert _tem_unique(tabela, {"bout_id", "model_version"})
    nomes = {c.name for c in tabela.constraints if isinstance(c, UniqueConstraint)}
    assert "uq_bout_prediction" in nomes


def test_bout_predictions_fk_para_bouts_indexada_e_obrigatoria() -> None:
    """``bout_id -> bouts.id`` é NOT NULL e indexada (acesso por luta)."""
    tabela = _tabela(TABELA)
    coluna = tabela.columns["bout_id"]
    assert {fk.target_fullname for fk in coluna.foreign_keys} == {"bouts.id"}
    assert coluna.nullable is False
    assert _tem_indice_na_coluna(tabela, "bout_id")


def test_bout_predictions_vencedor_previsto_e_fk_nullable_para_fighters() -> None:
    """``predicted_winner_id -> fighters.id`` é nullable, por simetria com ``bouts.winner_id``.

    A comparação previsto x ocorrido é de três estados nos dois lados: sem vencedor
    previsto não há acerto nem erro a computar.
    """
    coluna = _tabela(TABELA).columns["predicted_winner_id"]
    assert {fk.target_fullname for fk in coluna.foreign_keys} == {"fighters.id"}
    assert coluna.nullable is True


def test_bout_predictions_versao_e_contagem_obrigatorias() -> None:
    """``model_version`` e ``n_features`` são NOT NULL: sem eles a predição não é rastreável."""
    colunas = _tabela(TABELA).columns
    assert colunas["model_version"].nullable is False
    assert colunas["model_version"].type.python_type is str
    assert colunas["n_features"].nullable is False
    assert colunas["n_features"].type.python_type is int
    assert colunas["prob_predicted_winner"].nullable is False
    assert colunas["prob_predicted_winner"].type.python_type is float


def test_bout_predictions_source_obrigatorio() -> None:
    """Rastreio de origem: ``source`` NOT NULL em toda escrita (invariante do CLAUDE.md)."""
    coluna = _tabela(TABELA).columns["source"]
    assert coluna.nullable is False
    assert coluna.type.python_type is str


def test_bout_predictions_predicted_at_e_timezone_aware() -> None:
    """``predicted_at`` é um instante tz-aware (UTC), nunca naive (convenção do projeto)."""
    coluna = _tabela(TABELA).columns["predicted_at"]
    assert coluna.nullable is False
    assert coluna.type.timezone is True  # type: ignore[attr-defined]


def test_bout_predictions_nao_persiste_acerto() -> None:
    """O acerto é DERIVADO de ``bouts.winner_id``, nunca persistido (pré-agregação destrutiva).

    Também nenhuma coluna pré-agregada (``avg_``/``mean_``): métricas de acerto são
    calculadas sob demanda a partir do granular.
    """
    colunas = set(_tabela(TABELA).columns.keys())
    assert not (colunas & NOMES_DE_ACERTO)
    assert not {c for c in colunas if c.startswith(("avg_", "mean_"))}
