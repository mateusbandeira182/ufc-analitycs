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
# Identificadores externos da Cito acrescentados a ``events`` pelo M6 (SPEC 007, Slice 03).
IDENTIFICADORES_CITO = {"cito_slug", "cito_event_id"}
# Registro de predições (SPEC 007, Slice 07).
TABELA_PREDICOES = "bout_predictions"
# Contexto de card acrescentado a ``bouts`` pelo M6 (SPEC 007, Slice 05).
CONTEXTO_DE_CARD = {"card_section", "bout_order"}
# Identificador externo da fonte oficial da UFC acrescentado a ``events`` pelo M7
# (SPEC 008, Slice 02).
IDENTIFICADOR_OFICIAL = "ufc_event_id"
# Revisão da migration do M7, fixada por hash (nunca ``head``): é ela que o teste de
# ``downgrade -1`` precisa alcançar, e uma migration futura empilhada acima faria ``head``
# apontar para outro artefato.
REVISAO_M7 = "b1c4f2a90d37"
# Identificadores externos do lutador na fonte oficial acrescentados a ``fighters`` pelo M7
# (SPEC 008, Slice 03).
IDENTIFICADORES_OFICIAIS_FIGHTER = {"ufc_fighter_id", "ufc_mma_id"}
# Revisão da migration da Slice 03, também fixada por hash pelo mesmo motivo.
REVISAO_M7_FIGHTERS = "322648148188"


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

    Fixa a revisão-alvo (``ff6591f2d146``) em vez de ``head``: com a migration do M6
    empilhada acima, ``head`` deixou de ser a M5, e um ``-1`` a partir do head
    removeria os identificadores da Cito, não os artefatos do M5 -- mesmo ajuste que
    o teste de ``bout_features`` já recebeu quando a M5 empilhou sobre ele.
    """
    command.upgrade(alembic_cfg, "ff6591f2d146")
    assert TABELA_ROUNDS in _tabelas_existentes(migration_engine)

    command.downgrade(alembic_cfg, "-1")

    tabelas = _tabelas_existentes(migration_engine)
    assert TABELA_ROUNDS not in tabelas
    assert tabelas >= TABELAS
    assert "head_landed" not in _colunas(migration_engine, "bout_fighters")
    assert "referee" not in _colunas(migration_engine, "bouts")
    assert "weight_kg" not in _colunas(migration_engine, "fighters")
    assert "corner" in _tipos_enum_existentes(migration_engine)


def test_upgrade_cria_identificadores_cito_em_events(
    alembic_cfg: Config, migration_engine: Engine
) -> None:
    """CA-01: a migration do M6 cria ``cito_slug``/``cito_event_id`` em ``events``."""
    command.upgrade(alembic_cfg, "head")
    assert _colunas(migration_engine, "events") >= IDENTIFICADORES_CITO


def test_downgrade_um_passo_remove_identificadores_cito_preserva_events(
    alembic_cfg: Config, migration_engine: Engine
) -> None:
    """CA-01: o downgrade do M6 é simétrico -- dropa só as duas colunas aditivas.

    O restante de ``events`` (e as demais tabelas) permanece intacto: a migration é
    aditiva, então reverter não pode custar nenhum dado pré-existente.

    Fixa a revisão-alvo (``fd5a802eac7b``) em vez de ``head``: com a migration de
    ``bout_predictions`` empilhada acima, ``head`` deixou de ser a dos identificadores
    da Cito, e um ``-1`` a partir do head removeria a tabela de predições -- mesmo
    ajuste que os testes de ``bout_features`` e da M5 já receberam.
    """
    command.upgrade(alembic_cfg, "fd5a802eac7b")
    assert _colunas(migration_engine, "events") >= IDENTIFICADORES_CITO

    command.downgrade(alembic_cfg, "-1")

    colunas_events = _colunas(migration_engine, "events")
    assert not (IDENTIFICADORES_CITO & colunas_events)
    assert {"id", "name", "date", "location", "source"} <= colunas_events
    assert _tabelas_existentes(migration_engine) >= TABELAS | {TABELA_ROUNDS}


def test_upgrade_head_cria_bout_predictions(alembic_cfg: Config, migration_engine: Engine) -> None:
    """CA-02: ``alembic upgrade head`` cria a tabela ``bout_predictions``."""
    command.upgrade(alembic_cfg, "head")
    assert TABELA_PREDICOES in _tabelas_existentes(migration_engine)
    assert _colunas(migration_engine, TABELA_PREDICOES) >= {
        "bout_id",
        "predicted_winner_id",
        "prob_predicted_winner",
        "model_version",
        "n_features",
        "predicted_at",
        "source",
    }


def test_downgrade_um_passo_remove_bout_predictions_preserva_o_resto(
    alembic_cfg: Config, migration_engine: Engine
) -> None:
    """CA-02: o downgrade da Slice 07 dropa **só** ``bout_predictions``.

    A slice é inteiramente aditiva: reverter não pode custar o granular
    (``bouts``/``bout_fighters``), o round-a-round do M5, o cache derivado, os
    identificadores da Cito nem o enum ``corner`` (dono: ``bout_fighters``).

    Fixa a revisão-alvo (``c584e0a18606``) em vez de ``head``: com o contexto de card da
    Slice 05 empilhado acima, ``head`` deixou de ser a de ``bout_predictions``, e um ``-1``
    a partir do head removeria as colunas de card -- mesmo ajuste que os testes de
    ``bout_features``, da M5 e dos identificadores da Cito já receberam.
    """
    command.upgrade(alembic_cfg, "c584e0a18606")
    assert TABELA_PREDICOES in _tabelas_existentes(migration_engine)

    command.downgrade(alembic_cfg, "-1")

    tabelas = _tabelas_existentes(migration_engine)
    assert TABELA_PREDICOES not in tabelas
    assert tabelas >= TABELAS | {TABELA_ROUNDS, TABELA_DERIVADA}
    assert _colunas(migration_engine, "events") >= IDENTIFICADORES_CITO
    assert "corner" in _tipos_enum_existentes(migration_engine)


def test_upgrade_cria_contexto_de_card_em_bouts(
    alembic_cfg: Config, migration_engine: Engine
) -> None:
    """CA-06: a migration da Slice 05 cria ``card_section``/``bout_order`` em ``bouts``."""
    command.upgrade(alembic_cfg, "head")
    assert _colunas(migration_engine, "bouts") >= CONTEXTO_DE_CARD


def test_downgrade_um_passo_remove_contexto_de_card_preserva_bouts(
    alembic_cfg: Config, migration_engine: Engine
) -> None:
    """CA-06: o downgrade é simétrico -- dropa só as duas colunas aditivas de ``bouts``.

    A migration é aditiva no padrão da ADR 0004: nenhuma coluna pré-existente muda de tipo ou
    de nulidade, então reverter não pode custar nenhum dado já semeado (o contexto de luta do
    M5, o resultado, o granular por round, o cache derivado nem o enum ``corner``).

    Fixa a revisão-alvo (``3f1c38836a40``) em vez de ``head``, seguindo o precedente das
    migrations anteriores: assim uma migration futura empilhada acima não faz este ``-1``
    reverter o artefato errado.
    """
    command.upgrade(alembic_cfg, "3f1c38836a40")
    assert _colunas(migration_engine, "bouts") >= CONTEXTO_DE_CARD

    command.downgrade(alembic_cfg, "-1")

    colunas_bouts = _colunas(migration_engine, "bouts")
    assert not (CONTEXTO_DE_CARD & colunas_bouts)
    assert {"event_id", "winner_id", "method", "weight_class"} <= colunas_bouts
    assert {"title_bout", "scheduled_rounds", "referee"} <= colunas_bouts
    tabelas = _tabelas_existentes(migration_engine)
    assert tabelas >= TABELAS | {TABELA_ROUNDS, TABELA_DERIVADA, TABELA_PREDICOES}
    assert "corner" in _tipos_enum_existentes(migration_engine)


def test_upgrade_cria_ufc_event_id_em_events(alembic_cfg: Config, migration_engine: Engine) -> None:
    """CA-01: a migration do M7 cria ``events.ufc_event_id``."""
    command.upgrade(alembic_cfg, "head")
    assert IDENTIFICADOR_OFICIAL in _colunas(migration_engine, "events")


def test_downgrade_um_passo_remove_ufc_event_id_preserva_identificadores_cito(
    alembic_cfg: Config, migration_engine: Engine
) -> None:
    """CA-01: o downgrade do M7 é simétrico -- dropa só a coluna aditiva de ``events``.

    Os identificadores da Cito (M6), o restante de ``events``, as demais tabelas e o enum
    ``corner`` permanecem intactos: a migration é aditiva, então reverter não pode custar
    nenhum dado pré-existente.

    Fixa a revisão-alvo (``REVISAO_M7``) em vez de ``head``, seguindo o precedente das
    migrations anteriores: assim uma migration futura empilhada acima não faz este ``-1``
    reverter o artefato errado.
    """
    command.upgrade(alembic_cfg, REVISAO_M7)
    assert IDENTIFICADOR_OFICIAL in _colunas(migration_engine, "events")

    command.downgrade(alembic_cfg, "-1")

    colunas_events = _colunas(migration_engine, "events")
    assert IDENTIFICADOR_OFICIAL not in colunas_events
    assert colunas_events >= IDENTIFICADORES_CITO
    assert {"id", "name", "date", "location", "source"} <= colunas_events
    tabelas = _tabelas_existentes(migration_engine)
    assert tabelas >= TABELAS | {TABELA_ROUNDS, TABELA_DERIVADA, TABELA_PREDICOES}
    assert "corner" in _tipos_enum_existentes(migration_engine)


def test_upgrade_cria_identificadores_da_fonte_oficial_em_fighters(
    alembic_cfg: Config, migration_engine: Engine
) -> None:
    """CA-01: a migration da Slice 03 cria ``fighters.ufc_fighter_id``/``ufc_mma_id``."""
    command.upgrade(alembic_cfg, "head")
    assert _colunas(migration_engine, "fighters") >= IDENTIFICADORES_OFICIAIS_FIGHTER


def test_downgrade_um_passo_remove_identificadores_de_fighters_preserva_o_resto(
    alembic_cfg: Config, migration_engine: Engine
) -> None:
    """CA-01: o downgrade da Slice 03 dropa só as duas colunas aditivas de ``fighters``.

    O identificador do evento (Slice 02), o restante de ``fighters``, as demais tabelas e o
    enum ``corner`` permanecem intactos: a migration é aditiva, então reverter não pode custar
    nenhum dado pré-existente -- nem o cartel semeado, nem a antropometria do Kaggle.

    Fixa a revisão-alvo (``REVISAO_M7_FIGHTERS``) em vez de ``head``, seguindo o precedente das
    migrations anteriores: assim uma migration futura empilhada acima não faz este ``-1``
    reverter o artefato errado.
    """
    command.upgrade(alembic_cfg, REVISAO_M7_FIGHTERS)
    assert _colunas(migration_engine, "fighters") >= IDENTIFICADORES_OFICIAIS_FIGHTER

    command.downgrade(alembic_cfg, "-1")

    colunas_fighters = _colunas(migration_engine, "fighters")
    assert not (IDENTIFICADORES_OFICIAIS_FIGHTER & colunas_fighters)
    assert {"id", "name", "name_normalized", "date_of_birth", "source"} <= colunas_fighters
    assert {"height_cm", "reach_cm", "stance", "weight_kg"} <= colunas_fighters
    assert {"wins", "losses", "draws"} <= colunas_fighters
    assert IDENTIFICADOR_OFICIAL in _colunas(migration_engine, "events")
    tabelas = _tabelas_existentes(migration_engine)
    assert tabelas >= TABELAS | {TABELA_ROUNDS, TABELA_DERIVADA, TABELA_PREDICOES}
    assert "corner" in _tipos_enum_existentes(migration_engine)
