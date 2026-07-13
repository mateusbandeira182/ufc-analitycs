"""Testes das features de forma recente point-in-time -- CA-01..CA-04 do Plano 005-02.

Funções puras sobre DataFrame sintético (sem Postgres): a fixture é uma frame longa
determinística em código, o que mantém o teste as-of rápido e sem dependência de banco.
Cobrem: as-of das features expanding (win rate/finish rate/streak) e rolling de janela 3
(striking/grappling/control), rolling parcial (``min_periods=1``), estreia -> NaN explícito
e o teste as-of anti-leakage dedicado (mutar a luta N ou uma luta futura M não altera as
features de N). A corretude temporal (anti-leakage) é o requisito mais crítico da feature.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from apps.bouts.enums import BoutMethod
from ingestion.features.rolling import (
    COL_BOUT_ID,
    COL_FIGHTER_ID,
    COL_SIG_STRIKES_LANDED,
    CONTROL_TIME_AVG_R3,
    FINISH_RATE_PRIOR,
    RECENT_FORM_FEATURES,
    ROUND1_SIG_STRIKE_SHARE_R3,
    ROUND_DYNAMICS_FEATURES,
    SHARE_BODY_R3,
    SHARE_CLINCH_R3,
    SHARE_DISTANCE_R3,
    SHARE_GROUND_R3,
    SHARE_HEAD_R3,
    SHARE_LEG_R3,
    SIG_STRIKES_ABSORBED_PM_R3,
    SIG_STRIKES_LANDED_PM_R3,
    TAKEDOWN_DEFENSE_R3,
    TAKEDOWNS_LANDED_AVG_R3,
    WIN_RATE_PRIOR,
    WIN_STREAK_PRIOR,
    WINDOW_RECENT,
    add_recent_form_features,
    add_round_dynamics_features,
)

_FIGHTER_A = 1


def _participation(
    *,
    fighter_id: int,
    bout_id: int,
    event_day: int,
    result: str,
    method: BoutMethod,
    fight_round: int,
    ending: int,
    sig_landed: int,
    takedowns_landed: int,
    takedowns_attempted: int,
    control: int,
    head_landed: int = 0,
    body_landed: int = 0,
    leg_landed: int = 0,
    distance_landed: int = 0,
    clinch_landed: int = 0,
    ground_landed: int = 0,
) -> dict[str, object]:
    """Uma linha lutador-luta da frame longa (as colunas que ``rolling`` consome)."""
    return {
        COL_FIGHTER_ID: fighter_id,
        COL_BOUT_ID: bout_id,
        "event_date": date(2024, 1, event_day),
        "result": result,
        "method": method.value,
        "round": fight_round,
        "ending_time_seconds": ending,
        COL_SIG_STRIKES_LANDED: sig_landed,
        "takedowns_landed": takedowns_landed,
        "takedowns_attempted": takedowns_attempted,
        "control_time_seconds": control,
        "head_landed": head_landed,
        "body_landed": body_landed,
        "leg_landed": leg_landed,
        "distance_landed": distance_landed,
        "clinch_landed": clinch_landed,
        "ground_landed": ground_landed,
    }


def _known_history_frame() -> pd.DataFrame:
    """Frame determinística: lutador A com 5 lutas conhecidas + oponentes (um por luta).

    A (id 1): V(KO), D(dec), V(sub), V(dec), V(dec). Cada luta traz os dois cantos
    (mesmo ``bout_id``) para o pareamento de adversário (absorvido/defesa de queda).
    As stats do oponente naquela luta são o que A absorve/enfrenta.
    """
    # (bout_id, dia, resultado de A, método, round, ending, A: sig/tdL/tdA/ctrl,
    #  opp: sig/tdL/tdA)
    specs = [
        (101, 1, "win", BoutMethod.KO_TKO, 1, 120, (30, 1, 2, 60), (10, 0, 2)),
        (102, 2, "loss", BoutMethod.DECISION, 3, 300, (20, 0, 1, 30), (40, 2, 4)),
        (103, 3, "win", BoutMethod.SUBMISSION, 2, 60, (15, 3, 5, 120), (5, 0, 1)),
        (104, 4, "win", BoutMethod.DECISION, 3, 300, (50, 1, 2, 10), (25, 1, 3)),
        (105, 5, "win", BoutMethod.DECISION, 1, 60, (100, 0, 0, 0), (1, 0, 1)),
    ]
    rows: list[dict[str, object]] = []
    opponent_id = 2
    for bout_id, day, a_result, method, fight_round, ending, a_stats, opp_stats in specs:
        a_sig, a_tdl, a_tda, a_ctrl = a_stats
        opp_sig, opp_tdl, opp_tda = opp_stats
        opp_result = "loss" if a_result == "win" else "win"
        rows.append(
            _participation(
                fighter_id=_FIGHTER_A,
                bout_id=bout_id,
                event_day=day,
                result=a_result,
                method=method,
                fight_round=fight_round,
                ending=ending,
                sig_landed=a_sig,
                takedowns_landed=a_tdl,
                takedowns_attempted=a_tda,
                control=a_ctrl,
            )
        )
        rows.append(
            _participation(
                fighter_id=opponent_id,
                bout_id=bout_id,
                event_day=day,
                result=opp_result,
                method=method,
                fight_round=fight_round,
                ending=ending,
                sig_landed=opp_sig,
                takedowns_landed=opp_tdl,
                takedowns_attempted=opp_tda,
                control=0,
            )
        )
        opponent_id += 1
    frame = pd.DataFrame(rows)
    return frame.sort_values(
        by=[COL_FIGHTER_ID, "event_date", COL_BOUT_ID], kind="stable"
    ).reset_index(drop=True)


def _fighter_a(out: pd.DataFrame) -> pd.DataFrame:
    """As linhas do lutador A na ordem cronológica (b1..b5), reindexadas de 0."""
    rows = out.loc[out[COL_FIGHTER_ID] == _FIGHTER_A]
    return rows.sort_values("event_date", kind="stable").reset_index(drop=True)


def test_expanding_as_of_win_rate_finish_rate_streak() -> None:
    """CA-01/CA-03: win rate, finish rate e streak da luta N usam só as lutas 1..N-1."""
    out = add_recent_form_features(_known_history_frame())
    a = _fighter_a(out)

    assert a[WIN_RATE_PRIOR].tolist()[1:] == pytest.approx([1.0, 0.5, 2 / 3, 0.75])
    assert pd.isna(a[WIN_RATE_PRIOR].iloc[0])
    assert a[FINISH_RATE_PRIOR].tolist()[1:] == pytest.approx([1.0, 0.5, 2 / 3, 0.5])
    assert pd.isna(a[FINISH_RATE_PRIOR].iloc[0])
    assert a[WIN_STREAK_PRIOR].tolist()[1:] == pytest.approx([1.0, -1.0, 1.0, 2.0])
    assert pd.isna(a[WIN_STREAK_PRIOR].iloc[0])


def test_rolling_as_of_striking_grappling_control() -> None:
    """CA-01/CA-03: rolling de janela 3 de striking/grappling/control, point-in-time.

    Absorvido e defesa de queda vêm do canto oposto da mesma luta (pareamento por
    ``bout_id``), e a agregação usa apenas as lutas anteriores.
    """
    out = add_recent_form_features(_known_history_frame())
    a = _fighter_a(out)

    assert a[SIG_STRIKES_LANDED_PM_R3].tolist()[1:] == pytest.approx(
        [15.0, 50 / 17, 65 / 23, 85 / 36]
    )
    assert a[SIG_STRIKES_ABSORBED_PM_R3].tolist()[1:] == pytest.approx(
        [5.0, 50 / 17, 55 / 23, 70 / 36]
    )
    assert a[TAKEDOWNS_LANDED_AVG_R3].tolist()[1:] == pytest.approx([1.0, 0.5, 4 / 3, 4 / 3])
    assert a[TAKEDOWN_DEFENSE_R3].tolist()[1:] == pytest.approx(
        [1.0, 1 - 2 / 6, 1 - 2 / 7, 1 - 3 / 8]
    )
    assert a[CONTROL_TIME_AVG_R3].tolist()[1:] == pytest.approx([60.0, 45.0, 70.0, 160 / 3])


def test_rolling_window_caps_at_three() -> None:
    """CA-04: na 5a luta a janela cobre só as lutas 2..4 (a 1a sai da janela de 3)."""
    out = add_recent_form_features(_known_history_frame())
    a = _fighter_a(out)

    # b5: soma de striking sobre b2,b3,b4 (b1 fora da janela de 3).
    assert a[SIG_STRIKES_LANDED_PM_R3].iloc[4] == pytest.approx(85 / 36)
    assert WINDOW_RECENT == 3


def test_rolling_parcial_com_menos_de_tres_lutas() -> None:
    """CA-04: com 1 e 2 lutas anteriores as features existem (``min_periods=1``)."""
    out = add_recent_form_features(_known_history_frame())
    a = _fighter_a(out)

    # b2 tem 1 luta anterior; b3 tem 2. Nenhuma é NaN (janela parcial válida).
    assert not a[SIG_STRIKES_LANDED_PM_R3].iloc[1:3].isna().any()
    assert not a[CONTROL_TIME_AVG_R3].iloc[1:3].isna().any()


def test_estreia_todas_as_features_nan() -> None:
    """CA-02: na primeira luta de cada lutador, todas as features são NaN explícito."""
    out = add_recent_form_features(_known_history_frame())

    estreias = out.groupby(COL_FIGHTER_ID).head(1)
    assert out.loc[estreias.index, RECENT_FORM_FEATURES].isna().all().all()


def test_as_of_anti_leakage_mutar_luta_corrente_ou_futura() -> None:
    """CA-01 (peça central): mutar a luta N ou uma luta futura M não altera N.

    Prova direta de que a luta corrente não vaza (a linha N não lê a própria luta) e de
    que lutas futuras não vazam para o passado.
    """
    frame = _known_history_frame()
    base = add_recent_form_features(frame)
    idx_n = frame.index[(frame[COL_FIGHTER_ID] == _FIGHTER_A) & (frame[COL_BOUT_ID] == 103)][0]
    idx_futura = frame.index[(frame[COL_FIGHTER_ID] == _FIGHTER_A) & (frame[COL_BOUT_ID] == 104)][0]
    esperado_n = base.loc[idx_n][RECENT_FORM_FEATURES]

    mut_corrente = frame.copy()
    mut_corrente.loc[idx_n, COL_SIG_STRIKES_LANDED] = 999
    out_corrente = add_recent_form_features(mut_corrente)
    pd.testing.assert_series_equal(out_corrente.loc[idx_n][RECENT_FORM_FEATURES], esperado_n)

    mut_futura = frame.copy()
    mut_futura.loc[idx_futura, COL_SIG_STRIKES_LANDED] = 999
    out_futura = add_recent_form_features(mut_futura)
    pd.testing.assert_series_equal(out_futura.loc[idx_n][RECENT_FORM_FEATURES], esperado_n)


def test_preserva_colunas_originais_e_adiciona_features() -> None:
    """A saída mantém as colunas de entrada e acrescenta apenas as features de forma recente."""
    frame = _known_history_frame()
    out = add_recent_form_features(frame)

    assert set(frame.columns).issubset(set(out.columns))
    assert set(RECENT_FORM_FEATURES).issubset(set(out.columns))
    # Não muta a frame de entrada (opera sobre cópia).
    assert list(frame.columns) == list(_known_history_frame().columns)


def test_denominador_zero_produz_nan_nao_inf() -> None:
    """Denominador zero (minutos anteriores nulos / quedas enfrentadas zero) -> NaN, nunca inf.

    ``inf`` não é JSON válido e quebraria a materialização em JSONB; a guarda ``_safe_ratio``
    mapeia ``x/0`` para ``NaN`` explícito (coerente com a estreia). Aqui a 1ª luta de A tem
    ``fight_minutes == 0`` (round 1, ending 0) e ambos os cantos com 0 quedas tentadas, então
    as taxas por minuto e a defesa de queda da 2ª luta (que usam só a 1ª como histórico)
    seriam ``x/0`` sem a guarda.
    """
    rows = [
        _participation(
            fighter_id=_FIGHTER_A,
            bout_id=1,
            event_day=1,
            result="win",
            method=BoutMethod.KO_TKO,
            fight_round=1,
            ending=0,
            sig_landed=30,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
        ),
        _participation(
            fighter_id=2,
            bout_id=1,
            event_day=1,
            result="loss",
            method=BoutMethod.KO_TKO,
            fight_round=1,
            ending=0,
            sig_landed=10,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
        ),
        _participation(
            fighter_id=_FIGHTER_A,
            bout_id=2,
            event_day=2,
            result="win",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=50,
            takedowns_landed=1,
            takedowns_attempted=2,
            control=100,
        ),
        _participation(
            fighter_id=3,
            bout_id=2,
            event_day=2,
            result="loss",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=20,
            takedowns_landed=0,
            takedowns_attempted=1,
            control=0,
        ),
    ]
    frame = (
        pd.DataFrame(rows)
        .sort_values(by=[COL_FIGHTER_ID, "event_date", COL_BOUT_ID], kind="stable")
        .reset_index(drop=True)
    )

    segunda_luta = _fighter_a(add_recent_form_features(frame)).iloc[1]
    # pd.isna é True para NaN e False para inf: sem a guarda seria inf -> teste vermelho.
    assert pd.isna(segunda_luta[SIG_STRIKES_LANDED_PM_R3])
    assert pd.isna(segunda_luta[SIG_STRIKES_ABSORBED_PM_R3])
    assert pd.isna(segunda_luta[TAKEDOWN_DEFENSE_R3])


# --- Perfil de striking: share por alvo (cabeça/corpo/perna) e posição (dist/clinch/solo) ---

_STRIKING_SHARES = [
    SHARE_HEAD_R3,
    SHARE_BODY_R3,
    SHARE_LEG_R3,
    SHARE_DISTANCE_R3,
    SHARE_CLINCH_R3,
    SHARE_GROUND_R3,
]


def _striking_frame() -> pd.DataFrame:
    """Lutador A com 2 lutas e splits conhecidos; oponente por luta (pareamento intacto).

    Luta 1 de A: alvo cabeça=20, corpo=5, perna=5 (total 30); posição distância=18,
    clinch=6, solo=6 (total 30). Luta 2: valores diferentes -- as shares da luta 2 usam
    **só** a luta 1 (``shift(1)``).
    """
    rows = [
        _participation(
            fighter_id=_FIGHTER_A,
            bout_id=101,
            event_day=1,
            result="win",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=30,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
            head_landed=20,
            body_landed=5,
            leg_landed=5,
            distance_landed=18,
            clinch_landed=6,
            ground_landed=6,
        ),
        _participation(
            fighter_id=2,
            bout_id=101,
            event_day=1,
            result="loss",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=10,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
        ),
        _participation(
            fighter_id=_FIGHTER_A,
            bout_id=102,
            event_day=2,
            result="win",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=20,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
            head_landed=10,
            body_landed=10,
            leg_landed=0,
            distance_landed=5,
            clinch_landed=5,
            ground_landed=10,
        ),
        _participation(
            fighter_id=3,
            bout_id=102,
            event_day=2,
            result="loss",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=5,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
        ),
    ]
    return (
        pd.DataFrame(rows)
        .sort_values(by=[COL_FIGHTER_ID, "event_date", COL_BOUT_ID], kind="stable")
        .reset_index(drop=True)
    )


def test_share_de_striking_as_of_usa_so_lutas_anteriores() -> None:
    """CA-01: as shares da 2a luta usam só a 1a; razão de somas na janela, point-in-time."""
    out = add_recent_form_features(_striking_frame())
    a = _fighter_a(out)

    # Share da 2a luta = distribuição da 1a luta (cabeça 20 / (20+5+5)=30, etc.).
    assert a[SHARE_HEAD_R3].iloc[1] == pytest.approx(20 / 30)
    assert a[SHARE_BODY_R3].iloc[1] == pytest.approx(5 / 30)
    assert a[SHARE_LEG_R3].iloc[1] == pytest.approx(5 / 30)
    assert a[SHARE_DISTANCE_R3].iloc[1] == pytest.approx(18 / 30)
    assert a[SHARE_CLINCH_R3].iloc[1] == pytest.approx(6 / 30)
    assert a[SHARE_GROUND_R3].iloc[1] == pytest.approx(6 / 30)


def test_share_de_striking_estreia_e_nan() -> None:
    """CA-01: na estreia (sem histórico) todas as shares de striking são NaN explícito."""
    out = add_recent_form_features(_striking_frame())
    a = _fighter_a(out)

    for coluna in _STRIKING_SHARES:
        assert pd.isna(a[coluna].iloc[0])
    assert set(_STRIKING_SHARES).issubset(set(RECENT_FORM_FEATURES))


def test_share_de_striking_denominador_zero_vira_nan_nao_inf() -> None:
    """CA-01: sem golpes conectados anteriores, share é NaN (denominador zero), nunca inf."""
    rows = [
        _participation(
            fighter_id=_FIGHTER_A,
            bout_id=201,
            event_day=1,
            result="win",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=0,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
            head_landed=0,
            body_landed=0,
            leg_landed=0,
            distance_landed=0,
            clinch_landed=0,
            ground_landed=0,
        ),
        _participation(
            fighter_id=2,
            bout_id=201,
            event_day=1,
            result="loss",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=10,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
        ),
        _participation(
            fighter_id=_FIGHTER_A,
            bout_id=202,
            event_day=2,
            result="win",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=20,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
            head_landed=10,
            body_landed=5,
            leg_landed=5,
            distance_landed=10,
            clinch_landed=5,
            ground_landed=5,
        ),
        _participation(
            fighter_id=3,
            bout_id=202,
            event_day=2,
            result="loss",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=5,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
        ),
    ]
    frame = (
        pd.DataFrame(rows)
        .sort_values(by=[COL_FIGHTER_ID, "event_date", COL_BOUT_ID], kind="stable")
        .reset_index(drop=True)
    )

    segunda = _fighter_a(add_recent_form_features(frame)).iloc[1]
    # A 1a luta de A não conectou golpe algum -> denominador zero -> NaN (não inf).
    for coluna in _STRIKING_SHARES:
        assert pd.isna(segunda[coluna])


# --- Dinâmica por round: round1_sig_strike_share point-in-time -------------------------


def _round_long_frame() -> pd.DataFrame:
    """Frame longa mínima: lutador A (com round-a-round) e B (sem), 2 lutas cada."""
    return (
        pd.DataFrame(
            [
                {COL_FIGHTER_ID: 1, COL_BOUT_ID: 101, "event_date": date(2024, 1, 1)},
                {COL_FIGHTER_ID: 1, COL_BOUT_ID: 102, "event_date": date(2024, 2, 1)},
                {COL_FIGHTER_ID: 2, COL_BOUT_ID: 201, "event_date": date(2024, 1, 1)},
                {COL_FIGHTER_ID: 2, COL_BOUT_ID: 202, "event_date": date(2024, 2, 1)},
            ]
        )
        .sort_values(by=[COL_FIGHTER_ID, "event_date", COL_BOUT_ID], kind="stable")
        .reset_index(drop=True)
    )


def _round_stats_for_a() -> pd.DataFrame:
    """Round-a-round só do lutador A: luta 101 (r1=15,r2=5) e 102 (r1=10,r2=10,r3=10)."""
    return pd.DataFrame(
        [
            {COL_BOUT_ID: 101, COL_FIGHTER_ID: 1, "round": 1, COL_SIG_STRIKES_LANDED: 15},
            {COL_BOUT_ID: 101, COL_FIGHTER_ID: 1, "round": 2, COL_SIG_STRIKES_LANDED: 5},
            {COL_BOUT_ID: 102, COL_FIGHTER_ID: 1, "round": 1, COL_SIG_STRIKES_LANDED: 10},
            {COL_BOUT_ID: 102, COL_FIGHTER_ID: 1, "round": 2, COL_SIG_STRIKES_LANDED: 10},
            {COL_BOUT_ID: 102, COL_FIGHTER_ID: 1, "round": 3, COL_SIG_STRIKES_LANDED: 10},
        ]
    )


def test_round_dynamics_as_of_usa_so_lutas_anteriores() -> None:
    """CA-02: a dinâmica por round da 2a luta usa só a 1a; estreia é NaN.

    A luta 101 de A conectou 15 dos 20 golpes no round 1 (share 0.75); a feature as-of da
    luta 102 é a média das lutas anteriores = 0.75. A estreia (luta 101) não tem histórico
    -> NaN.
    """
    out = add_round_dynamics_features(_round_long_frame(), _round_stats_for_a())
    a = out.loc[out[COL_FIGHTER_ID] == 1].sort_values("event_date").reset_index(drop=True)

    assert pd.isna(a[ROUND1_SIG_STRIKE_SHARE_R3].iloc[0])
    assert a[ROUND1_SIG_STRIKE_SHARE_R3].iloc[1] == pytest.approx(0.75)
    assert ROUND1_SIG_STRIKE_SHARE_R3 in ROUND_DYNAMICS_FEATURES


def test_round_dynamics_degrada_para_nan_sem_round_a_round() -> None:
    """CA-02: lutador sem round-a-round nas lutas anteriores tem a feature NaN (não erro/inf)."""
    out = add_round_dynamics_features(_round_long_frame(), _round_stats_for_a())
    b = out.loc[out[COL_FIGHTER_ID] == 2].sort_values("event_date").reset_index(drop=True)

    # B não tem nenhuma linha em bout_fighter_rounds -> feature NaN nas duas lutas.
    assert b[ROUND1_SIG_STRIKE_SHARE_R3].isna().all()


def test_round_dynamics_round_stats_vazio_tudo_nan() -> None:
    """CA-02: sem nenhum round-a-round no banco, a feature existe e é toda NaN."""
    vazio = pd.DataFrame(columns=[COL_BOUT_ID, COL_FIGHTER_ID, "round", COL_SIG_STRIKES_LANDED])

    out = add_round_dynamics_features(_round_long_frame(), vazio)

    assert ROUND1_SIG_STRIKE_SHARE_R3 in out.columns
    assert out[ROUND1_SIG_STRIKE_SHARE_R3].isna().all()


def test_round_dynamics_nao_persiste_metrico_por_bout_local() -> None:
    """CA-02 (anti-leakage): só a feature as-of entra; o métrico por-bout não vira coluna."""
    out = add_round_dynamics_features(_round_long_frame(), _round_stats_for_a())

    # A única coluna nova é a agregada as-of; o share por-bout da luta corrente não sobra.
    novas = set(out.columns) - set(_round_long_frame().columns)
    assert novas == {ROUND1_SIG_STRIKE_SHARE_R3}
