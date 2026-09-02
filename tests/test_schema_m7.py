"""Testes de metadados do schema do M7 (não tocam o banco).

Afirmam a forma de ``Base.metadata`` diretamente dos models para a fonte oficial da UFC
(SPEC 008): o identificador externo do evento em ``events`` -- ``ufc_event_id`` -- e os do
lutador em ``fighters`` -- ``ufc_fighter_id``/``ufc_mma_id`` --, todos nullable e **aditivos**
(nenhuma coluna existente muda de tipo ou de nulidade).

Isolado de ``tests/test_schema.py`` (fundacional), ``tests/test_schema_m5.py`` e
``tests/test_schema_m6.py``, para manter o escopo do M7 separado -- mesmo precedente das
separações anteriores.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.sql.schema import Table

# Importa os models (só side-effect) para registrar as tabelas em Base.metadata.
from apps.events import models as _events_models  # noqa: F401
from apps.fighters import models as _fighters_models  # noqa: F401
from mma_analytics.db import Base

# Coluna que o M7 acrescenta a ``events`` (SPEC 008, RF-07): o identificador do evento na API
# oficial da UFC, resolvido casando data local + nome normalizado contra o catálogo varrido --
# nunca derivado por regra a partir do nome.
IDENTIFICADOR_OFICIAL = "ufc_event_id"

# Colunas que o M7 acrescenta a ``fighters`` (SPEC 008, Slice 03, RF-07): os identificadores do
# lutador na API oficial da UFC, observados nas lutas dos eventos já mapeados -- nunca derivados
# do nome.
IDENTIFICADORES_FONTE_OFICIAL_FIGHTER = {"ufc_fighter_id", "ufc_mma_id"}


def _tabela(nome: str) -> Table:
    return Base.metadata.tables[nome]


def _tem_indice_na_coluna(tabela: Table, coluna: str) -> bool:
    return any(coluna in {c.name for c in idx.columns} for idx in tabela.indexes)


def test_events_ganha_ufc_event_id_nullable() -> None:
    """``events`` ganha ``ufc_event_id`` nullable (CA-01).

    Nullable é load-bearing: evento sem correspondência na fonte oficial permanece nulo --
    ausência explícita, nunca sentinela. Um id externo errado é pior que ausente, porque a
    Slice 04 corrige canto a partir dele.
    """
    colunas = _tabela("events").columns
    assert IDENTIFICADOR_OFICIAL in colunas
    assert colunas[IDENTIFICADOR_OFICIAL].nullable is True


def test_events_ufc_event_id_e_string_32_indexada() -> None:
    """``ufc_event_id`` é ``String(32)`` e indexado (CA-01).

    Indexado porque a Slice 04 parte do id externo para o evento persistido. ``String(32)``
    guarda o inteiro da fonte como texto opaco, no mesmo padrão de ``cito_event_id``.
    """
    coluna = _tabela("events").columns[IDENTIFICADOR_OFICIAL]
    assert isinstance(coluna.type, String)
    assert coluna.type.length == 32
    assert _tem_indice_na_coluna(_tabela("events"), IDENTIFICADOR_OFICIAL)


def test_events_preserva_colunas_e_nulidade_existentes() -> None:
    """A migration do M7 é aditiva: nenhuma coluna pré-existente muda de nulidade (CA-01)."""
    colunas = _tabela("events").columns
    assert colunas["name"].nullable is False
    assert colunas["date"].nullable is False
    assert colunas["source"].nullable is False
    assert colunas["location"].nullable is True
    # Os identificadores da Cito (M6) continuam existindo e nullable: o M7 não os aposenta.
    assert colunas["cito_slug"].nullable is True
    assert colunas["cito_event_id"].nullable is True


def test_fighters_ganha_identificadores_da_fonte_oficial_nullable() -> None:
    """``fighters`` ganha ``ufc_fighter_id``/``ufc_mma_id`` nullable (CA-01).

    Nullable é load-bearing e a ausência é o **estado normal**, não uma falha: lutador ativo
    apenas antes de 2010-03-21 está fora da janela por decisão (RF-03) e nunca receberá id.
    Nunca sentinela, nunca string vazia.
    """
    colunas = _tabela("fighters").columns
    assert set(colunas.keys()) >= IDENTIFICADORES_FONTE_OFICIAL_FIGHTER
    assert all(colunas[nome].nullable is True for nome in IDENTIFICADORES_FONTE_OFICIAL_FIGHTER)


def test_fighters_ufc_fighter_id_e_string_32_indexada() -> None:
    """``ufc_fighter_id`` é ``String(32)`` e indexado; ``ufc_mma_id`` é ``String(32)`` (CA-01).

    Indexado porque a entity resolution passa a resolver **por ele** antes do nome (RF-07).
    ``String(32)`` guarda o inteiro da fonte como texto opaco -- identificador externo nunca
    entra em aritmética --, no mesmo padrão de ``events.ufc_event_id`` e ``cito_event_id``.
    O ``ufc_mma_id`` não é indexado: nada resolve por ele, ele só acompanha o dado.
    """
    colunas = _tabela("fighters").columns
    for nome in IDENTIFICADORES_FONTE_OFICIAL_FIGHTER:
        tipo = colunas[nome].type
        assert isinstance(tipo, String)
        assert tipo.length == 32
    assert _tem_indice_na_coluna(_tabela("fighters"), "ufc_fighter_id")


def test_fighters_preserva_colunas_e_nulidade_existentes() -> None:
    """A migration do M7 é aditiva em ``fighters``: nada pré-existente muda de nulidade (CA-01)."""
    colunas = _tabela("fighters").columns
    assert colunas["name"].nullable is False
    assert colunas["name_normalized"].nullable is False
    assert colunas["source"].nullable is False
    assert colunas["date_of_birth"].nullable is True
    assert colunas["weight_kg"].nullable is True
    assert {"wins", "losses", "draws"} <= set(colunas.keys())
    assert all(colunas[nome].nullable is False for nome in ("wins", "losses", "draws"))
