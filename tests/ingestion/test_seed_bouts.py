"""Testes do seed de bouts + bout_fighters -- CA-01 a CA-07 do Plano 002-04.

Cobrem o mapeamento puro do core da luta (método/round/tempo/weight_class/vencedor),
a explosão wide->long em exatamente duas linhas de ``bout_fighters`` com as stats
granulares por canto (ADR 0001), a resolução de FKs contra fighters/events já semeados
(reuso da entity resolution da Slice 02 e do seed de events da Slice 03), a carga
idempotente por chave natural e o tratamento de lutas cujo lutador/evento não resolve
(log + skip, sem inserir órfão). Os testes de carga rodam contra o Postgres de teste
``ufc_bum_test`` na sessão transacional (rollback ao final).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.normalize import normalize_name
from ingestion.seed_bouts import (
    BoutLoadResult,
    build_bout_fighter,
    build_event_index,
    build_fighter_index,
    load_bouts,
    map_bout_core,
    resolve_bout_fks,
    seed_bouts,
)
from ingestion.sources.kaggle import load_event_details, load_fight_details

_FIXTURES = Path(__file__).parent / "fixtures"
_FIGHT_DETAILS = _FIXTURES / "bouts_fight_details_sample.csv"
_EVENT_DETAILS = _FIXTURES / "bouts_event_details_sample.csv"

# A fixture tem 4 lutas em 2 eventos, todas com FKs resolviveis apos o seed de apoio.
_TOTAL_BOUTS = 4
_TOTAL_BOUT_FIGHTERS = 8

# Colunas consumidas de ``fight_details.csv`` (valores como texto, como o Pandas lê o CSV).
_DEFAULT_ROW: dict[str, str] = {
    "event_name": "UFC 300: Test",
    "event_id": "evt300",
    "fight_id": "f1",
    "r_name": "Alexander Volkanovski",
    "b_name": "Ilia Topuria",
    "division": "featherweight",
    "method": "KO/TKO",
    "finish_round": "2",
    "match_time_sec": "180",
    "date": "April 13, 2024",
    "winner": "Ilia Topuria",
    "r_kd": "0.0",
    "r_sig_str_landed": "17.0",
    "r_sig_str_atmpted": "38.0",
    "r_td_landed": "3.0",
    "r_td_atmpted": "10.0",
    "r_sub_att": "1.0",
    "r_ctrl": "278.0",
    "b_kd": "1.0",
    "b_sig_str_landed": "52.0",
    "b_sig_str_atmpted": "60.0",
    "b_td_landed": "0.0",
    "b_td_atmpted": "0.0",
    "b_sub_att": "0.0",
    "b_ctrl": "34.0",
}


def _row(**overrides: str) -> dict[str, str]:
    """Devolve uma linha crua de fight_details com os campos sobrescritos."""
    return {**_DEFAULT_ROW, **overrides}


def test_map_bout_core_mapeia_ko_tko() -> None:
    """CA-01: 'KO/TKO' vira ``BoutMethod.KO_TKO`` com round/tempo/weight_class tipados."""
    core = map_bout_core(_row(method="KO/TKO", finish_round="2", match_time_sec="180"))
    assert core["method"] is BoutMethod.KO_TKO
    assert core["round"] == 2
    assert core["ending_time_seconds"] == 180
    assert core["weight_class"] == "featherweight"


def test_map_bout_core_mapeia_submissao() -> None:
    """CA-01: 'Submission' vira ``BoutMethod.SUBMISSION``."""
    assert map_bout_core(_row(method="Submission"))["method"] is BoutMethod.SUBMISSION


def test_map_bout_core_mapeia_decisoes() -> None:
    """CA-01: as tres decisoes (unanime/dividida/majoritaria) viram ``DECISION``."""
    for token in ("Decision - Unanimous", "Decision - Split", "Decision - Majority"):
        assert map_bout_core(_row(method=token))["method"] is BoutMethod.DECISION


def test_map_bout_core_mapeia_tko_medico_como_ko_tko() -> None:
    """CA-01: 'TKO - Doctor's Stoppage' agrupa em ``KO_TKO``."""
    assert map_bout_core(_row(method="TKO - Doctor's Stoppage"))["method"] is BoutMethod.KO_TKO


def test_map_bout_core_mapeia_dq() -> None:
    """CA-01: 'DQ' vira ``BoutMethod.DQ`` (com vencedor presente)."""
    core = map_bout_core(_row(method="DQ", winner="Alexander Volkanovski"))
    assert core["method"] is BoutMethod.DQ
    assert core["winner_corner"] is Corner.RED


def test_map_bout_core_no_contest_zera_vencedor() -> None:
    """CA-01: 'Overturned'/'Could Not Continue'/'Other' viram ``NO_CONTEST`` sem vencedor."""
    for token in ("Overturned", "Could Not Continue", "Other"):
        core = map_bout_core(_row(method=token, winner=""))
        assert core["method"] is BoutMethod.NO_CONTEST
        assert core["winner_corner"] is None


def test_map_bout_core_vencedor_no_canto_correto() -> None:
    """CA-01: o vencedor casa com o canto pelo nome normalizado (red ou blue)."""
    assert map_bout_core(_row(winner="Ilia Topuria"))["winner_corner"] is Corner.BLUE
    assert map_bout_core(_row(winner="Alexander Volkanovski"))["winner_corner"] is Corner.RED


def test_map_bout_core_empate_tem_vencedor_nulo() -> None:
    """CA-01: decisao sem vencedor (empate) mantem o metodo e zera o vencedor."""
    core = map_bout_core(_row(method="Decision - Majority", winner=""))
    assert core["method"] is BoutMethod.DECISION
    assert core["winner_corner"] is None


def test_map_bout_core_round_e_tempo_ausentes_viram_none() -> None:
    """CA-01/CA-06: round/tempo/weight_class ausentes viram ``None``."""
    core = map_bout_core(_row(finish_round="", match_time_sec="", division=""))
    assert core["round"] is None
    assert core["ending_time_seconds"] is None
    assert core["weight_class"] is None


def test_build_bout_fighter_extrai_stats_do_canto_red() -> None:
    """CA-03: o canto red recebe as stats ``r_*`` granulares e ``source="kaggle"``."""
    red = build_bout_fighter(_row(), Corner.RED, fighter_id=1, bout_id=10)
    assert red.corner is Corner.RED
    assert red.fighter_id == 1
    assert red.bout_id == 10
    assert red.knockdowns == 0
    assert red.sig_strikes_landed == 17
    assert red.sig_strikes_attempted == 38
    assert red.takedowns_landed == 3
    assert red.takedowns_attempted == 10
    assert red.submission_attempts == 1
    assert red.control_time_seconds == 278
    assert red.source == "kaggle"


def test_build_bout_fighter_extrai_stats_do_canto_blue() -> None:
    """CA-03: o canto blue recebe as stats ``b_*``, distintas do red (nao e media)."""
    blue = build_bout_fighter(_row(), Corner.BLUE, fighter_id=2, bout_id=10)
    assert blue.corner is Corner.BLUE
    assert blue.knockdowns == 1
    assert blue.sig_strikes_landed == 52
    assert blue.control_time_seconds == 34


def test_build_bout_fighter_stats_ausentes_viram_none() -> None:
    """CA-06: luta antiga sem box-score vira linha com stats ``None`` (nao e excluida)."""
    empty = dict.fromkeys(("r_kd", "r_sig_str_landed", "r_sig_str_atmpted", "r_ctrl"), "")
    red = build_bout_fighter(_row(**empty), Corner.RED, fighter_id=1, bout_id=10)
    assert red.knockdowns is None
    assert red.sig_strikes_landed is None
    assert red.control_time_seconds is None
    assert red.source == "kaggle"


# --- Suporte para os testes de resolucao de FK e carga (Postgres real) --------------------


def _add_fighter(session: Session, name: str, dob: date | None = None) -> Fighter:
    """Semeia um lutador minimo de apoio (como a Slice 02 faria) e devolve o model."""
    fighter = Fighter(
        name=name,
        name_normalized=normalize_name(name),
        nickname=None,
        date_of_birth=dob,
        height_cm=None,
        reach_cm=None,
        stance=None,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
    )
    session.add(fighter)
    return fighter


def _add_event(session: Session, name: str, event_date: date) -> Event:
    """Semeia um evento de apoio (como a Slice 03 faria) e devolve o model."""
    event = Event(name=name, date=event_date, location=None, source="kaggle")
    session.add(event)
    return event


def _seed_support(session: Session) -> None:
    """Semeia os fighters e events que as 4 lutas da fixture referenciam."""
    for name in (
        "Alexander Volkanovski",
        "Ilia Topuria",
        "Jon Jones",
        "Stipe Miocic",
        "Fighter X",
        "Fighter Y",
        "Old A",
        "Old B",
    ):
        _add_fighter(session, name)
    _add_event(session, "UFC 300: Test", date(2024, 4, 13))
    _add_event(session, "UFC Fight Night: Test", date(2024, 5, 1))
    session.flush()


def _load_from_fixture(session: Session) -> BoutLoadResult:
    return load_bouts(
        session, load_fight_details(_FIGHT_DETAILS), load_event_details(_EVENT_DETAILS)
    )


def test_resolve_bout_fks_casa_com_ids_persistidos(db_session: Session) -> None:
    """CA-02: red/blue/event de uma linha resolvem para os ids ja persistidos."""
    _seed_support(db_session)
    fighter_index = build_fighter_index(db_session)
    event_index = build_event_index(db_session)

    resolved = resolve_bout_fks(_row(), fighter_index, event_index)
    assert resolved is not None
    event_id, red_id, blue_id = resolved

    (volk_id,) = db_session.scalars(
        select(Fighter.id).where(Fighter.name_normalized == "alexander volkanovski")
    ).all()
    (topu_id,) = db_session.scalars(
        select(Fighter.id).where(Fighter.name_normalized == "ilia topuria")
    ).all()
    (evt_id,) = db_session.scalars(select(Event.id).where(Event.name == "UFC 300: Test")).all()
    assert (event_id, red_id, blue_id) == (evt_id, volk_id, topu_id)


def test_mesmo_lutador_em_cantos_opostos_resolve_para_mesmo_id(db_session: Session) -> None:
    """CA-02: um lutador em canto red numa luta e blue noutra resolve para o mesmo id."""
    _seed_support(db_session)
    fighter_index = build_fighter_index(db_session)
    event_index = build_event_index(db_session)

    as_red = resolve_bout_fks(
        _row(r_name="Jon Jones", b_name="Stipe Miocic", winner="Jon Jones"),
        fighter_index,
        event_index,
    )
    as_blue = resolve_bout_fks(
        _row(r_name="Stipe Miocic", b_name="Jon Jones", winner="Jon Jones"),
        fighter_index,
        event_index,
    )
    assert as_red is not None and as_blue is not None
    assert as_red[1] == as_blue[2]  # Jon Jones: red numa, blue na outra -> mesmo id


def test_resolve_bout_fks_retorna_none_quando_fighter_nao_resolve(db_session: Session) -> None:
    """CA-02: lutador ausente nao gera FK inventada -- resolve para ``None`` (skip)."""
    _seed_support(db_session)
    fighter_index = build_fighter_index(db_session)
    event_index = build_event_index(db_session)

    unresolved = resolve_bout_fks(
        _row(r_name="Nao Existe", b_name="Ilia Topuria"), fighter_index, event_index
    )
    assert unresolved is None


def test_resolve_bout_fks_data_vazia_pula_sem_abortar(db_session: Session) -> None:
    """CA-02: luta cujo ``event_id`` nao existe em event_details vem com ``date=""``
    (preenchido por ``_merge_fight_rows``); resolve para ``None`` (skip), sem levantar
    ``ValueError`` -- nao aborta o seed inteiro."""
    _seed_support(db_session)
    fighter_index = build_fighter_index(db_session)
    event_index = build_event_index(db_session)

    unresolved = resolve_bout_fks(_row(date=""), fighter_index, event_index)
    assert unresolved is None


def test_build_fighter_index_ignora_homonimos_ambiguos(db_session: Session) -> None:
    """CA-02: nome que mapeia para dois lutadores (homonimo) fica fora do indice (ambiguo)."""
    _add_fighter(db_session, "John Doe", date(1985, 1, 1))
    _add_fighter(db_session, "John Doe", date(1986, 2, 2))
    _add_fighter(db_session, "Ilia Topuria", date(1997, 1, 21))
    db_session.flush()

    fighter_index = build_fighter_index(db_session)
    assert "john doe" not in fighter_index
    assert "ilia topuria" in fighter_index


def test_carga_popula_bouts_e_bout_fighters(db_session: Session) -> None:
    """CA-03/CA-05: cada luta vira 1 bout + 2 bout_fighters, com ``source="kaggle"``."""
    _seed_support(db_session)
    result = _load_from_fixture(db_session)

    assert result.bouts_inserted == _TOTAL_BOUTS
    assert result.bout_fighters_inserted == _TOTAL_BOUT_FIGHTERS
    assert result.skipped == 0

    assert db_session.scalar(select(func.count()).select_from(Bout)) == _TOTAL_BOUTS
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == _TOTAL_BOUT_FIGHTERS
    bout_sources = set(db_session.scalars(select(Bout.source)).all())
    bf_sources = set(db_session.scalars(select(BoutFighter.source)).all())
    assert bout_sources == {"kaggle"}
    assert bf_sources == {"kaggle"}


def test_cada_luta_gera_exatamente_duas_linhas_por_canto(db_session: Session) -> None:
    """CA-03: toda luta tem exatamente uma linha red e uma blue em bout_fighters."""
    _seed_support(db_session)
    _load_from_fixture(db_session)

    for (bout_id,) in db_session.execute(select(Bout.id)).all():
        corners = set(
            db_session.scalars(
                select(BoutFighter.corner).where(BoutFighter.bout_id == bout_id)
            ).all()
        )
        assert corners == {Corner.RED, Corner.BLUE}


def test_winner_id_resolvido_e_nulo_em_no_contest(db_session: Session) -> None:
    """CA-01/CA-05: vencedor resolvido pelo canto; no contest tem vencedor e metodo corretos."""
    _seed_support(db_session)
    _load_from_fixture(db_session)

    (evt_id,) = db_session.scalars(select(Event.id).where(Event.name == "UFC 300: Test")).all()
    (topu_id,) = db_session.scalars(
        select(Fighter.id).where(Fighter.name_normalized == "ilia topuria")
    ).all()

    bouts = db_session.scalars(select(Bout).where(Bout.event_id == evt_id)).all()
    by_method = {b.method: b for b in bouts}
    assert by_method[BoutMethod.KO_TKO].winner_id == topu_id
    no_contest = by_method[BoutMethod.NO_CONTEST]
    assert no_contest.winner_id is None


def test_stats_granulares_preservadas_por_canto(db_session: Session) -> None:
    """CA-03: stats por luta preservadas e distintas por canto (nunca media)."""
    _seed_support(db_session)
    _load_from_fixture(db_session)

    (volk_id,) = db_session.scalars(
        select(Fighter.id).where(Fighter.name_normalized == "alexander volkanovski")
    ).all()
    (topu_id,) = db_session.scalars(
        select(Fighter.id).where(Fighter.name_normalized == "ilia topuria")
    ).all()

    volk_line = db_session.scalars(
        select(BoutFighter).where(BoutFighter.fighter_id == volk_id)
    ).one()
    topu_line = db_session.scalars(
        select(BoutFighter).where(BoutFighter.fighter_id == topu_id)
    ).one()
    assert volk_line.sig_strikes_landed == 17
    assert volk_line.control_time_seconds == 278
    assert topu_line.sig_strikes_landed == 52
    assert topu_line.knockdowns == 1


def test_luta_antiga_sem_stats_persiste_com_none(db_session: Session) -> None:
    """CA-06: a luta sem box-score nao e excluida -- vira 2 linhas com stats ``None``."""
    _seed_support(db_session)
    _load_from_fixture(db_session)

    (old_a,) = db_session.scalars(
        select(Fighter.id).where(Fighter.name_normalized == "old a")
    ).all()
    line = db_session.scalars(select(BoutFighter).where(BoutFighter.fighter_id == old_a)).one()
    assert line.sig_strikes_landed is None
    assert line.control_time_seconds is None
    assert line.knockdowns is None


def test_carga_e_idempotente(db_session: Session) -> None:
    """CA-04: a segunda execucao nao insere nada nem altera as duas contagens."""
    _seed_support(db_session)
    first = seed_bouts(db_session, _FIGHT_DETAILS, _EVENT_DETAILS)
    bouts_after_first = db_session.scalar(select(func.count()).select_from(Bout))
    bf_after_first = db_session.scalar(select(func.count()).select_from(BoutFighter))

    second = seed_bouts(db_session, _FIGHT_DETAILS, _EVENT_DETAILS)
    bouts_after_second = db_session.scalar(select(func.count()).select_from(Bout))
    bf_after_second = db_session.scalar(select(func.count()).select_from(BoutFighter))

    assert first.bouts_inserted == _TOTAL_BOUTS
    assert second.bouts_inserted == 0
    assert second.bout_fighters_inserted == 0
    assert bouts_after_first == bouts_after_second == _TOTAL_BOUTS
    assert bf_after_first == bf_after_second == _TOTAL_BOUT_FIGHTERS


def test_cantos_invertidos_nao_duplicam_a_luta(db_session: Session) -> None:
    """CA-04: reexecutar com os cantos trocados nao cria uma segunda luta (par nao-ordenado)."""
    _seed_support(db_session)
    _load_from_fixture(db_session)
    bouts_before = db_session.scalar(select(func.count()).select_from(Bout))

    swapped = load_fight_details(_FIGHT_DETAILS)
    first = swapped.iloc[0]
    swapped.loc[0, ["r_name", "b_name"]] = [first["b_name"], first["r_name"]]
    result = load_bouts(db_session, swapped, load_event_details(_EVENT_DETAILS))

    assert result.bouts_inserted == 0
    assert db_session.scalar(select(func.count()).select_from(Bout)) == bouts_before


def test_luta_com_lutador_ambiguo_e_pulada(db_session: Session) -> None:
    """CA-02: luta cujo lutador nao resolve (homonimo ambiguo) e pulada, sem orfao."""
    _add_fighter(db_session, "John Doe", date(1985, 1, 1))
    _add_fighter(db_session, "John Doe", date(1986, 2, 2))
    _add_fighter(db_session, "Ilia Topuria", date(1997, 1, 21))
    _add_event(db_session, "UFC 300: Test", date(2024, 4, 13))
    db_session.flush()

    fight = load_fight_details(_FIGHT_DETAILS).iloc[[0]].copy()
    fight.loc[0, "r_name"] = "John Doe"
    fight.loc[0, "b_name"] = "Ilia Topuria"
    events = load_event_details(_EVENT_DETAILS).iloc[[0]].copy()
    events.loc[0, "winner"] = "Ilia Topuria"

    result = load_bouts(db_session, fight, events)
    assert result.bouts_inserted == 0
    assert result.skipped == 1
    assert db_session.scalar(select(func.count()).select_from(Bout)) == 0
