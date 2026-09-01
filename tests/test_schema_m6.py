"""Testes de metadados do schema do M6 (não tocam o banco).

Afirmam a forma de ``Base.metadata`` diretamente dos models para o enriquecimento do M6
(SPEC 007): os identificadores externos da Cito em ``events`` -- ``cito_slug`` (indexado)
e ``cito_event_id`` -- e o contexto de card em ``bouts`` -- ``card_section``/``bout_order``
--, todos nullable e **aditivos** (nenhuma coluna existente muda de tipo ou de nulidade).

Isolado de ``tests/test_schema.py`` (fundacional) e de ``tests/test_schema_m5.py``, para
manter o escopo do M6 separado -- mesmo precedente da separação anterior.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.sql.schema import Table

# Importa os models (só side-effect) para registrar as tabelas em Base.metadata.
from apps.bouts import models as _bouts_models  # noqa: F401
from apps.events import models as _events_models  # noqa: F401
from mma_analytics.db import Base

# Colunas que o M6 acrescenta a ``events`` (SPEC 007, RF-04): o identificador do evento
# na Cito, resolvido a partir do catálogo real -- nunca derivado por regra.
IDENTIFICADORES_CITO = {"cito_slug", "cito_event_id"}

# Contexto de card que o M6 acrescenta a ``bouts`` (SPEC 007, Slice 05): o rótulo da seção
# do card e a posição ordenável da luta, ambos vindos do mesmo payload já pago.
CONTEXTO_DE_CARD = {"card_section", "bout_order"}


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


def test_bouts_ganha_contexto_de_card_nullable() -> None:
    """``bouts`` ganha ``card_section``/``bout_order``, ambos nullable (CA-06).

    Nullable é load-bearing: luta sem contexto no payload permanece nula -- ausência
    explícita, nunca sentinela.
    """
    colunas = _tabela("bouts").columns
    assert set(colunas.keys()) >= CONTEXTO_DE_CARD
    assert all(colunas[c].nullable for c in CONTEXTO_DE_CARD)


def test_bouts_card_section_guarda_o_rotulo_cru_da_cito() -> None:
    """``card_section`` é ``String(32)`` e guarda o rótulo **cru** ('Main Card'/'Prelims').

    Sem enum e sem normalização: o vocabulário da Cito é dela, e travá-lo num enum obrigaria
    uma migration a cada rótulo novo. O tamanho acomoda os rótulos observados com folga.
    """
    coluna = _tabela("bouts").columns["card_section"]
    assert isinstance(coluna.type, String)
    assert coluna.type.length == 32


def test_bouts_preserva_colunas_e_nulidade_existentes() -> None:
    """A migration do contexto de card é aditiva: nada pré-existente em ``bouts`` muda (CA-06)."""
    colunas = _tabela("bouts").columns
    assert colunas["event_id"].nullable is False
    assert colunas["method"].nullable is False
    assert colunas["source"].nullable is False
    assert colunas["winner_id"].nullable is True
    assert colunas["weight_class"].nullable is True
