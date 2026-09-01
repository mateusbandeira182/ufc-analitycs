"""Testes de metadados do schema do M6 (não tocam o banco).

Afirmam a forma de ``Base.metadata`` diretamente dos models para o enriquecimento do M6
(SPEC 007): os identificadores externos da Cito em ``events`` -- ``cito_slug`` (indexado)
e ``cito_event_id`` --, ambos nullable e **aditivos** (nenhuma coluna existente muda de
tipo ou de nulidade).

Isolado de ``tests/test_schema.py`` (fundacional) e de ``tests/test_schema_m5.py``, para
manter o escopo do M6 separado -- mesmo precedente da separação anterior.
"""

from __future__ import annotations

from sqlalchemy.sql.schema import Table

# Importa os models (só side-effect) para registrar as tabelas em Base.metadata.
from apps.events import models as _events_models  # noqa: F401
from mma_analytics.db import Base

# Colunas que o M6 acrescenta a ``events`` (SPEC 007, RF-04): o identificador do evento
# na Cito, resolvido a partir do catálogo real -- nunca derivado por regra.
IDENTIFICADORES_CITO = {"cito_slug", "cito_event_id"}


def _tabela(nome: str) -> Table:
    return Base.metadata.tables[nome]


def _tem_indice_na_coluna(tabela: Table, coluna: str) -> bool:
    return any(coluna in {c.name for c in idx.columns} for idx in tabela.indexes)


def test_events_ganha_identificadores_cito_nullable() -> None:
    """``events`` ganha ``cito_slug``/``cito_event_id``, ambos nullable (CA-01).

    Nullable é load-bearing: evento sem correspondência no catálogo permanece nulo --
    ausência explícita, nunca sentinela.
    """
    colunas = _tabela("events").columns
    assert set(colunas.keys()) >= IDENTIFICADORES_CITO
    assert all(colunas[c].nullable for c in IDENTIFICADORES_CITO)


def test_events_cito_slug_e_indexado() -> None:
    """``events.cito_slug`` é indexado (CA-01): a busca por slug é o acesso do backfill."""
    assert _tem_indice_na_coluna(_tabela("events"), "cito_slug")


def test_events_preserva_colunas_e_nulidade_existentes() -> None:
    """A migration do M6 é aditiva: nenhuma coluna pré-existente muda de nulidade (CA-01)."""
    colunas = _tabela("events").columns
    assert colunas["name"].nullable is False
    assert colunas["date"].nullable is False
    assert colunas["source"].nullable is False
    assert colunas["location"].nullable is True
