"""Testes do backfill de ``fighters.weight_kg`` a partir do CSV do seed -- CA-03 a CA-06.

O M5 criou a coluna, o schema Pydantic e os testes de API de ``weight_kg``, mas nenhuma
escrita: a base ficou 100% nula porque a coluna ``weight`` nunca entrou na projeção do seed.
Estes testes cobrem o fluxo que fecha essa ponta -- CSV -> seed -> backfill -> ``GET
/api/v1/fighters/{id}`` --, a idempotência do UPDATE (segunda execução devolve ``updated=0``),
a garantia de que o backfill nunca insere linha e o parser de argumentos do CLI.

Rodam contra o Postgres de teste ``ufc_bum_test`` na sessão transacional (rollback ao final);
zero chamadas à Cito.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from apps.fighters.models import Fighter
from ingestion.backfill_weight import _parse_backfill_args
from ingestion.seed_fighters import backfill_fighter_weight, seed_fighters
from ingestion.sources.kaggle import load_fighter_details

_FIXTURE = Path(__file__).parent / "fixtures" / "fighter_details_sample.csv"


def _seed_sem_peso(session: Session) -> None:
    """Semeia a fixture e zera ``weight_kg``, reproduzindo a base herdada do M5."""
    seed_fighters(session, _FIXTURE)
    session.execute(update(Fighter).values(weight_kg=None))
    session.flush()


def _contagem_com_peso(session: Session) -> int | None:
    """Quantos lutadores têm ``weight_kg`` não-nulo neste instante."""
    return session.scalar(
        select(func.count()).select_from(Fighter).where(Fighter.weight_kg.is_not(None))
    )


def test_backfill_preenche_weight_kg_ate_a_api(client: TestClient, db_session: Session) -> None:
    """CA-06: base semeada sem peso -> backfill -> o detalhe da API expõe ``weight_kg``."""
    _seed_sem_peso(db_session)

    backfill_fighter_weight(db_session, load_fighter_details(_FIXTURE))

    volk = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == "alexander volkanovski")
    ).one()
    assert client.get(f"/api/v1/fighters/{volk.id}").json()["weight_kg"] == 65.77


def test_backfill_mantem_nulo_quem_nao_tem_peso_no_csv(
    client: TestClient, db_session: Session
) -> None:
    """CA-06: lutador sem peso no CSV continua com ``weight_kg`` nulo na API (nunca zero)."""
    _seed_sem_peso(db_session)

    backfill_fighter_weight(db_session, load_fighter_details(_FIXTURE))

    ghost = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == "ghost prospect")
    ).one()
    assert client.get(f"/api/v1/fighters/{ghost.id}").json()["weight_kg"] is None


def test_backfill_preenche_por_chave_natural(db_session: Session) -> None:
    """CA-03: os lutadores casados por ``(name_normalized, date_of_birth)`` recebem o peso."""
    _seed_sem_peso(db_session)

    resultado = backfill_fighter_weight(db_session, load_fighter_details(_FIXTURE))

    pesos = {
        (fighter.name_normalized, fighter.date_of_birth): fighter.weight_kg
        for fighter in db_session.scalars(select(Fighter))
    }
    assert pesos == {
        ("alexander volkanovski", date(1982, 5, 8)): 65.77,
        ("bruno silva", date(1989, 7, 13)): 83.91,
        ("bruno silva", date(1990, 3, 16)): 56.7,
        ("jan blachowicz", date(1983, 2, 24)): 93.0,
        ("ghost prospect", None): None,
    }
    assert resultado.updated == 4


def test_backfill_conta_lutador_sem_peso_no_csv(db_session: Session) -> None:
    """CA-03: linha do CSV sem peso não escreve nada e é contada em ``without_weight``."""
    _seed_sem_peso(db_session)

    resultado = backfill_fighter_weight(db_session, load_fighter_details(_FIXTURE))

    ghost = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == "ghost prospect")
    ).one()
    assert ghost.weight_kg is None
    assert resultado.without_weight == 1


def test_backfill_nunca_insere_lutador_ausente(db_session: Session) -> None:
    """CA-03: linha do CSV sem fighter persistido é pulada, jamais criada (UPDATE puro)."""
    seed_fighters(db_session, _FIXTURE)
    db_session.execute(
        update(Fighter)
        .where(Fighter.name_normalized == "jan blachowicz")
        .values(weight_kg=None, name_normalized="outro nome qualquer")
    )
    db_session.flush()
    total_antes = db_session.scalar(select(func.count()).select_from(Fighter))

    resultado = backfill_fighter_weight(db_session, load_fighter_details(_FIXTURE))

    assert resultado.skipped == 1
    assert db_session.scalar(select(func.count()).select_from(Fighter)) == total_antes


def test_backfill_e_idempotente(db_session: Session) -> None:
    """CA-04: a segunda execução devolve ``updated=0`` e não altera a contagem preenchida."""
    _seed_sem_peso(db_session)

    primeiro = backfill_fighter_weight(db_session, load_fighter_details(_FIXTURE))
    preenchidos = _contagem_com_peso(db_session)
    segundo = backfill_fighter_weight(db_session, load_fighter_details(_FIXTURE))

    assert primeiro.updated > 0
    assert segundo.updated == 0
    assert _contagem_com_peso(db_session) == preenchidos


def test_backfill_nao_sobrescreve_peso_ja_gravado(db_session: Session) -> None:
    """CA-04: valor já persistido é preservado -- o backfill só toca ``weight_kg`` nulo."""
    _seed_sem_peso(db_session)
    db_session.execute(
        update(Fighter)
        .where(Fighter.name_normalized == "alexander volkanovski")
        .values(weight_kg=100.0)
    )
    db_session.flush()

    backfill_fighter_weight(db_session, load_fighter_details(_FIXTURE))

    volk = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == "alexander volkanovski")
    ).one()
    assert volk.weight_kg == 100.0


def test_parse_backfill_args_le_dataset_dir() -> None:
    """``--dataset-dir`` é parseado como ``Path``."""
    args = _parse_backfill_args(["--dataset-dir", "/data/ufc"])
    assert args.dataset_dir == Path("/data/ufc")


def test_parse_backfill_args_dataset_dir_default_none() -> None:
    """Sem ``--dataset-dir`` o valor é ``None`` (sinaliza aquisição via kagglehub)."""
    args = _parse_backfill_args([])
    assert args.dataset_dir is None
