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

from analysis.dataset import PROSCRIBED_FEATURE_BASES
from apps.bouts.enums import BoutMethod
from ingestion.features.rolling import (
    BODY_ACCURACY_R3,
    CAREER_MINUTES_BEFORE,
    CLINCH_ACCURACY_R3,
    COL_BOUT_ID,
    COL_FIGHTER_ID,
    COL_ROUND_NUMBER,
    COL_SIG_STRIKES_LANDED,
    CONTROL_TIME_AVG_R3,
    CONTROL_TIME_AVG_TREND,
    DISTANCE_ACCURACY_R3,
    FINISH_RATE_PRIOR,
    FIVE_ROUND_BOUTS_BEFORE,
    GROUND_ACCURACY_R3,
    HEAD_ACCURACY_R3,
    KNOCKDOWNS_AVG_R3,
    KNOCKDOWNS_PM_R3,
    KO_LOSSES_PRIOR,
    LEG_ACCURACY_R3,
    RECENT_FORM_FEATURES,
    ROUND1_SIG_STRIKE_SHARE_R3,
    ROUND_DYNAMICS_FEATURES,
    SHARE_BODY_R3,
    SHARE_CLINCH_R3,
    SHARE_DISTANCE_R3,
    SHARE_GROUND_R3,
    SHARE_HEAD_R3,
    SHARE_LEG_R3,
    SIG_STRIKE_ACCURACY_R3,
    SIG_STRIKES_ABSORBED_PM_R3,
    SIG_STRIKES_ABSORBED_PM_TREND,
    SIG_STRIKES_LANDED_PM_R3,
    SIG_STRIKES_LANDED_PM_TREND,
    SUBMISSION_ATTEMPTS_AVG_R3,
    SUBMISSION_LOSSES_PRIOR,
    TAKEDOWN_ACCURACY_R3,
    TAKEDOWN_DEFENSE_R3,
    TAKEDOWN_DEFENSE_TREND,
    TAKEDOWNS_LANDED_AVG_R3,
    TAKEDOWNS_LANDED_AVG_TREND,
    TOTAL_TO_SIG_STRIKE_RATIO_R3,
    WIN_RATE_PRIOR,
    WIN_STREAK_PRIOR,
    WINDOW_RECENT,
    add_recent_form_features,
    add_round_dynamics_features,
)
from ingestion.features.trajectory import CAREER_BOUTS_BEFORE, add_experience

_FIGHTER_A = 1

# As bases as-of do bloco C da SPEC 009 (Slice 04). Lista literal de propósito -- se
# ``RECENT_FORM_FEATURES`` crescer sem que o bloco cresça junto, a guarda de nomes (RF-10) e
# o registro da ablação continuam falando do bloco certo.
_BLOCO_C_BASES: tuple[str, ...] = (
    KO_LOSSES_PRIOR,
    SUBMISSION_LOSSES_PRIOR,
    CAREER_MINUTES_BEFORE,
    SIG_STRIKES_LANDED_PM_TREND,
    SIG_STRIKES_ABSORBED_PM_TREND,
    TAKEDOWNS_LANDED_AVG_TREND,
    TAKEDOWN_DEFENSE_TREND,
    CONTROL_TIME_AVG_TREND,
)


def _participation(
    *,
    fighter_id: int,
    bout_id: int,
    event_day: int,
    result: str,
    method: BoutMethod | None,
    fight_round: int,
    ending: int,
    sig_landed: int,
    takedowns_landed: int,
    takedowns_attempted: int,
    control: float,
    head_landed: int = 0,
    body_landed: int = 0,
    leg_landed: int = 0,
    distance_landed: int = 0,
    clinch_landed: int = 0,
    ground_landed: int = 0,
    sig_attempted: int = 0,
    head_attempted: int = 0,
    body_attempted: int = 0,
    leg_attempted: int = 0,
    distance_attempted: int = 0,
    clinch_attempted: int = 0,
    ground_attempted: int = 0,
    knockdowns: int = 0,
    submission_attempts: int = 0,
    total_strikes_landed: float = 0,
    scheduled_rounds: int | None = 3,
) -> dict[str, object]:
    """Uma linha lutador-luta da frame longa (as colunas que ``rolling`` consome).

    As colunas tentadas (``*_attempted``), os knockdowns, as tentativas de finalização e o
    total de golpes conectados entram com ``0`` por padrão: são as cruas que o bloco A da
    SPEC 009 lê, e ``0`` mantém as fixtures antigas com denominador zero -- ou seja, com as
    precisões ausentes, que é o comportamento correto de quem não registrou tentativa
    alguma (RF-03), e não um valor inventado.

    ``method`` aceita ``None`` e ``control`` aceita ``NaN`` desde a Slice 04 (bloco C):
    método ausente é degradação real do granular (não incrementa contagem de derrota por
    nocaute nem por finalização) e tempo de controle ausente é o que separa a ponta recente
    da ponta de carreira nas tendências -- as duas ausências precisam atravessar a fixture
    sem virar zero.

    ``scheduled_rounds`` entra na Slice 03 (bloco B2): ``add_recent_form_features`` passou a
    consumi-la para contar a experiência em cinco rounds. O padrão ``3`` é o formato comum de
    card, e ``None`` é aceito de propósito -- 6,9% das lutas do banco real não têm o formato
    registrado, e a ausência precisa atravessar a fixture sem virar zero.
    """
    return {
        COL_FIGHTER_ID: fighter_id,
        COL_BOUT_ID: bout_id,
        "event_date": date(2024, 1, event_day),
        "result": result,
        "method": method.value if method else None,
        "round": fight_round,
        "ending_time_seconds": ending,
        COL_SIG_STRIKES_LANDED: sig_landed,
        "sig_strikes_attempted": sig_attempted,
        "takedowns_landed": takedowns_landed,
        "takedowns_attempted": takedowns_attempted,
        "control_time_seconds": control,
        "head_landed": head_landed,
        "body_landed": body_landed,
        "leg_landed": leg_landed,
        "distance_landed": distance_landed,
        "clinch_landed": clinch_landed,
        "ground_landed": ground_landed,
        "head_attempted": head_attempted,
        "body_attempted": body_attempted,
        "leg_attempted": leg_attempted,
        "distance_attempted": distance_attempted,
        "clinch_attempted": clinch_attempted,
        "ground_attempted": ground_attempted,
        "knockdowns": knockdowns,
        "submission_attempts": submission_attempts,
        "total_strikes_landed": total_strikes_landed,
        "scheduled_rounds": scheduled_rounds,
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

    ``inf`` não é JSON válido e quebraria a materialização em JSONB; a guarda ``safe_ratio``
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
            {COL_BOUT_ID: 101, COL_FIGHTER_ID: 1, COL_ROUND_NUMBER: 1, COL_SIG_STRIKES_LANDED: 15},
            {COL_BOUT_ID: 101, COL_FIGHTER_ID: 1, COL_ROUND_NUMBER: 2, COL_SIG_STRIKES_LANDED: 5},
            {COL_BOUT_ID: 102, COL_FIGHTER_ID: 1, COL_ROUND_NUMBER: 1, COL_SIG_STRIKES_LANDED: 10},
            {COL_BOUT_ID: 102, COL_FIGHTER_ID: 1, COL_ROUND_NUMBER: 2, COL_SIG_STRIKES_LANDED: 10},
            {COL_BOUT_ID: 102, COL_FIGHTER_ID: 1, COL_ROUND_NUMBER: 3, COL_SIG_STRIKES_LANDED: 10},
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
    vazio = pd.DataFrame(
        columns=[COL_BOUT_ID, COL_FIGHTER_ID, COL_ROUND_NUMBER, COL_SIG_STRIKES_LANDED]
    )

    out = add_round_dynamics_features(_round_long_frame(), vazio)

    assert ROUND1_SIG_STRIKE_SHARE_R3 in out.columns
    assert out[ROUND1_SIG_STRIKE_SHARE_R3].isna().all()


def test_round_dynamics_nao_persiste_metrico_por_bout_local() -> None:
    """CA-02 (anti-leakage): só a feature as-of entra; o métrico por-bout não vira coluna."""
    out = add_round_dynamics_features(_round_long_frame(), _round_stats_for_a())

    # A única coluna nova é a agregada as-of; o share por-bout da luta corrente não sobra.
    novas = set(out.columns) - set(_round_long_frame().columns)
    assert novas == {ROUND1_SIG_STRIKE_SHARE_R3}


# --- Bloco A da SPEC 009: precisão, poder, ameaça de grappling e trabalho total ---------

_PRECISAO_FEATURES = [
    SIG_STRIKE_ACCURACY_R3,
    HEAD_ACCURACY_R3,
    BODY_ACCURACY_R3,
    LEG_ACCURACY_R3,
    DISTANCE_ACCURACY_R3,
    CLINCH_ACCURACY_R3,
    GROUND_ACCURACY_R3,
    TAKEDOWN_ACCURACY_R3,
]


def _bloco_a_frame() -> pd.DataFrame:
    """Lutador A com 3 lutas cujas duas primeiras separam razão de somas de média de razões.

    Luta 1 é curta e cirúrgica (1 de 1 golpe significativo, 2 minutos); luta 2 é longa e de
    volume (10 de 40, 15 minutos). A razão de somas da janela é ``11/41 ≈ 0,268``, enquanto
    a média das razões por luta seria ``(1,00 + 0,25)/2 = 0,625`` -- qualquer implementação
    que faça a média das razões falha nestes números. Cada dimensão (cabeça, corpo, perna,
    distância, clinch, solo, queda) tem sua própria dupla de valores, pelo mesmo motivo.
    """
    # (bout_id, dia, round, ending, sig L/A, head L/A, body L/A, leg L/A,
    #  dist L/A, clinch L/A, ground L/A, td L/A, kd, sub att, total conectado)
    specs = [
        (101, 1, 1, 120, (1, 1), (1, 1), (1, 2), (0, 1), (1, 1), (0, 1), (0, 2), (1, 1), 1, 0, 5),
        (
            102,
            2,
            3,
            300,
            (10, 40),
            (6, 30),
            (3, 8),
            (1, 2),
            (8, 32),
            (1, 4),
            (1, 4),
            (1, 9),
            0,
            3,
            60,
        ),
        (103, 3, 3, 300, (5, 5), (5, 5), (0, 0), (0, 0), (5, 5), (0, 0), (0, 0), (0, 0), 0, 0, 5),
    ]
    rows: list[dict[str, object]] = []
    opponent_id = 2
    for (
        bout_id,
        day,
        fight_round,
        ending,
        sig,
        head,
        body,
        leg,
        distance,
        clinch,
        ground,
        takedowns,
        knockdowns,
        submissions,
        total_landed,
    ) in specs:
        rows.append(
            _participation(
                fighter_id=_FIGHTER_A,
                bout_id=bout_id,
                event_day=day,
                result="win",
                method=BoutMethod.DECISION,
                fight_round=fight_round,
                ending=ending,
                sig_landed=sig[0],
                sig_attempted=sig[1],
                takedowns_landed=takedowns[0],
                takedowns_attempted=takedowns[1],
                control=0,
                head_landed=head[0],
                head_attempted=head[1],
                body_landed=body[0],
                body_attempted=body[1],
                leg_landed=leg[0],
                leg_attempted=leg[1],
                distance_landed=distance[0],
                distance_attempted=distance[1],
                clinch_landed=clinch[0],
                clinch_attempted=clinch[1],
                ground_landed=ground[0],
                ground_attempted=ground[1],
                knockdowns=knockdowns,
                submission_attempts=submissions,
                total_strikes_landed=total_landed,
            )
        )
        rows.append(
            _participation(
                fighter_id=opponent_id,
                bout_id=bout_id,
                event_day=day,
                result="loss",
                method=BoutMethod.DECISION,
                fight_round=fight_round,
                ending=ending,
                sig_landed=1,
                sig_attempted=4,
                takedowns_landed=0,
                takedowns_attempted=1,
                control=0,
            )
        )
        opponent_id += 1
    return (
        pd.DataFrame(rows)
        .sort_values(by=[COL_FIGHTER_ID, "event_date", COL_BOUT_ID], kind="stable")
        .reset_index(drop=True)
    )


def test_precisao_e_razao_de_somas_na_janela_nunca_media_de_razoes() -> None:
    """CA-01/CA-03: cada precisão da luta N é ``sum(landed)/sum(attempted)`` das anteriores.

    Os valores da fixture fazem a razão de somas divergir da média de razões por luta em
    todas as oito dimensões -- se alguém trocar a fórmula, este teste fica vermelho.
    """
    a = _fighter_a(add_recent_form_features(_bloco_a_frame()))

    terceira = a.iloc[2]
    assert terceira[SIG_STRIKE_ACCURACY_R3] == pytest.approx(11 / 41)
    assert terceira[HEAD_ACCURACY_R3] == pytest.approx(7 / 31)
    assert terceira[BODY_ACCURACY_R3] == pytest.approx(4 / 10)
    assert terceira[LEG_ACCURACY_R3] == pytest.approx(1 / 3)
    assert terceira[DISTANCE_ACCURACY_R3] == pytest.approx(9 / 33)
    assert terceira[CLINCH_ACCURACY_R3] == pytest.approx(1 / 5)
    assert terceira[GROUND_ACCURACY_R3] == pytest.approx(1 / 6)
    assert terceira[TAKEDOWN_ACCURACY_R3] == pytest.approx(2 / 10)
    # A média de razões daria 0,625 na significativa -- a fórmula errada não passa aqui.
    assert terceira[SIG_STRIKE_ACCURACY_R3] != pytest.approx(0.625)


def test_precisao_da_segunda_luta_usa_so_a_primeira() -> None:
    """CA-01: point-in-time -- a precisão da 2a luta é exatamente a da 1a, sem a corrente."""
    a = _fighter_a(add_recent_form_features(_bloco_a_frame()))

    segunda = a.iloc[1]
    assert segunda[SIG_STRIKE_ACCURACY_R3] == pytest.approx(1.0)
    assert segunda[BODY_ACCURACY_R3] == pytest.approx(0.5)
    assert segunda[TAKEDOWN_ACCURACY_R3] == pytest.approx(1.0)
    # Estreia sem histórico: ausência explícita em todas as oito precisões.
    for coluna in _PRECISAO_FEATURES:
        assert pd.isna(a[coluna].iloc[0])
    assert set(_PRECISAO_FEATURES).issubset(set(RECENT_FORM_FEATURES))


def test_poder_knockdowns_por_minuto_e_media_por_luta_na_janela() -> None:
    """CA-01/CA-03: ``knockdowns_pm_r3`` é soma/soma de minutos; ``knockdowns_avg_r3``, média.

    A 1a luta de A durou 2 minutos com 1 knockdown; a 2a, 15 minutos com nenhum. Na 3a luta
    a taxa por minuto é ``1/17`` (razão de somas), enquanto a média por luta é ``0,5`` --
    números que separam as duas fórmulas e impedem trocá-las uma pela outra.
    """
    a = _fighter_a(add_recent_form_features(_bloco_a_frame()))

    terceira = a.iloc[2]
    assert terceira[KNOCKDOWNS_PM_R3] == pytest.approx(1 / 17)
    assert terceira[KNOCKDOWNS_AVG_R3] == pytest.approx(0.5)
    # A 2a luta enxerga só a 1a: 1 knockdown em 2 minutos.
    assert a[KNOCKDOWNS_PM_R3].iloc[1] == pytest.approx(0.5)
    assert a[KNOCKDOWNS_AVG_R3].iloc[1] == pytest.approx(1.0)
    # Estreia: ausência explícita nas duas.
    assert pd.isna(a[KNOCKDOWNS_PM_R3].iloc[0])
    assert pd.isna(a[KNOCKDOWNS_AVG_R3].iloc[0])
    assert {KNOCKDOWNS_PM_R3, KNOCKDOWNS_AVG_R3}.issubset(set(RECENT_FORM_FEATURES))


def test_ameaca_de_grappling_e_trabalho_fora_do_significativo() -> None:
    """CA-01/CA-03: média de tentativas de finalização e razão total/significativo na janela.

    ``submission_attempts_avg_r3`` é média por luta; ``total_to_sig_strike_ratio_r3`` é razão
    de somas (``65/11``), que difere da média das razões por luta (``5,5``) -- de novo os
    números separam as duas fórmulas.
    """
    a = _fighter_a(add_recent_form_features(_bloco_a_frame()))

    terceira = a.iloc[2]
    assert terceira[SUBMISSION_ATTEMPTS_AVG_R3] == pytest.approx(1.5)
    assert terceira[TOTAL_TO_SIG_STRIKE_RATIO_R3] == pytest.approx(65 / 11)
    assert terceira[TOTAL_TO_SIG_STRIKE_RATIO_R3] != pytest.approx(5.5)
    assert pd.isna(a[SUBMISSION_ATTEMPTS_AVG_R3].iloc[0])
    assert pd.isna(a[TOTAL_TO_SIG_STRIKE_RATIO_R3].iloc[0])


def test_total_to_sig_ratio_com_luta_anterior_de_total_nulo() -> None:
    """RF-03: o backfill parcial de ``total_strikes_landed`` não anula a feature inteira.

    ``total_strikes_landed`` veio do enriquecimento M5/M7 e não cobre a janela toda como as
    colunas ``_attempted``. Com uma luta anterior sem esse dado, a soma da janela ignora o
    nulo (``min_periods=1``) e a feature continua definida a partir das lutas que têm dado --
    degradação explícita, sem imputação.
    """
    frame = _bloco_a_frame()
    alvo = frame.index[(frame[COL_FIGHTER_ID] == _FIGHTER_A) & (frame[COL_BOUT_ID] == 101)][0]
    frame.loc[alvo, "total_strikes_landed"] = float("nan")

    a = _fighter_a(add_recent_form_features(frame))

    # Só a 2a luta (60 conectados totais / 10 significativos) sobra na janela da 3a.
    assert a[TOTAL_TO_SIG_STRIKE_RATIO_R3].iloc[2] == pytest.approx(60 / 11)


def test_bloco_a_entra_no_conjunto_de_features_de_forma_recente() -> None:
    """As 12 features do bloco A entram em ``RECENT_FORM_FEATURES`` (contrato do pipeline)."""
    bloco_a = [
        *_PRECISAO_FEATURES,
        KNOCKDOWNS_PM_R3,
        KNOCKDOWNS_AVG_R3,
        SUBMISSION_ATTEMPTS_AVG_R3,
        TOTAL_TO_SIG_STRIKE_RATIO_R3,
    ]

    assert len(bloco_a) == 12
    assert set(bloco_a).issubset(set(RECENT_FORM_FEATURES))


def test_denominador_zero_do_bloco_a_produz_ausencia_nunca_zero_nem_inf() -> None:
    """CA-02 (RF-03): sem tentativa, sem minuto e sem golpe significativo anteriores -> nulo.

    A 1a luta do lutador não registrou tentativa alguma, terminou com zero minuto de
    cativeiro e não conectou golpe significativo: na 2a luta as oito precisões,
    ``knockdowns_pm_r3`` e ``total_to_sig_strike_ratio_r3`` têm denominador zero e precisam
    ser **ausentes** -- nunca ``0`` (que o modelo leria como "erra tudo"), nunca ``inf`` (que
    não é JSON válido e quebraria o JSONB), nunca imputação.

    A asserção usa ``pd.isna`` (``True`` para ``NaN`` **e** ``pd.NA``, ``False`` para
    ``inf``), porque as colunas cruas nullable do Postgres podem chegar como ``Int64``.
    """
    rows = [
        _participation(
            fighter_id=_FIGHTER_A,
            bout_id=301,
            event_day=1,
            result="win",
            method=BoutMethod.KO_TKO,
            fight_round=1,
            ending=0,
            sig_landed=0,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
            knockdowns=2,
            total_strikes_landed=7,
        ),
        _participation(
            fighter_id=2,
            bout_id=301,
            event_day=1,
            result="loss",
            method=BoutMethod.KO_TKO,
            fight_round=1,
            ending=0,
            sig_landed=0,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
        ),
        _participation(
            fighter_id=_FIGHTER_A,
            bout_id=302,
            event_day=2,
            result="win",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=50,
            sig_attempted=100,
            takedowns_landed=1,
            takedowns_attempted=2,
            control=100,
        ),
        _participation(
            fighter_id=3,
            bout_id=302,
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

    segunda = _fighter_a(add_recent_form_features(frame)).iloc[1]

    for coluna in (*_PRECISAO_FEATURES, KNOCKDOWNS_PM_R3, TOTAL_TO_SIG_STRIKE_RATIO_R3):
        assert pd.isna(segunda[coluna]), coluna
    # A média por luta não tem denominador de dado: 2 knockdowns numa luta anterior = 2,0.
    assert segunda[KNOCKDOWNS_AVG_R3] == pytest.approx(2.0)


def _formato_frame(formatos: list[int | None]) -> pd.DataFrame:
    """Frame do lutador A com uma luta por formato agendado, em ordem cronológica.

    Cada luta traz só o canto de A -- as features de formato não dependem do adversário, e o
    pareamento do canto oposto já é exercitado pelos testes de striking/grappling.
    """
    rows = [
        _participation(
            fighter_id=_FIGHTER_A,
            bout_id=200 + indice,
            event_day=indice + 1,
            result="win",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=10,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
            scheduled_rounds=formato,
        )
        for indice, formato in enumerate(formatos)
    ]
    return (
        pd.DataFrame(rows)
        .sort_values(by=[COL_FIGHTER_ID, "event_date", COL_BOUT_ID], kind="stable")
        .reset_index(drop=True)
    )


def test_five_round_bouts_before_conta_so_as_lutas_anteriores() -> None:
    """CA-05 (RF-01): a contagem da luta N usa apenas as lutas 1..N-1 do lutador.

    Formatos ``[5, 3, 5, 3]``: a 4a luta viu duas de cinco rounds antes dela. A 3a viu uma --
    a de cinco rounds que é a **própria** 3a não entra na conta dela mesma.
    """
    out = add_recent_form_features(_formato_frame([5, 3, 5, 3]))
    a = _fighter_a(out)

    assert a[FIVE_ROUND_BOUTS_BEFORE].tolist()[1:] == pytest.approx([1.0, 1.0, 2.0])
    assert pd.isna(a[FIVE_ROUND_BOUTS_BEFORE].iloc[0])


def test_five_round_bouts_before_e_identica_com_e_sem_as_lutas_futuras() -> None:
    """CA-05 (RF-01): point-in-time linha a linha -- lutas futuras não mudam a luta N.

    A prova direta do anti-leakage: recalcular a feature sobre a frame truncada na luta N
    tem de dar exatamente o mesmo valor que sobre a frame inteira.
    """
    completa = _formato_frame([5, 3, 5, 3, 5])
    truncada = completa.iloc[:3].copy()

    valor_completa = add_recent_form_features(completa)[FIVE_ROUND_BOUTS_BEFORE].iloc[2]
    valor_truncada = add_recent_form_features(truncada)[FIVE_ROUND_BOUTS_BEFORE].iloc[2]

    assert valor_completa == pytest.approx(valor_truncada)
    assert valor_completa == pytest.approx(1.0)


def test_five_round_bouts_before_zero_legitimo_e_distinto_da_estreia() -> None:
    """CA-08 (RF-03): zero (tem histórico, nunca fez cinco rounds) não é nulo (estreia).

    A distinção é o coração da feature: "já passou por vinte e cinco minutos" e "nunca teve a
    chance" são estados diferentes, e colapsá-los em zero apagaria justamente o sinal que o
    bloco B2 existe para medir.
    """
    a = _fighter_a(add_recent_form_features(_formato_frame([3, 3, 3, 3])))

    assert pd.isna(a[FIVE_ROUND_BOUTS_BEFORE].iloc[0])
    assert a[FIVE_ROUND_BOUTS_BEFORE].tolist()[1:] == pytest.approx([0.0, 0.0, 0.0])


def test_five_round_bouts_before_formato_desconhecido_nao_vira_zero() -> None:
    """CA-08 (RF-03): anterior sem formato registrado é ausência, nunca "não foi cinco".

    ``scheduled_rounds`` está preenchido em 93,1% do banco. Contar o nulo como "não foi de
    cinco rounds" seria **imputação**, proibida pela RF-03: afirmaria sobre a luta um formato
    que ninguém registrou. Enquanto nenhuma anterior for conhecida a contagem é nula; assim
    que uma for, a contagem passa a existir sobre as conhecidas.
    """
    a = _fighter_a(add_recent_form_features(_formato_frame([None, None, 5, 3])))

    assert pd.isna(a[FIVE_ROUND_BOUTS_BEFORE].iloc[0])  # estreia
    assert pd.isna(a[FIVE_ROUND_BOUTS_BEFORE].iloc[1])  # única anterior é desconhecida
    assert pd.isna(a[FIVE_ROUND_BOUTS_BEFORE].iloc[2])  # as duas anteriores, desconhecidas
    # A 4a luta tem a 3a (de cinco rounds) como anterior conhecida -> contagem definida.
    assert a[FIVE_ROUND_BOUTS_BEFORE].iloc[3] == pytest.approx(1.0)


def test_five_round_bouts_before_entra_no_conjunto_de_features_de_forma_recente() -> None:
    """A feature do bloco B2 entra em ``RECENT_FORM_FEATURES`` (contrato do pipeline)."""
    assert FIVE_ROUND_BOUTS_BEFORE in RECENT_FORM_FEATURES


# --- Bloco C da SPEC 009: histórico qualificado (como perde, quilometragem, tendência) ---


def _historico_frame(
    desfechos: list[tuple[str, BoutMethod | None]],
    *,
    duracoes: list[tuple[int, int]] | None = None,
) -> pd.DataFrame:
    """Frame do lutador A com uma luta por desfecho ``(resultado, método)``, cronológica.

    Só o canto de A: as contagens de derrota por método e a quilometragem não dependem do
    adversário, e o pareamento do canto oposto já é exercitado pelos testes de striking.
    Sem ``duracoes``, toda luta dura três rounds cheios (quinze minutos), o que isola o que
    está sob prova; com ela, cada luta recebe o par ``(round, ending_time_seconds)``.
    """
    formatos = duracoes if duracoes is not None else [(3, 300)] * len(desfechos)
    rows = [
        _participation(
            fighter_id=_FIGHTER_A,
            bout_id=400 + indice,
            event_day=indice + 1,
            result=resultado,
            method=metodo,
            fight_round=formatos[indice][0],
            ending=formatos[indice][1],
            sig_landed=10,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=0,
        )
        for indice, (resultado, metodo) in enumerate(desfechos)
    ]
    return (
        pd.DataFrame(rows)
        .sort_values(by=[COL_FIGHTER_ID, "event_date", COL_BOUT_ID], kind="stable")
        .reset_index(drop=True)
    )


# V(KO), D(KO), D(finalização), D(decisão), V(decisão). Cobre de uma vez: vitória por
# nocaute não conta como derrota, as duas contagens sobem de forma independente e a derrota
# por decisão não entra em nenhuma das duas (a SPEC proíbe categoria própria para ela).
_HISTORICO_DE_DERROTAS: list[tuple[str, BoutMethod | None]] = [
    ("win", BoutMethod.KO_TKO),
    ("loss", BoutMethod.KO_TKO),
    ("loss", BoutMethod.SUBMISSION),
    ("loss", BoutMethod.DECISION),
    ("win", BoutMethod.DECISION),
]


def test_derrotas_por_ko_e_por_finalizacao_contam_so_as_lutas_anteriores() -> None:
    """CA-01 (RF-01): as contagens da luta N usam apenas as lutas 1..N-1 do lutador.

    O argumento de domínio do C1 é "já foi nocauteado N vezes, o queixo não se recupera" --
    por isso é **contagem**, não taxa: a normalização por carreira o modelo obtém de
    ``career_bouts_before``, que já é feature. Uma vitória por nocaute não incrementa nada, e
    a derrota por decisão não entra em nenhuma das duas.
    """
    a = _fighter_a(add_recent_form_features(_historico_frame(_HISTORICO_DE_DERROTAS)))

    assert a[KO_LOSSES_PRIOR].tolist()[1:] == pytest.approx([0.0, 1.0, 1.0, 1.0])
    assert a[SUBMISSION_LOSSES_PRIOR].tolist()[1:] == pytest.approx([0.0, 0.0, 1.0, 1.0])


def test_derrotas_por_ko_estreia_e_nula_e_zero_e_legitimo_na_segunda_luta() -> None:
    """CA-02 (RF-03): ``0`` é histórico sem esse tipo de derrota; ``NaN`` é a estreia.

    A distinção é o coração do C1: "nunca foi nocauteado em três lutas" e "nunca lutou" são
    estados diferentes, e colapsá-los em zero apagaria justamente o sinal medido.
    """
    a = _fighter_a(
        add_recent_form_features(
            _historico_frame([("loss", BoutMethod.DECISION), ("win", BoutMethod.DECISION)])
        )
    )

    assert pd.isna(a[KO_LOSSES_PRIOR].iloc[0])
    assert pd.isna(a[SUBMISSION_LOSSES_PRIOR].iloc[0])
    assert a[KO_LOSSES_PRIOR].iloc[1] == pytest.approx(0.0)
    assert a[SUBMISSION_LOSSES_PRIOR].iloc[1] == pytest.approx(0.0)


def test_derrota_com_metodo_desconhecido_nao_incrementa_nenhuma_contagem() -> None:
    """Degradação registrada: método nulo é "não-KO e não-finalização", não um terceiro estado.

    Coerente com ``finish_rate_prior``, que já trata método ausente como não-finalização. A
    contagem permanece definida (o histórico existe), apenas não sobe.
    """
    a = _fighter_a(
        add_recent_form_features(
            _historico_frame([("loss", None), ("loss", BoutMethod.KO_TKO), ("win", None)])
        )
    )

    assert a[KO_LOSSES_PRIOR].tolist()[1:] == pytest.approx([0.0, 1.0])
    assert a[SUBMISSION_LOSSES_PRIOR].tolist()[1:] == pytest.approx([0.0, 0.0])


def test_derrotas_por_ko_sao_identicas_com_e_sem_as_lutas_futuras() -> None:
    """CA-01 (RF-01): point-in-time linha a linha -- lutas futuras não mudam a luta N.

    A prova direta do anti-leakage: recalcular sobre a frame truncada na luta N tem de dar
    exatamente o mesmo valor que sobre a frame inteira.
    """
    completa = _fighter_a(add_recent_form_features(_historico_frame(_HISTORICO_DE_DERROTAS)))
    truncada = _fighter_a(add_recent_form_features(_historico_frame(_HISTORICO_DE_DERROTAS[:3])))

    for coluna in (KO_LOSSES_PRIOR, SUBMISSION_LOSSES_PRIOR):
        assert completa[coluna].iloc[2] == pytest.approx(truncada[coluna].iloc[2]), coluna
    assert completa[KO_LOSSES_PRIOR].iloc[2] == pytest.approx(1.0)
    assert completa[SUBMISSION_LOSSES_PRIOR].iloc[2] == pytest.approx(0.0)


def test_nomes_do_bloco_c_nao_colidem_com_as_bases_proscritas() -> None:
    """CA-07 (RF-10): nenhum nome do bloco C é média de carreira proscrita do snapshot 2025.

    ``splm``, ``str_acc``, ``sapm``, ``td_avg``... embutem o resultado da luta que se quer
    prever (ADR 0002). As gêmeas de carreira do C3 são justamente médias de carreira -- só
    que **point-in-time** e nunca emitidas; um nome parecido com o das proscritas confundiria
    leitor e guarda, por isso a checagem cobre também as variantes de canto.
    """
    for nome in _BLOCO_C_BASES:
        assert nome not in PROSCRIBED_FEATURE_BASES, nome
        for sufixo in ("", "_a", "_b", "_diff"):
            coluna = f"{nome}{sufixo}"
            assert coluna not in {
                f"{proscrita}{s}"
                for proscrita in PROSCRIBED_FEATURE_BASES
                for s in ("", "_a", "_b", "_diff")
            }, coluna


def test_career_minutes_before_soma_os_minutos_das_lutas_anteriores() -> None:
    """CA-01/CA-02 (RF-01/RF-03): a quilometragem da luta N soma só as lutas 1..N-1.

    Minutos de cada luta de A em ``_known_history_frame``: 2, 15, 6, 15, 1
    (``(round - 1) * 5 + ending_time_seconds / 60``). A soma expanding é, portanto,
    ``NaN, 2, 17, 23, 38`` -- e é a **carreira inteira**, não a janela de 3 (que na 5a luta
    daria 36). ``NaN`` na estreia: não há luta anterior de onde tirar cativeiro.
    """
    a = _fighter_a(add_recent_form_features(_known_history_frame()))

    assert pd.isna(a[CAREER_MINUTES_BEFORE].iloc[0])
    assert a[CAREER_MINUTES_BEFORE].tolist()[1:] == pytest.approx([2.0, 17.0, 23.0, 38.0])


def test_career_minutes_before_distingue_quilometragem_de_numero_de_lutas() -> None:
    """CA-03: vinte decisões e vinte nocautes no 1o round dão ``career_bouts_before`` igual.

    A tese do bloco C2 em forma executável: ``career_bouts_before`` (M4) trata as duas
    carreiras como idênticas -- vinte lutas anteriores nas duas --, enquanto a quilometragem
    separa trezentos minutos de cativeiro de vinte. É exatamente a dimensão que faltava.
    """
    decisoes = _historico_frame([("win", BoutMethod.DECISION)] * 21, duracoes=[(3, 300)] * 21)
    nocautes = _historico_frame([("win", BoutMethod.KO_TKO)] * 21, duracoes=[(1, 60)] * 21)

    ultima_decisoes = add_experience(add_recent_form_features(decisoes)).iloc[20]
    ultima_nocautes = add_experience(add_recent_form_features(nocautes)).iloc[20]

    assert ultima_decisoes[CAREER_BOUTS_BEFORE] == ultima_nocautes[CAREER_BOUTS_BEFORE] == 20
    assert ultima_decisoes[CAREER_MINUTES_BEFORE] == pytest.approx(20 * 15.0)
    assert ultima_nocautes[CAREER_MINUTES_BEFORE] == pytest.approx(20 * 1.0)


def test_career_minutes_before_e_identica_com_e_sem_as_lutas_futuras() -> None:
    """CA-01 (RF-01): point-in-time linha a linha -- lutas futuras não mudam a luta N."""
    completa = _known_history_frame()
    truncada = completa.loc[completa[COL_BOUT_ID] <= 103].copy()
    idx = completa.index[(completa[COL_FIGHTER_ID] == _FIGHTER_A) & (completa[COL_BOUT_ID] == 103)][
        0
    ]

    valor_completa = add_recent_form_features(completa)[CAREER_MINUTES_BEFORE].loc[idx]
    valor_truncada = add_recent_form_features(truncada)[CAREER_MINUTES_BEFORE].loc[idx]

    assert valor_completa == pytest.approx(valor_truncada)
    assert valor_completa == pytest.approx(17.0)


_TENDENCIAS = [
    SIG_STRIKES_LANDED_PM_TREND,
    SIG_STRIKES_ABSORBED_PM_TREND,
    TAKEDOWNS_LANDED_AVG_TREND,
    TAKEDOWN_DEFENSE_TREND,
    CONTROL_TIME_AVG_TREND,
]


def _tendencia_frame(
    sig_por_luta: list[int], *, controles: list[float] | None = None
) -> pd.DataFrame:
    """Frame do lutador A com lutas de mesma duração e golpe conectado variável.

    Quinze minutos por luta (três rounds cheios) em todas: com a duração fixa, a diferença
    entre a janela de 3 e a carreira vem só do golpe conectado, que é o que o teste de sinal
    precisa isolar. ``controles`` permite injetar tempo de controle ausente (``NaN``) para
    exercitar a ponta nula das tendências.
    """
    tempos = controles if controles is not None else [0.0] * len(sig_por_luta)
    rows = [
        _participation(
            fighter_id=_FIGHTER_A,
            bout_id=500 + indice,
            event_day=indice + 1,
            result="win",
            method=BoutMethod.DECISION,
            fight_round=3,
            ending=300,
            sig_landed=sig,
            takedowns_landed=0,
            takedowns_attempted=0,
            control=tempos[indice],
        )
        for indice, sig in enumerate(sig_por_luta)
    ]
    return (
        pd.DataFrame(rows)
        .sort_values(by=[COL_FIGHTER_ID, "event_date", COL_BOUT_ID], kind="stable")
        .reset_index(drop=True)
    )


def test_tendencia_e_a_diferenca_entre_a_janela_de_tres_e_a_carreira() -> None:
    """CA-05: cada ``*_trend`` vale ``<base>_r3 - <base>_carreira``, linha a linha.

    Na 5a luta de A a janela cobre b2..b4 e a carreira cobre b1..b4 -- as duas pontas
    **divergem**, que é a condição sem a qual o teste passaria com tendência sempre zero.
    Minutos: b1=2, b2=15, b3=6, b4=15 (janela 36, carreira 38).
    """
    a = _fighter_a(add_recent_form_features(_known_history_frame()))
    quinta = a.iloc[4]

    assert quinta[SIG_STRIKES_LANDED_PM_TREND] == pytest.approx(85 / 36 - 115 / 38)
    assert quinta[SIG_STRIKES_ABSORBED_PM_TREND] == pytest.approx(70 / 36 - 80 / 38)
    assert quinta[TAKEDOWNS_LANDED_AVG_TREND] == pytest.approx(4 / 3 - 5 / 4)
    assert quinta[TAKEDOWN_DEFENSE_TREND] == pytest.approx((1 - 3 / 8) - (1 - 3 / 10))
    assert quinta[CONTROL_TIME_AVG_TREND] == pytest.approx(160 / 3 - 55.0)


def test_tendencia_e_exatamente_zero_quando_a_janela_cobre_a_carreira_inteira() -> None:
    """CA-05: com três lutas anteriores, janela e carreira coincidem -- tendência nula.

    Amarra a gêmea de carreira à **mesma fórmula** da ponta recente: se ela fosse calculada
    de outro jeito (média de razões em vez de razão de somas, por exemplo), a diferença não
    daria zero justamente onde os dois conjuntos de lutas são idênticos.
    """
    quarta = _fighter_a(add_recent_form_features(_known_history_frame())).iloc[3]

    for tendencia in _TENDENCIAS:
        assert quarta[tendencia] == pytest.approx(0.0), tendencia


def test_sinal_da_tendencia_e_positivo_em_ascensao_e_negativo_em_declinio() -> None:
    """CA-05: forma recente acima da carreira -> positivo; abaixo -> negativo.

    A convenção de sinal é o que torna a feature legível ("está subindo" vs. "está caindo") e
    é a razão de a diferença ser ``r3 - carreira``, não o contrário.
    """
    ascensao = _fighter_a(add_recent_form_features(_tendencia_frame([10, 20, 30, 40, 50])))
    declinio = _fighter_a(add_recent_form_features(_tendencia_frame([50, 40, 30, 20, 10])))

    assert ascensao[SIG_STRIKES_LANDED_PM_TREND].iloc[4] == pytest.approx(2.0 - 100 / 60)
    assert ascensao[SIG_STRIKES_LANDED_PM_TREND].iloc[4] > 0
    assert declinio[SIG_STRIKES_LANDED_PM_TREND].iloc[4] == pytest.approx(2.0 - 140 / 60)
    assert declinio[SIG_STRIKES_LANDED_PM_TREND].iloc[4] < 0


def test_tendencia_com_ponta_recente_nula_e_carreira_definida_permanece_nula() -> None:
    """CA-02 (RF-03): qualquer ponta nula anula a tendência -- nunca vira a outra ponta.

    Cenário assimétrico de propósito: só a 1a luta tem tempo de controle registrado, então na
    5a luta a **carreira** está definida (55 segundos, média sobre a única conhecida) e a
    **janela de 3** (lutas 2..4, todas ausentes) é nula. Sem a propagação do ``NaN`` a
    subtração viraria ``0 - 55``, inventando um declínio que o dado não afirma.
    """
    nan = float("nan")
    a = _fighter_a(
        add_recent_form_features(
            _tendencia_frame([10, 10, 10, 10, 10], controles=[55.0, nan, nan, nan, nan])
        )
    )
    quinta = a.iloc[4]

    assert pd.isna(quinta[CONTROL_TIME_AVG_R3])
    assert pd.isna(quinta[CONTROL_TIME_AVG_TREND])
    # A ponta de carreira existe (é intermediária, não emitida) -- a prova indireta é que a
    # tendência da 2a luta, onde as duas pontas cobrem só a 1a, é exatamente zero.
    assert a[CONTROL_TIME_AVG_TREND].iloc[1] == pytest.approx(0.0)


def test_tendencia_com_denominador_zero_e_nula_nunca_zero_nem_inf() -> None:
    """CA-02 (RF-03): denominador zero nas gêmeas de carreira vira ``NaN``, jamais ``inf``.

    ``inf`` não é JSON válido e quebraria a materialização em JSONB. Aqui nenhuma queda é
    tentada contra A em nenhuma luta, então a defesa de queda é indefinida nas duas pontas --
    e a tendência precisa ser ausência, não ``0.0`` (que o modelo leria como "estável").
    """
    quinta = _fighter_a(add_recent_form_features(_tendencia_frame([10, 20, 30, 40, 50]))).iloc[4]

    assert pd.isna(quinta[TAKEDOWN_DEFENSE_TREND])
    assert quinta[TAKEDOWN_DEFENSE_TREND] != float("inf")


def test_gemeas_de_carreira_nao_sao_emitidas_como_coluna() -> None:
    """CA-04 (risco declarado da sprint): só os cinco ``*_trend`` saem; as gêmeas ficam locais.

    Emitir as gêmeas de carreira dobraria as colunas do bloco e o faria medir "carreira +
    tendência" em vez de tendência -- dois blocos acoplados num veredito só, o oposto do que
    a RF-06 exige. O precedente do projeto para métrico local que nunca vira coluna é
    ``_round1_share_by_participation``.
    """
    entrada = _known_history_frame()
    saida = add_recent_form_features(entrada)

    assert set(saida.columns) - set(entrada.columns) == set(RECENT_FORM_FEATURES)
    assert not [coluna for coluna in saida.columns if str(coluna).endswith("_carreira")]
    for tendencia in _TENDENCIAS:
        base = tendencia.removesuffix("_trend")
        assert base not in saida.columns, base
        assert f"{base}_r3" in saida.columns, base


def test_tendencias_sao_identicas_com_e_sem_as_lutas_futuras() -> None:
    """CA-01 (RF-01): point-in-time linha a linha -- lutas futuras não mudam a luta N.

    Vale para as duas pontas: se a gêmea de carreira esquecesse o ``shift(1)``, a luta N
    entraria na própria média e este teste ficaria vermelho.
    """
    completa = _known_history_frame()
    truncada = completa.loc[completa[COL_BOUT_ID] <= 104].copy()
    idx = completa.index[(completa[COL_FIGHTER_ID] == _FIGHTER_A) & (completa[COL_BOUT_ID] == 104)][
        0
    ]

    pd.testing.assert_series_equal(
        add_recent_form_features(completa).loc[idx][_TENDENCIAS],
        add_recent_form_features(truncada).loc[idx][_TENDENCIAS],
    )
