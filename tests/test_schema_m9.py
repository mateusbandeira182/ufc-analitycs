"""Testes de metadados do schema do M9 (não tocam o banco).

Afirmam a forma de ``Base.metadata`` diretamente do model para o cache as-of por lutador
(SPEC 011, Slice 01): a tabela nova ``fighter_asof_features``, chaveada por ``fighter_id``,
com **duas** colunas JSONB distintas -- ``features`` (o vetor alinhável a ``feature_names``)
e ``style_history`` (o insumo do bloco D3, que é feature do PAR e por isso jamais entra no
vetor por lutador; decisão D-15 da SPEC 011).

A Slice 02 acrescentou aqui a forma de ``bouts`` depois de ``status``/``method`` nullable
(RF-07): mesmo marco, mesmo arquivo.

Isolado de ``tests/test_schema.py`` (fundacional) e dos testes de schema do M5/M6/M7 para
manter o escopo do M9 separado -- mesmo precedente das separações anteriores.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Enum, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.schema import Table

# Importa os models (só side-effect) para registrar as tabelas em Base.metadata.
from apps.bouts import models as _bouts_models  # noqa: F401
from apps.features import models as _features_models  # noqa: F401
from apps.fighters import models as _fighters_models  # noqa: F401
from mma_analytics.db import Base

TABELA_ASOF = "fighter_asof_features"

# As seis colunas do cache as-of (RF-01/RF-05 da SPEC 011).
COLUNAS_ASOF = {
    "fighter_id",
    "features",
    "style_history",
    "n_bouts_source",
    "source",
    "materialized_at",
}


def _tabela(nome: str) -> Table:
    return Base.metadata.tables[nome]


def test_fighter_asof_features_tem_as_seis_colunas() -> None:
    """A tabela do cache as-of expõe exatamente as seis colunas do contrato (CA-01)."""
    assert set(_tabela(TABELA_ASOF).columns.keys()) == COLUNAS_ASOF


def test_fighter_asof_features_fighter_id_pk_e_fk_para_fighters() -> None:
    """``fighter_id`` é PK e FK para ``fighters.id`` -- a chave natural do upsert (RF-02).

    PK não-nula é o que torna ``INSERT ... ON CONFLICT (fighter_id) DO UPDATE`` limpo e
    determinístico, o mesmo argumento que vale para ``bout_features.bout_id``.
    """
    fighter_id = _tabela(TABELA_ASOF).columns["fighter_id"]
    assert fighter_id.primary_key is True
    alvos = {fk.target_fullname for fk in fighter_id.foreign_keys}
    assert "fighters.id" in alvos


def test_fighter_asof_features_tem_duas_colunas_jsonb_nao_nulas() -> None:
    """``features`` e ``style_history`` são JSONB NOT NULL e **colunas distintas** (D-15).

    Duas colunas e não uma: ``style_history`` é o insumo de ``similar_style_win_rate_prior``,
    que é feature do PAR (depende de quem é o adversário). Misturá-la ao vetor convidaria
    alguém a alinhá-la a ``feature_names`` e quebrar o serving.
    """
    colunas = _tabela(TABELA_ASOF).columns
    for nome in ("features", "style_history"):
        assert isinstance(colunas[nome].type, JSONB), nome
        assert colunas[nome].nullable is False, nome


def test_fighter_asof_features_guarda_o_diagnostico_de_defasagem() -> None:
    """``n_bouts_source``/``source``/``materialized_at`` são NOT NULL (RF-05).

    ``n_bouts_source`` é o denominador da guarda barata de defasagem (comparado com o
    ``count(*)`` real) e ``materialized_at`` é timezone-aware -- instante, nunca data de
    calendário nua.
    """
    colunas = _tabela(TABELA_ASOF).columns
    assert colunas["n_bouts_source"].nullable is False
    assert colunas["n_bouts_source"].type.python_type is int
    assert colunas["source"].nullable is False
    assert isinstance(colunas["source"].type, String)
    materialized_at = colunas["materialized_at"]
    assert materialized_at.nullable is False
    assert materialized_at.type.python_type is datetime
    assert getattr(materialized_at.type, "timezone", False) is True


# ---------------------------------------------------------------------------- #
# Slice 02 -- ``bouts.status`` e ``method`` nullable (RF-07).
# ---------------------------------------------------------------------------- #

TABELA_BOUTS = "bouts"

# Nulidade esperada de cada coluna de ``bouts`` DEPOIS da Slice 02. A lista inteira entra
# de propósito: a migration mexe em duas colunas, e o único jeito de provar que não mexeu
# nas outras é declarar todas -- é a regressão que a tabela de varredura existe para evitar.
NULIDADE_BOUTS = {
    "id": False,
    "event_id": False,
    "winner_id": True,
    "method": True,  # nullable a partir da Slice 02: luta agendada não tem desfecho
    "round": True,
    "ending_time_seconds": True,
    "weight_class": True,
    "title_bout": True,
    "scheduled_rounds": True,
    "referee": True,
    "card_section": True,
    "bout_order": True,
    "status": False,  # toda luta tem status; o default do model é ``completed``
    "source": False,
}


def test_bouts_tem_status_nao_nulo_com_enum_nomeado() -> None:
    """``bouts.status`` é NOT NULL, enum ``bout_status`` com ``completed``/``scheduled``.

    O nome do tipo importa: é por ele que a migration cria e dropa o enum no Postgres, e é
    ele que a Sprint 04 vai referenciar ao escrever luta agendada.
    """
    status = _tabela(TABELA_BOUTS).columns["status"]
    assert status.nullable is False
    assert isinstance(status.type, Enum)
    assert status.type.name == "bout_status"
    assert set(status.type.enums) == {"completed", "scheduled"}


def test_bouts_method_passa_a_nullable() -> None:
    """``bouts.method`` é nullable (RF-07): luta agendada não tem método de encerramento.

    O enum ``bout_method`` continua o mesmo -- só a nulidade muda. Fabricar um método
    placeholder para a luta por vir gravaria desfecho falso no alvo do modelo.
    """
    method = _tabela(TABELA_BOUTS).columns["method"]
    assert method.nullable is True
    assert isinstance(method.type, Enum)
    assert method.type.name == "bout_method"


def test_bouts_nao_muda_a_nulidade_de_nenhuma_outra_coluna() -> None:
    """Fora de ``status`` e ``method``, ``bouts`` fica intacta (guarda de regressão)."""
    colunas = _tabela(TABELA_BOUTS).columns
    assert {nome: coluna.nullable for nome, coluna in colunas.items()} == NULIDADE_BOUTS
