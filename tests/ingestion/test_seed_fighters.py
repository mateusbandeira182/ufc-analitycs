"""Testes da carga idempotente de fighters -- CA-02, CA-04, CA-05.

Cobrem a aquisição por caminho local determinístico (CI, sem credencial), a
gravação de ``source="kaggle"`` em toda escrita, a dedup por nome normalizado
(Volkanovski não duplica; homônimos com DOB distinta permanecem) e a idempotência
(rodar a carga duas vezes mantém a contagem). Rodam contra o Postgres de teste
``ufc_bum_test`` na sessão transacional (rollback ao final).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.fighters.enums import Stance
from apps.fighters.models import Fighter
from ingestion.seed_fighters import seed_fighters
from ingestion.sources.kaggle import load_fighter_details

_FIXTURE = Path(__file__).parent / "fixtures" / "fighter_details_sample.csv"

# A fixture tem 6 linhas; Volkanovski aparece 2x (colapsa) -> 5 lutadores únicos.
_UNIQUE_FIGHTERS = 5


def test_aquisicao_por_caminho_local_retorna_dataframe_valido() -> None:
    """CA-02: a aquisição lê o CSV local e expõe as colunas reais do dataset."""
    frame = load_fighter_details(_FIXTURE)
    assert len(frame) == 6
    assert {"name", "nick_name", "dob", "height", "reach", "stance"} <= set(frame.columns)


def test_carga_popula_fighters_deduplicando(db_session: Session) -> None:
    """CA-03/CA-05: a carga insere um lutador por chave natural; Volkanovski não duplica."""
    inserted = seed_fighters(db_session, _FIXTURE)
    assert inserted == _UNIQUE_FIGHTERS

    total = db_session.scalar(select(func.count()).select_from(Fighter))
    assert total == _UNIQUE_FIGHTERS

    volks = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == "alexander volkanovski")
    ).all()
    assert len(volks) == 1


def test_homonimos_com_dob_distinta_sao_dois_registros(db_session: Session) -> None:
    """CA-03: dois 'Bruno Silva' com DOB diferente permanecem separados no banco."""
    seed_fighters(db_session, _FIXTURE)
    silvas = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == "bruno silva")
    ).all()
    assert len(silvas) == 2
    assert {s.date_of_birth for s in silvas} == {date(1989, 7, 13), date(1990, 3, 16)}


def test_toda_escrita_grava_source_kaggle(db_session: Session) -> None:
    """CA-04: nenhum lutador é gravado sem ``source="kaggle"``."""
    seed_fighters(db_session, _FIXTURE)
    sources = set(db_session.scalars(select(Fighter.source)).all())
    assert sources == {"kaggle"}


def test_carga_e_idempotente(db_session: Session) -> None:
    """CA-05: a segunda execução não insere nada nem altera a contagem."""
    first = seed_fighters(db_session, _FIXTURE)
    total_after_first = db_session.scalar(select(func.count()).select_from(Fighter))

    second = seed_fighters(db_session, _FIXTURE)
    total_after_second = db_session.scalar(select(func.count()).select_from(Fighter))

    assert first == _UNIQUE_FIGHTERS
    assert second == 0
    assert total_after_first == total_after_second == _UNIQUE_FIGHTERS


def test_atributos_mapeados_da_borda(db_session: Session) -> None:
    """Os campos do CSV chegam tipados ao banco (medidas em cm, stance enum, DOB)."""
    seed_fighters(db_session, _FIXTURE)
    (volk,) = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == "alexander volkanovski")
    ).all()
    assert volk.date_of_birth == date(1982, 5, 8)
    assert volk.height_cm == 168
    assert volk.reach_cm == 183
    assert volk.stance is Stance.ORTHODOX
    assert volk.nickname == "The Great"
    assert (volk.wins, volk.losses, volk.draws) == (26, 4, 0)

    # Stance fora do enum (Open Stance) e medidas ausentes -> NULL.
    (jan,) = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == "jan blachowicz")
    ).all()
    assert jan.stance is None
    assert jan.reach_cm is None

    # DOB ausente -> NULL (não bloqueia a carga).
    (ghost,) = db_session.scalars(
        select(Fighter).where(Fighter.name_normalized == "ghost prospect")
    ).all()
    assert ghost.date_of_birth is None
    assert ghost.height_cm is None
