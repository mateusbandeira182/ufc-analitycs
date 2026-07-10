"""Testes do seed orquestrado ponta-a-ponta -- CA-01 a CA-05 do Plano 002-05.

Cobrem a orquestração da carga completa (``fighters`` -> ``events`` -> ``bouts``/
``bout_fighters``) num único comando, a idempotência do seed **inteiro** (rodar duas
vezes deixa as quatro contagens estáveis e re-resolve as FKs para os mesmos ids), a
gravação de ``source="kaggle"`` em toda escrita e a configuração da origem do dataset
sem editar código (``resolve_dataset_dir``). Os testes de carga rodam contra o Postgres
de teste ``ufc_bum_test`` na sessão transacional (rollback ao final); a dupla execução
ocorre na mesma transação, onde as linhas já inseridas (ainda não commitadas) são
visíveis à segunda carga -- é o que valida a dedup por chave natural ponta-a-ponta.

A fonte determinística é o dataset de amostra em ``fixtures/seed_sample/`` (três CSVs
que espelham o dataset real da ADR 0002): quatro lutadores, dois eventos e três lutas.
A luta do segundo evento coloca Ilia Topuria e Jon Jones em cantos opostos aos das
lutas do primeiro evento -- exercita a re-resolução da FK do mesmo lutador para o mesmo
``fighter_id`` em lutas distintas.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.seed import SeedCounts, resolve_dataset_dir, run_seed

_SAMPLE_DIR = Path(__file__).parent / "fixtures" / "seed_sample"

# Contagens esperadas do dataset de amostra (quatro lutadores, dois eventos, três lutas).
_EXPECTED = SeedCounts(fighters=4, events=2, bouts=3, bout_fighters=6)


def _bout_fk_ids(session: Session) -> set[tuple[int, int]]:
    """Conjunto dos pares ``(event_id, fighter_id)`` referenciados pelas lutas persistidas."""
    return {
        (event_id, fighter_id)
        for event_id, fighter_id in session.execute(
            select(Bout.event_id, BoutFighter.fighter_id).join(
                BoutFighter, BoutFighter.bout_id == Bout.id
            )
        )
    }


def test_run_seed_popula_quatro_tabelas(db_session: Session) -> None:
    """CA-01: a carga orquestrada a partir de banco vazio popula as quatro tabelas na ordem."""
    counts = run_seed(db_session, _SAMPLE_DIR)

    assert counts == _EXPECTED
    assert db_session.scalar(select(func.count()).select_from(Fighter)) == _EXPECTED.fighters
    assert db_session.scalar(select(func.count()).select_from(Event)) == _EXPECTED.events
    assert db_session.scalar(select(func.count()).select_from(Bout)) == _EXPECTED.bouts
    assert (
        db_session.scalar(select(func.count()).select_from(BoutFighter)) == _EXPECTED.bout_fighters
    )


def test_run_seed_idempotente(db_session: Session) -> None:
    """CA-02/CA-03: a reexecução mantém as contagens e re-resolve as FKs para os mesmos ids."""
    first = run_seed(db_session, _SAMPLE_DIR)
    fks_after_first = _bout_fk_ids(db_session)

    second = run_seed(db_session, _SAMPLE_DIR)
    fks_after_second = _bout_fk_ids(db_session)

    assert first == _EXPECTED
    assert second == first  # nenhuma das quatro contagens muda na reexecução
    assert fks_after_second == fks_after_first  # nenhum id novo; FKs re-resolvidas para os mesmos

    # Cada luta continua com exatamente duas linhas em bout_fighters (uma por canto).
    rows_por_bout = db_session.execute(
        select(BoutFighter.bout_id, func.count()).group_by(BoutFighter.bout_id)
    ).all()
    assert len(rows_por_bout) == _EXPECTED.bouts
    assert all(n == 2 for _, n in rows_por_bout)


def test_run_seed_grava_source_kaggle(db_session: Session) -> None:
    """CA-05: toda linha das quatro tabelas da carga orquestrada tem ``source="kaggle"``."""
    run_seed(db_session, _SAMPLE_DIR)

    assert set(db_session.scalars(select(Fighter.source)).all()) == {"kaggle"}
    assert set(db_session.scalars(select(Event.source)).all()) == {"kaggle"}
    assert set(db_session.scalars(select(Bout.source)).all()) == {"kaggle"}
    assert set(db_session.scalars(select(BoutFighter.source)).all()) == {"kaggle"}


def test_resolve_dataset_dir_prioriza_cli_sobre_env() -> None:
    """CA-04: o caminho vindo da CLI vence a variável de ambiente."""
    resolved = resolve_dataset_dir(cli_path=Path("/x"), env={"SEED_DATASET_DIR": "/y"})
    assert resolved == Path("/x")


def test_resolve_dataset_dir_cai_para_env() -> None:
    """CA-04: sem CLI, a origem vem da variável de ambiente ``SEED_DATASET_DIR``."""
    resolved = resolve_dataset_dir(cli_path=None, env={"SEED_DATASET_DIR": "/data/ufc"})
    assert resolved == Path("/data/ufc")


def test_resolve_dataset_dir_sem_config_retorna_none() -> None:
    """CA-04: sem CLI nem env, retorna ``None`` -- dispara a aquisição programática (kagglehub)."""
    assert resolve_dataset_dir(cli_path=None, env={}) is None
