"""Teste de round-trip da migration inicial contra o Postgres de teste real.

``upgrade head`` deve criar as quatro tabelas; ``downgrade base`` deve reverter
sem resíduo -- nenhuma das tabelas e nenhum dos tipos enum (``stance``,
``bout_method``, ``corner``) pode permanecer (CA-01, CA-02).
"""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import Engine, inspect, text

from alembic import command

TABELAS = {"fighters", "events", "bouts", "bout_fighters"}
TIPOS_ENUM = {"stance", "bout_method", "corner"}
TABELA_DERIVADA = "bout_features"
TABELA_ROUNDS = "bout_fighter_rounds"


def _tabelas_existentes(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _colunas(engine: Engine, tabela: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(tabela)}


def _tipos_enum_existentes(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        linhas = conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'")).scalars()
        return set(linhas)


def test_upgrade_cria_as_quatro_tabelas(alembic_cfg: Config, migration_engine: Engine) -> None:
    """``alembic upgrade head`` cria as quatro tabelas no Postgres de teste."""
    command.upgrade(alembic_cfg, "head")
    assert _tabelas_existentes(migration_engine) >= TABELAS


def test_downgrade_reverte_sem_residuo(alembic_cfg: Config, migration_engine: Engine) -> None:
    """``alembic downgrade base`` remove tabelas e tipos enum sem deixar resíduo."""
    command.upgrade(alembic_cfg, "head")
    assert _tipos_enum_existentes(migration_engine) >= TIPOS_ENUM

    command.downgrade(alembic_cfg, "base")
    assert not (TABELAS & _tabelas_existentes(migration_engine))
    assert not (TIPOS_ENUM & _tipos_enum_existentes(migration_engine))


def test_upgrade_head_cria_bout_features(alembic_cfg: Config, migration_engine: Engine) -> None:
    """``alembic upgrade head`` cria a tabela derivada ``bout_features`` (cache reconstrutível)."""
    command.upgrade(alembic_cfg, "head")
    assert TABELA_DERIVADA in _tabelas_existentes(migration_engine)


def test_downgrade_um_passo_remove_bout_features_preserva_enum_corner(
    alembic_cfg: Config, migration_engine: Engine
) -> None:
    """O downgrade da migration de ``bout_features`` dropa SÓ a tabela derivada.

    O enum ``corner`` pertence a ``bout_fighters`` (migration inicial); nunca é dropado
    aqui. As tabelas granulares (``bouts``/``bout_fighters``) permanecem intactas.

    Fixa a revisão-alvo (``f3d1bfc5aa70``) em vez de ``head``: com a M5 empilhada
    acima, ``head`` deixou de ser a migration de ``bout_features``, e um ``-1`` a
    partir do head removeria a M5, não a tabela derivada.
    """
    command.upgrade(alembic_cfg, "f3d1bfc5aa70")
    assert TABELA_DERIVADA in _tabelas_existentes(migration_engine)

    command.downgrade(alembic_cfg, "-1")

    tabelas = _tabelas_existentes(migration_engine)
    assert TABELA_DERIVADA not in tabelas
    # O granular e o enum compartilhado sobrevivem ao downgrade de um passo.
    assert tabelas >= TABELAS
    assert "corner" in _tipos_enum_existentes(migration_engine)


def test_upgrade_cria_splits_contexto_e_rounds(
    alembic_cfg: Config, migration_engine: Engine
) -> None:
    """A migration M5 cria ``bout_fighter_rounds`` e as colunas wide aditivas."""
    command.upgrade(alembic_cfg, "head")
    assert TABELA_ROUNDS in _tabelas_existentes(migration_engine)
    colunas_bf = _colunas(migration_engine, "bout_fighters")
    assert {"head_landed", "total_strikes_landed", "reversals"} <= colunas_bf
    colunas_bouts = _colunas(migration_engine, "bouts")
    assert {"title_bout", "scheduled_rounds", "referee"} <= colunas_bouts
    assert "weight_kg" in _colunas(migration_engine, "fighters")


def test_downgrade_um_passo_remove_m5_preserva_granular_e_enum(
    alembic_cfg: Config, migration_engine: Engine
) -> None:
    """O downgrade da migration M5 remove só o próprio artefato aditivo.

    A tabela ``bout_fighter_rounds`` e as colunas wide somem; o granular
    pré-existente (``bouts``/``bout_fighters``/``fighters``) e o enum ``corner``
    (dono: ``bout_fighters``) permanecem intactos.
    """
    command.upgrade(alembic_cfg, "head")
    assert TABELA_ROUNDS in _tabelas_existentes(migration_engine)

    command.downgrade(alembic_cfg, "-1")

    tabelas = _tabelas_existentes(migration_engine)
    assert TABELA_ROUNDS not in tabelas
    assert tabelas >= TABELAS
    assert "head_landed" not in _colunas(migration_engine, "bout_fighters")
    assert "referee" not in _colunas(migration_engine, "bouts")
    assert "weight_kg" not in _colunas(migration_engine, "fighters")
    assert "corner" in _tipos_enum_existentes(migration_engine)
