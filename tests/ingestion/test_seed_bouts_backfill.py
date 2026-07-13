"""Testes do backfill de splits totais + contexto do CSV do seed (Plano M5-02, CA-01 a CA-05).

Cobrem, em duas camadas:

- **Parsers puros** (sem I/O): ``build_bout_fighter_splits`` extrai os 7 grupos de split por
  canto (landed/attempted) das colunas ``r_*``/``b_*`` do ``fight_details.csv``; ``reversals``
  **nao** vem do CSV do seed (permanece fora do dicionario -- virá da Cito na Slice 05).
  ``map_bout_context`` lê ``title_bout``/``scheduled_rounds``/``referee``, degradando ausência
  para ``None`` de forma explícita.
- **Backfill contra Postgres real**: ``backfill_splits_and_context`` faz **UPDATE** (nunca
  INSERT) nas linhas de ``bout_fighters``/``bouts`` já persistidas pela chave natural (evento +
  par não-ordenado de fighter_ids), grava/preserva ``source="kaggle"`` e é idempotente -- rodar
  de novo mantém contagem e conteúdo. 0 chamadas Cito.

Os testes de backfill rodam contra o Postgres de teste ``ufc_bum_test`` na sessão transacional
(rollback ao final), reusando os helpers de apoio do seed (``_add_fighter``/``_add_event``/
``_seed_support``).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.seed_bouts import (
    BackfillResult,
    backfill_splits_and_context,
    build_bout_fighter_splits,
    load_bouts,
    map_bout_context,
)
from ingestion.sources.kaggle import load_event_details, load_fight_details
from tests.ingestion.test_seed_bouts import (
    _EVENT_DETAILS,
    _FIGHT_DETAILS,
    _TOTAL_BOUT_FIGHTERS,
    _TOTAL_BOUTS,
    _seed_support,
)

# Linha crua do canto red com os 7 grupos de split (texto, como o Pandas lê o CSV).
_SPLIT_ROW: dict[str, str] = {
    "r_total_str_landed": "37.0",
    "r_total_str_atmpted": "61.0",
    "r_head_landed": "9.0",
    "r_head_atmpted": "26.0",
    "r_body_landed": "8.0",
    "r_body_atmpted": "12.0",
    "r_leg_landed": "0.0",
    "r_leg_atmpted": "0.0",
    "r_dist_landed": "9.0",
    "r_dist_atmpted": "26.0",
    "r_clinch_landed": "3.0",
    "r_clinch_atmpted": "3.0",
    "r_ground_landed": "5.0",
    "r_ground_atmpted": "9.0",
    "b_total_str_landed": "60.0",
    "b_total_str_atmpted": "80.0",
    "b_head_landed": "40.0",
    "b_head_atmpted": "55.0",
    "b_body_landed": "10.0",
    "b_body_atmpted": "14.0",
    "b_leg_landed": "2.0",
    "b_leg_atmpted": "3.0",
    "b_dist_landed": "50.0",
    "b_dist_atmpted": "70.0",
    "b_clinch_landed": "6.0",
    "b_clinch_atmpted": "7.0",
    "b_ground_landed": "4.0",
    "b_ground_atmpted": "5.0",
}


# --- PASSO-01: parser de splits por canto (puro) ------------------------------------------


def test_build_bout_fighter_splits_extrai_grupos_do_canto_red() -> None:
    """CA-01: o canto red recebe os 7 grupos de split (landed/attempted) das colunas ``r_*``."""
    splits = build_bout_fighter_splits(_SPLIT_ROW, Corner.RED)
    assert splits["total_strikes_landed"] == 37
    assert splits["total_strikes_attempted"] == 61
    assert splits["head_landed"] == 9
    assert splits["head_attempted"] == 26
    assert splits["body_landed"] == 8
    assert splits["body_attempted"] == 12
    assert splits["leg_landed"] == 0
    assert splits["leg_attempted"] == 0
    assert splits["distance_landed"] == 9
    assert splits["distance_attempted"] == 26
    assert splits["clinch_landed"] == 3
    assert splits["clinch_attempted"] == 3
    assert splits["ground_landed"] == 5
    assert splits["ground_attempted"] == 9


def test_build_bout_fighter_splits_canto_blue_e_distinto() -> None:
    """CA-01: o canto blue lê as colunas ``b_*``, distintas do red (granular, nunca média)."""
    splits = build_bout_fighter_splits(_SPLIT_ROW, Corner.BLUE)
    assert splits["distance_landed"] == 50
    assert splits["head_landed"] == 40
    assert splits["total_strikes_attempted"] == 80


def test_build_bout_fighter_splits_ausente_vira_none() -> None:
    """CA-01: valor de split ausente (após trim) degrada para ``None``."""
    row = {**_SPLIT_ROW, "r_dist_landed": "", "r_head_atmpted": ""}
    splits = build_bout_fighter_splits(row, Corner.RED)
    assert splits["distance_landed"] is None
    assert splits["head_attempted"] is None


def test_build_bout_fighter_splits_malformado_vira_none() -> None:
    """CA-01: valor de split não numérico (célula malformada) degrada para ``None``.

    Diferente da ausência (``""``), uma célula não-parseável do CSV não deve abortar o backfill
    inteiro com ``ValueError``: degrada graciosamente para ``None`` (simétrico ao parser booleano),
    enquanto os demais grupos do mesmo canto seguem parseando normalmente.
    """
    row = {**_SPLIT_ROW, "r_dist_landed": "abc"}
    splits = build_bout_fighter_splits(row, Corner.RED)
    assert splits["distance_landed"] is None
    assert splits["head_landed"] == 9
    assert splits["total_strikes_landed"] == 37


def test_build_bout_fighter_splits_nao_inclui_reversals() -> None:
    """CA-01: ``reversals`` nao existe no ``fight_details.csv`` -- fica fora do dicionário."""
    splits = build_bout_fighter_splits(_SPLIT_ROW, Corner.RED)
    assert "reversals" not in splits


# --- PASSO-03: parser de contexto da luta (puro) ------------------------------------------


def test_map_bout_context_le_title_rounds_referee() -> None:
    """CA-02: title fight, rounds agendados e árbitro presentes são tipados na borda."""
    ctx = map_bout_context({"title_fight": "1", "total_rounds": "5.0", "referee": "Marc Goddard"})
    assert ctx["title_bout"] is True
    assert ctx["scheduled_rounds"] == 5
    assert ctx["referee"] == "Marc Goddard"


def test_map_bout_context_title_fight_falso() -> None:
    """CA-02: ``title_fight`` ``"0"``/``"0.0"`` vira ``False`` (não ``None``)."""
    assert (
        map_bout_context({"title_fight": "0", "total_rounds": "3", "referee": "X"})["title_bout"]
        is False
    )
    assert (
        map_bout_context({"title_fight": "0.0", "total_rounds": "3", "referee": "X"})["title_bout"]
        is False
    )


def test_map_bout_context_ausente_vira_none() -> None:
    """CA-02: ausência de title/rounds/referee degrada para ``None`` de forma explícita."""
    ctx = map_bout_context({"title_fight": "", "total_rounds": "", "referee": ""})
    assert ctx["title_bout"] is None
    assert ctx["scheduled_rounds"] is None
    assert ctx["referee"] is None


# --- PASSO-05/07: backfill contra o Postgres real -----------------------------------------


def _fixture_frames() -> tuple[object, object]:
    return load_fight_details(_FIGHT_DETAILS), load_event_details(_EVENT_DETAILS)


def _seed_and_load(session: Session) -> None:
    """Semeia fighters/events de apoio e carrega bouts/bout_fighters (splits ainda ``NULL``)."""
    _seed_support(session)
    fight_details, event_details = _fixture_frames()
    load_bouts(session, fight_details, event_details)  # type: ignore[arg-type]


def _run_backfill(session: Session) -> BackfillResult:
    fight_details, event_details = _fixture_frames()
    return backfill_splits_and_context(session, fight_details, event_details)  # type: ignore[arg-type]


def _volk_line(session: Session) -> BoutFighter:
    (volk_id,) = session.scalars(
        select(Fighter.id).where(Fighter.name_normalized == "alexander volkanovski")
    ).all()
    return session.scalars(select(BoutFighter).where(BoutFighter.fighter_id == volk_id)).one()


def _topu_line(session: Session) -> BoutFighter:
    (topu_id,) = session.scalars(
        select(Fighter.id).where(Fighter.name_normalized == "ilia topuria")
    ).all()
    return session.scalars(select(BoutFighter).where(BoutFighter.fighter_id == topu_id)).one()


def test_backfill_preenche_splits_e_contexto(db_session: Session) -> None:
    """CA-03: UPDATE preenche splits + contexto sem criar linha; ``source="kaggle"``."""
    _seed_and_load(db_session)
    bouts_before = db_session.scalar(select(func.count()).select_from(Bout))
    bf_before = db_session.scalar(select(func.count()).select_from(BoutFighter))

    result = _run_backfill(db_session)

    # Nenhuma linha nova criada (UPDATE, nunca INSERT).
    assert db_session.scalar(select(func.count()).select_from(Bout)) == bouts_before
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == bf_before
    assert result.skipped == 0

    volk = _volk_line(db_session)
    assert volk.distance_landed == 9
    assert volk.head_attempted == 26
    assert volk.total_strikes_landed == 37
    assert volk.reversals is None  # sem coluna no CSV do seed
    assert volk.source == "kaggle"

    # Blue distinto do red -- granular por luta, nunca média.
    topu = _topu_line(db_session)
    assert topu.distance_landed == 50
    assert topu.head_landed == 40

    (evt_id,) = db_session.scalars(select(Event.id).where(Event.name == "UFC 300: Test")).all()
    bout = db_session.scalars(
        select(Bout).where(Bout.event_id == evt_id, Bout.id == volk.bout_id)
    ).one()
    assert bout.title_bout is False
    assert bout.scheduled_rounds == 3
    assert bout.referee == "Herb Dean"
    assert bout.source == "kaggle"


def test_backfill_luta_ausente_e_pulada_sem_criar_linha(db_session: Session) -> None:
    """CA-03: sem o seed de bouts prévio, o backfill pula tudo (nao cria linha)."""
    _seed_support(db_session)  # fighters/events, mas nenhum bout carregado

    result = _run_backfill(db_session)

    assert result.bouts_updated == 0
    assert result.bout_fighters_updated == 0
    assert result.skipped == _TOTAL_BOUTS
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 0
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == 0


def _snapshot_splits(session: Session) -> list[tuple[int, int | None, int | None, str]]:
    """Amostra estável (ordenada) dos splits por linha, para comparar entre execuções."""
    return [
        (bf.fighter_id, bf.distance_landed, bf.head_attempted, bf.source)
        for bf in session.scalars(select(BoutFighter).order_by(BoutFighter.fighter_id)).all()
    ]


def test_backfill_e_idempotente(db_session: Session) -> None:
    """CA-04: rodar o backfill duas vezes mantém contagem e conteúdo (idempotência)."""
    _seed_and_load(db_session)
    _run_backfill(db_session)
    bouts_before = db_session.scalar(select(func.count()).select_from(Bout))
    bf_before = db_session.scalar(select(func.count()).select_from(BoutFighter))
    sample = _snapshot_splits(db_session)

    _run_backfill(db_session)

    assert db_session.scalar(select(func.count()).select_from(Bout)) == bouts_before
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == bf_before
    assert _snapshot_splits(db_session) == sample
    # Sanidade: as contagens batem com o total da fixture (nada foi inserido pelo backfill).
    assert bouts_before == _TOTAL_BOUTS
    assert bf_before == _TOTAL_BOUT_FIGHTERS
