"""Testes da frame longa por lutador-luta -- CA-01 do Plano 005-01.

Cobrem a leitura do granular via Pandas dentro da transação de teste
(``read_granular``), a junção das quatro tabelas numa linha por participação
(``build_long_frame``), a derivação do resultado por linha (win/loss/no_contest/
draw), a preservação das stats brutas e do ``source`` (nunca médias, ADR 0001) e a
ordenação temporal determinística por ``fighter_id``/``event_date``/``bout_id``.

Rodam contra o Postgres de teste ``ufc_bum_test`` na sessão transacional
(``db_session``, rollback ao final). A semeadura reusa o estilo de builders locais
de ``tests/ingestion/test_seed_bouts.py``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.features.long_frame import build_long_frame, read_granular
from ingestion.normalize import normalize_name


def _add_fighter(session: Session, name: str, dob: date | None = None) -> Fighter:
    """Semeia um lutador mínimo e devolve o model já com id após o flush."""
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
    session.flush()
    return fighter


def _add_event(session: Session, name: str, event_date: date) -> Event:
    """Semeia um evento de apoio e devolve o model já com id após o flush."""
    event = Event(name=name, date=event_date, location=None, source="kaggle")
    session.add(event)
    session.flush()
    return event


def _add_bout(
    session: Session,
    *,
    event: Event,
    red: Fighter,
    blue: Fighter,
    winner: Fighter | None,
    method: BoutMethod = BoutMethod.DECISION,
    red_sig_strikes: int | None = None,
    source: str = "kaggle",
) -> Bout:
    """Semeia uma luta com exatamente duas linhas em ``bout_fighters`` (red/blue).

    ``winner`` nulo com ``method`` != ``NO_CONTEST`` representa empate; nulo com
    ``NO_CONTEST`` representa no contest. As stats do canto red são parametrizáveis
    para exercitar a preservação do dado bruto por luta.
    """
    bout = Bout(
        event_id=event.id,
        winner_id=winner.id if winner is not None else None,
        method=method,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source=source,
    )
    session.add(bout)
    session.flush()
    session.add_all(
        [
            BoutFighter(
                bout_id=bout.id,
                fighter_id=red.id,
                corner=Corner.RED,
                knockdowns=None,
                sig_strikes_landed=red_sig_strikes,
                sig_strikes_attempted=None,
                takedowns_landed=None,
                takedowns_attempted=None,
                submission_attempts=None,
                control_time_seconds=None,
                source=source,
            ),
            BoutFighter(
                bout_id=bout.id,
                fighter_id=blue.id,
                corner=Corner.BLUE,
                knockdowns=None,
                sig_strikes_landed=None,
                sig_strikes_attempted=None,
                takedowns_landed=None,
                takedowns_attempted=None,
                submission_attempts=None,
                control_time_seconds=None,
                source=source,
            ),
        ]
    )
    session.flush()
    return bout


def test_read_granular_devolve_as_quatro_frames(db_session: Session) -> None:
    """``read_granular`` lê as quatro tabelas na transação, com colunas-chave e contagens."""
    red = _add_fighter(db_session, "Alexander Volkanovski")
    blue = _add_fighter(db_session, "Ilia Topuria")
    event = _add_event(db_session, "UFC 300: Test", date(2024, 4, 13))
    _add_bout(db_session, event=event, red=red, blue=blue, winner=blue, method=BoutMethod.KO_TKO)

    frames = read_granular(db_session)

    assert set(frames.bout_fighters.columns) >= {"bout_id", "fighter_id", "corner"}
    assert len(frames.fighters) == 2
    assert len(frames.events) == 1
    assert len(frames.bouts) == 1
    assert len(frames.bout_fighters) == 2


def _seed_two_bouts(db_session: Session) -> tuple[int, int]:
    """Semeia dois eventos/lutas conhecidos e devolve ``(winner_id, loser_id)`` da 1a luta.

    Luta 1: Volkanovski (red) vence Topuria (blue) por KO/TKO, com stats brutas no red.
    Luta 2: no contest entre Jones (red) e Miocic (blue) -- vencedor nulo.
    """
    volk = _add_fighter(db_session, "Alexander Volkanovski")
    topu = _add_fighter(db_session, "Ilia Topuria")
    jones = _add_fighter(db_session, "Jon Jones")
    miocic = _add_fighter(db_session, "Stipe Miocic")
    evt1 = _add_event(db_session, "UFC 300: Test", date(2024, 4, 13))
    evt2 = _add_event(db_session, "UFC Fight Night: Test", date(2024, 5, 1))
    _add_bout(
        db_session,
        event=evt1,
        red=volk,
        blue=topu,
        winner=volk,
        method=BoutMethod.KO_TKO,
        red_sig_strikes=17,
    )
    _add_bout(
        db_session,
        event=evt2,
        red=jones,
        blue=miocic,
        winner=None,
        method=BoutMethod.NO_CONTEST,
    )
    return volk.id, topu.id


def test_build_long_frame_uma_linha_por_participacao(db_session: Session) -> None:
    """CA-01: o nº de linhas é o de participações (== 2 x nº de bouts)."""
    _seed_two_bouts(db_session)
    frames = read_granular(db_session)

    long_df = build_long_frame(frames)

    assert len(long_df) == len(frames.bout_fighters)
    assert len(long_df) == 2 * len(frames.bouts) == 4


def test_build_long_frame_deriva_win_e_loss(db_session: Session) -> None:
    """CA-01: o vencedor recebe ``win`` e o perdedor ``loss`` na mesma luta."""
    winner_id, loser_id = _seed_two_bouts(db_session)

    long_df = build_long_frame(read_granular(db_session))

    vencedor = long_df.loc[long_df["fighter_id"] == winner_id].iloc[0]
    perdedor = long_df.loc[long_df["fighter_id"] == loser_id].iloc[0]
    assert vencedor["result"] == "win"
    assert perdedor["result"] == "loss"


def test_build_long_frame_deriva_no_contest(db_session: Session) -> None:
    """CA-01: luta com vencedor nulo e método ``NO_CONTEST`` vira ``no_contest`` nos dois cantos."""
    jones = _add_fighter(db_session, "Jon Jones")
    miocic = _add_fighter(db_session, "Stipe Miocic")
    evt = _add_event(db_session, "UFC Fight Night: Test", date(2024, 5, 1))
    _add_bout(
        db_session, event=evt, red=jones, blue=miocic, winner=None, method=BoutMethod.NO_CONTEST
    )

    long_df = build_long_frame(read_granular(db_session))

    assert set(long_df["result"]) == {"no_contest"}


def test_build_long_frame_deriva_draw(db_session: Session) -> None:
    """CA-01: vencedor nulo com método != ``NO_CONTEST`` (empate) vira ``draw`` nos dois cantos."""
    a = _add_fighter(db_session, "Fighter X")
    b = _add_fighter(db_session, "Fighter Y")
    evt = _add_event(db_session, "UFC Draw: Test", date(2024, 6, 1))
    _add_bout(db_session, event=evt, red=a, blue=b, winner=None, method=BoutMethod.DECISION)

    long_df = build_long_frame(read_granular(db_session))

    assert set(long_df["result"]) == {"draw"}


def test_build_long_frame_preserva_stats_brutas_e_source(db_session: Session) -> None:
    """CA-01: cada linha traz a stat bruta daquela luta, ``event_date`` e ``source`` (sem média)."""
    winner_id, _ = _seed_two_bouts(db_session)

    long_df = build_long_frame(read_granular(db_session))

    vencedor = long_df.loc[long_df["fighter_id"] == winner_id].iloc[0]
    assert vencedor["sig_strikes_landed"] == 17
    assert vencedor["event_date"] == date(2024, 4, 13)
    assert vencedor["source"] == "kaggle"
    assert list(long_df.columns) == [
        "fighter_id",
        "fighter_name",
        "event_id",
        "event_name",
        "event_date",
        "bout_id",
        "corner",
        "result",
        "method",
        "round",
        "ending_time_seconds",
        "knockdowns",
        "sig_strikes_landed",
        "sig_strikes_attempted",
        "takedowns_landed",
        "takedowns_attempted",
        "submission_attempts",
        "control_time_seconds",
        "source",
    ]


def test_build_long_frame_ordena_por_lutador_e_data(db_session: Session) -> None:
    """CA-01: as lutas de um lutador saem em ordem crescente de data, desempate por ``bout_id``.

    As lutas são semeadas **fora de ordem cronológica** (as duas da data posterior antes
    da anterior) para que a ordenação estável seja de fato exigida -- a ordem natural de
    leitura não coincide com a esperada.
    """
    hero = _add_fighter(db_session, "Alexander Volkanovski")
    rivals = [_add_fighter(db_session, f"Rival {i}") for i in range(3)]
    early = _add_event(db_session, "UFC Early: Test", date(2023, 1, 1))
    late = _add_event(db_session, "UFC Late: Test", date(2024, 12, 31))
    # Insere primeiro as duas lutas da data posterior, depois a da anterior: a ordem de
    # inserção (e de leitura) fica [late_1, late_2, early], distinta da cronológica.
    b_late_1 = _add_bout(db_session, event=late, red=hero, blue=rivals[1], winner=hero)
    b_late_2 = _add_bout(db_session, event=late, red=hero, blue=rivals[2], winner=hero)
    b_early = _add_bout(db_session, event=early, red=hero, blue=rivals[0], winner=hero)

    long_df = build_long_frame(read_granular(db_session))

    do_hero = long_df.loc[long_df["fighter_id"] == hero.id]
    assert do_hero["event_date"].is_monotonic_increasing
    # Cronológica: a data anterior primeiro; empate na data posterior desempata por bout_id.
    assert list(do_hero["bout_id"]) == [b_early.id, b_late_1.id, b_late_2.id]


def test_build_long_frame_e_deterministico(db_session: Session) -> None:
    """CA-01: construir a frame duas vezes produz DataFrames idênticos (ordenação estável)."""
    _seed_two_bouts(db_session)

    primeira = build_long_frame(read_granular(db_session))
    segunda = build_long_frame(read_granular(db_session))

    pd.testing.assert_frame_equal(primeira, segunda)
