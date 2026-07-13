"""Features de forma recente (rolling/expanding) point-in-time sobre a frame longa.

Núcleo da Slice 02 da SPEC 005 (M4 -- prontidão preditiva). ``add_recent_form_features``
enriquece a frame longa por lutador-luta da Slice 01 com features acumuladas de carreira
(expanding) e de forma recente (rolling de janela ``WINDOW_RECENT``), todas calculadas
**point-in-time**: usam apenas as lutas 1..N-1 do mesmo lutador, nunca a luta corrente nem
lutas futuras. O mecanismo anti-leakage é ``shift(1)`` dentro do grupo do lutador antes de
todo ``rolling``/``expanding`` -- a corretude temporal é o requisito mais crítico da feature.

Na **estreia** de cada lutador todas as features são ``NaN`` explícito (decisão #3 da SPEC:
sem sentinela, sem ``fillna`` -- a imputação é decisão da fase 2). Absorvido/defesa de queda
vêm do canto oposto da **mesma** luta (pareamento por ``bout_id``), entrando nas features só
depois via ``shift(1)`` -- enriquecimento intra-luta, não vazamento temporal.

O DataFrame do Pandas é fronteira dinâmica (``pyproject.toml`` marca ``pandas.*`` como
``follow_imports=skip``): as funções públicas recebem/devolvem ``pd.DataFrame`` tipado. Os
nomes de coluna de entrada são centralizados nas constantes ``COL_*`` (fonte única do
mapeamento com o contrato da Slice 01).
"""

from __future__ import annotations

import logging

import pandas as pd

from apps.bouts.enums import BoutMethod

logger = logging.getLogger(__name__)

# Janela rolling curta: últimas 3 lutas (decisão #2 da SPEC confirmada no Plano 005-02).
WINDOW_RECENT: int = 3

# Minutos por round completo no UFC (base do cálculo de ``fight_minutes``).
_ROUND_MINUTES: int = 5

# Colunas de entrada esperadas na frame longa (Slice 01). Centralizadas aqui como fonte
# única do mapeamento com o contrato da Sprint 01 -- nenhuma string mágica espalhada.
COL_FIGHTER_ID = "fighter_id"
COL_BOUT_ID = "bout_id"
COL_RESULT = "result"
COL_METHOD = "method"
COL_ROUND = "round"
COL_ENDING_TIME_SECONDS = "ending_time_seconds"
COL_SIG_STRIKES_LANDED = "sig_strikes_landed"
COL_ROUND_NUMBER = "round"
COL_TAKEDOWNS_LANDED = "takedowns_landed"
COL_TAKEDOWNS_ATTEMPTED = "takedowns_attempted"
COL_CONTROL_TIME_SECONDS = "control_time_seconds"
# Splits de golpe conectado (M5 Sprint 02), base do perfil de striking (Slice 06).
COL_HEAD_LANDED = "head_landed"
COL_BODY_LANDED = "body_landed"
COL_LEG_LANDED = "leg_landed"
COL_DISTANCE_LANDED = "distance_landed"
COL_CLINCH_LANDED = "clinch_landed"
COL_GROUND_LANDED = "ground_landed"

# Valores da coluna ``result`` (ver ``ingestion.features.long_frame.BoutResult``).
_RESULT_WIN = "win"
_RESULT_LOSS = "loss"

# Métodos que caracterizam uma finalização (para o ``finish_rate``).
_FINISH_METHODS: frozenset[str] = frozenset({BoutMethod.KO_TKO.value, BoutMethod.SUBMISSION.value})

# Colunas de feature produzidas por linha (expanding de carreira + rolling de janela 3).
WIN_RATE_PRIOR = "win_rate_prior"
FINISH_RATE_PRIOR = "finish_rate_prior"
WIN_STREAK_PRIOR = "win_streak_prior"
SIG_STRIKES_LANDED_PM_R3 = "sig_strikes_landed_pm_r3"
SIG_STRIKES_ABSORBED_PM_R3 = "sig_strikes_absorbed_pm_r3"
TAKEDOWNS_LANDED_AVG_R3 = "takedowns_landed_avg_r3"
TAKEDOWN_DEFENSE_R3 = "takedown_defense_r3"
CONTROL_TIME_AVG_R3 = "control_time_avg_r3"
# Perfil de striking point-in-time (M5 Slice 06): distribuição do golpe conectado por
# alvo (cabeça/corpo/perna) e por posição (distância/clinch/solo), sobre a janela de 3.
SHARE_HEAD_R3 = "share_head_r3"
SHARE_BODY_R3 = "share_body_r3"
SHARE_LEG_R3 = "share_leg_r3"
SHARE_DISTANCE_R3 = "share_distance_r3"
SHARE_CLINCH_R3 = "share_clinch_r3"
SHARE_GROUND_R3 = "share_ground_r3"
# Dinâmica por round point-in-time (M5 Slice 06): fração dos golpes conectados no round 1
# sobre o total do bout, agregada nas lutas anteriores. Depende do round-a-round da Cito
# (``bout_fighter_rounds``); degrada explicitamente para ``NaN`` quando ausente.
ROUND1_SIG_STRIKE_SHARE_R3 = "round1_sig_strike_share_r3"

RECENT_FORM_FEATURES: list[str] = [
    WIN_RATE_PRIOR,
    FINISH_RATE_PRIOR,
    WIN_STREAK_PRIOR,
    SIG_STRIKES_LANDED_PM_R3,
    SIG_STRIKES_ABSORBED_PM_R3,
    TAKEDOWNS_LANDED_AVG_R3,
    TAKEDOWN_DEFENSE_R3,
    CONTROL_TIME_AVG_R3,
    SHARE_HEAD_R3,
    SHARE_BODY_R3,
    SHARE_LEG_R3,
    SHARE_DISTANCE_R3,
    SHARE_CLINCH_R3,
    SHARE_GROUND_R3,
]

# Features de dinâmica por round (conjunto mínimo -- YAGNI): dependem de round_stats
# (``load_round_stats``), diferente das demais features (só a frame longa).
ROUND_DYNAMICS_FEATURES: list[str] = [ROUND1_SIG_STRIKE_SHARE_R3]


def _prior_expanding_mean(values: pd.Series, group: pd.Series) -> pd.Series:
    """Média acumulada das lutas anteriores por grupo: ``shift(1).expanding().mean()``.

    O ``shift(1)`` exclui a luta corrente (point-in-time); ``expanding().mean()`` ignora os
    ``NaN`` (valores fora do denominador, ex.: no contest/empate no win rate). Na estreia o
    ``shift`` produz ``NaN`` e a média sobre ``[NaN]`` permanece ``NaN``.
    """
    return values.groupby(group).transform(lambda s: s.shift(1).expanding().mean())


def _prior_rolling_sum(values: pd.Series, group: pd.Series) -> pd.Series:
    """Soma das lutas anteriores na janela ``WINDOW_RECENT`` por grupo (``min_periods=1``).

    ``shift(1)`` exclui a luta corrente; ``min_periods=1`` faz a soma existir a partir de 1
    luta anterior. Na estreia a janela contém só o ``NaN`` do shift -> resultado ``NaN``.
    """
    return values.groupby(group).transform(
        lambda s: s.shift(1).rolling(WINDOW_RECENT, min_periods=1).sum()
    )


def _prior_rolling_mean(values: pd.Series, group: pd.Series) -> pd.Series:
    """Média das lutas anteriores na janela ``WINDOW_RECENT`` por grupo (``min_periods=1``)."""
    return values.groupby(group).transform(
        lambda s: s.shift(1).rolling(WINDOW_RECENT, min_periods=1).mean()
    )


def _prior_streak(results: pd.Series) -> pd.Series:
    """Streak assinado das lutas decididas anteriores de um lutador (por grupo).

    ``+k`` vitórias consecutivas, ``-k`` derrotas consecutivas até N-1; a mudança de sinal
    reinicia a contagem; no contest/empate são ignorados (não contam nem quebram). Sem
    nenhuma luta decidida anterior (estreia ou só NC/empate antes) o valor é ``NaN``.
    """
    out: list[float] = []
    streak = 0
    for value in results:
        out.append(float(streak) if streak != 0 else float("nan"))
        if value == _RESULT_WIN:
            streak = streak + 1 if streak > 0 else 1
        elif value == _RESULT_LOSS:
            streak = streak - 1 if streak < 0 else -1
    return pd.Series(out, index=results.index)


def _fight_minutes(frame: pd.DataFrame) -> pd.Series:
    """Duração da luta em minutos: ``(round - 1) * 5 + ending_time_seconds / 60``.

    Lutas com ``round`` ou ``ending_time_seconds`` nulos produzem ``NaN`` -- propagado (não
    mascarado) nas taxas por minuto daquela luta.
    """
    return (frame[COL_ROUND] - 1) * _ROUND_MINUTES + frame[COL_ENDING_TIME_SECONDS] / 60.0


def _opponent_stats(frame: pd.DataFrame) -> pd.DataFrame:
    """Stats do canto oposto de cada luta, alinhadas linha a linha à frame de entrada.

    Pareia os dois cantos da **mesma** luta (merge por ``bout_id``, excluindo o próprio
    lutador) para obter, por participação: o striking conectado do adversário (o que o
    lutador absorve) e os takedowns tentados/conectados do adversário (defesa de queda).
    Dados sujos (canto faltante ou mais de um oponente) degradam de forma visível via log.
    """
    keys = frame.reset_index(names="_row_id")[["_row_id", COL_BOUT_ID, COL_FIGHTER_ID]]
    opponent_cols = frame[
        [
            COL_BOUT_ID,
            COL_FIGHTER_ID,
            COL_SIG_STRIKES_LANDED,
            COL_TAKEDOWNS_LANDED,
            COL_TAKEDOWNS_ATTEMPTED,
        ]
    ]
    paired = keys.merge(opponent_cols, on=COL_BOUT_ID, suffixes=("", "_opp"))
    paired = paired[paired[COL_FIGHTER_ID] != paired[f"{COL_FIGHTER_ID}_opp"]]
    duplicated = int(paired["_row_id"].duplicated().sum())
    if duplicated:
        logger.warning(
            "Pareamento de adversário: %d participações com mais de um oponente "
            "(dado sujo -- esperado exatamente 2 cantos por luta); mantendo o primeiro.",
            duplicated,
        )
        paired = paired.drop_duplicates(subset="_row_id", keep="first")
    return paired.set_index("_row_id").reindex(frame.index)


def _add_expanding_features(frame: pd.DataFrame) -> None:
    """Adiciona as features acumuladas de carreira (win rate, finish rate, streak)."""
    fighter = frame[COL_FIGHTER_ID]
    result = frame[COL_RESULT]

    # Vitória=1, derrota=0, no contest/empate=NaN (fora do denominador do win rate).
    won = result.map({_RESULT_WIN: 1.0, _RESULT_LOSS: 0.0})
    frame[WIN_RATE_PRIOR] = _prior_expanding_mean(won, fighter)

    # Finalização = vitória por KO/TKO ou finalização; demais lutas contam no denominador.
    is_finish = (result == _RESULT_WIN) & frame[COL_METHOD].isin(_FINISH_METHODS)
    frame[FINISH_RATE_PRIOR] = _prior_expanding_mean(is_finish.astype(float), fighter)

    frame[WIN_STREAK_PRIOR] = result.groupby(fighter).transform(_prior_streak)


def _safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Razão com denominador zero mapeado para ``NaN`` (nunca ``inf``).

    Um denominador zero (minutos anteriores nulos, tentativas de queda enfrentadas zero)
    torna a taxa indefinida. Sem esta guarda, ``x/0`` com ``x>0`` produziria ``inf`` -- que
    não é JSON válido e quebraria a materialização em JSONB. ``NaN`` explícito é coerente
    com o tratamento da estreia (decisão #3 da SPEC) e vira ``null`` na Slice 05.
    """
    return numerator / denominator.where(denominator != 0)


def _add_rolling_features(frame: pd.DataFrame) -> None:
    """Adiciona as features rolling de janela 3 (striking/grappling/control) point-in-time.

    Taxas por minuto usam razão de somas na janela (``sum(landed)/sum(minutes)``), não média
    de razões por luta -- evita distorção de lutas muito curtas. ``NaN/NaN`` na estreia
    permanece ``NaN``.
    """
    fighter = frame[COL_FIGHTER_ID]
    minutes = _fight_minutes(frame)
    prior_minutes = _prior_rolling_sum(minutes, fighter)
    opponent = _opponent_stats(frame)

    frame[SIG_STRIKES_LANDED_PM_R3] = _safe_ratio(
        _prior_rolling_sum(frame[COL_SIG_STRIKES_LANDED], fighter), prior_minutes
    )
    frame[SIG_STRIKES_ABSORBED_PM_R3] = _safe_ratio(
        _prior_rolling_sum(opponent[COL_SIG_STRIKES_LANDED], fighter), prior_minutes
    )
    frame[TAKEDOWNS_LANDED_AVG_R3] = _prior_rolling_mean(frame[COL_TAKEDOWNS_LANDED], fighter)
    conceded = _prior_rolling_sum(opponent[COL_TAKEDOWNS_LANDED], fighter)
    faced = _prior_rolling_sum(opponent[COL_TAKEDOWNS_ATTEMPTED], fighter)
    frame[TAKEDOWN_DEFENSE_R3] = 1.0 - _safe_ratio(conceded, faced)
    frame[CONTROL_TIME_AVG_R3] = _prior_rolling_mean(frame[COL_CONTROL_TIME_SECONDS], fighter)


def _add_striking_profile_features(frame: pd.DataFrame) -> None:
    """Adiciona as shares de striking point-in-time por alvo e por posição.

    Cada share é a razão de somas na janela (``sum(componente)/sum(total)``) das lutas
    **anteriores** -- não média de razões por luta -- reusando ``_prior_rolling_sum``
    (``shift(1)`` exclui a luta corrente). O denominador de alvo é ``cabeça+corpo+perna``
    conectados; o de posição é ``distância+clinch+solo`` -- assim cada trio soma 1 quando
    definido, e ``_safe_ratio`` mapeia denominador zero (nenhum golpe conectado antes) para
    ``NaN``, nunca ``inf`` (que quebraria o JSONB). Na estreia o ``shift`` produz ``NaN``.
    """
    fighter = frame[COL_FIGHTER_ID]
    head = _prior_rolling_sum(frame[COL_HEAD_LANDED], fighter)
    body = _prior_rolling_sum(frame[COL_BODY_LANDED], fighter)
    leg = _prior_rolling_sum(frame[COL_LEG_LANDED], fighter)
    target_total = head + body + leg
    frame[SHARE_HEAD_R3] = _safe_ratio(head, target_total)
    frame[SHARE_BODY_R3] = _safe_ratio(body, target_total)
    frame[SHARE_LEG_R3] = _safe_ratio(leg, target_total)

    distance = _prior_rolling_sum(frame[COL_DISTANCE_LANDED], fighter)
    clinch = _prior_rolling_sum(frame[COL_CLINCH_LANDED], fighter)
    ground = _prior_rolling_sum(frame[COL_GROUND_LANDED], fighter)
    position_total = distance + clinch + ground
    frame[SHARE_DISTANCE_R3] = _safe_ratio(distance, position_total)
    frame[SHARE_CLINCH_R3] = _safe_ratio(clinch, position_total)
    frame[SHARE_GROUND_R3] = _safe_ratio(ground, position_total)


def add_recent_form_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Devolve cópia da frame longa com as colunas de forma recente point-in-time.

    Assume a frame já ordenada por ``(fighter_id, event_date, bout_id)`` (contrato da
    Slice 01). As features usam apenas as lutas 1..N-1 do mesmo lutador; a estreia produz
    ``NaN`` explícito em todas as features. A frame de entrada não é mutada.
    """
    frame = frame.copy()
    _add_expanding_features(frame)
    _add_rolling_features(frame)
    _add_striking_profile_features(frame)
    return frame


def _round1_share_by_participation(round_stats: pd.DataFrame) -> pd.Series:
    """Fração dos golpes conectados no round 1 sobre o total do bout, por participação.

    Agrega ``round_stats`` (uma linha por canto-por-round) por ``(bout_id, fighter_id)``:
    o numerador é o conectado no round 1, o denominador é a soma de todos os rounds do
    bout. ``_safe_ratio`` mapeia denominador zero para ``NaN`` (nunca ``inf``). Devolve uma
    Série indexada por ``(bout_id, fighter_id)`` -- métrico por-bout, não coluna persistente
    (é desfecho da luta corrente; só entra nas features via agregação as-of).
    """
    keys = [COL_BOUT_ID, COL_FIGHTER_ID]
    total = round_stats.groupby(keys)[COL_SIG_STRIKES_LANDED].sum(min_count=1)
    round1 = (
        round_stats[round_stats[COL_ROUND_NUMBER] == 1]
        .groupby(keys)[COL_SIG_STRIKES_LANDED]
        .sum(min_count=1)
        .reindex(total.index)
    )
    return _safe_ratio(round1, total)


def add_round_dynamics_features(frame: pd.DataFrame, round_stats: pd.DataFrame) -> pd.DataFrame:
    """Devolve cópia da frame longa com a dinâmica por round point-in-time.

    Deriva o métrico por-bout ``round1_sig_strike_share`` (Série **local**, nunca coluna
    persistente -- é desfecho da luta corrente) a partir de ``round_stats`` (de
    ``load_round_stats``) e o agrega point-in-time por lutador (``_prior_rolling_mean``:
    ``shift(1)`` exclui a luta corrente). Um bout sem round-a-round tem métrico ``NaN``, e a
    agregação sobre ``NaN`` permanece ``NaN`` -- degradação explícita, sem imputação (o
    ``HistGradientBoostingClassifier`` trata ``NaN`` nativamente). A frame não é mutada.
    """
    frame = frame.copy()
    if round_stats.empty:
        per_bout = pd.Series(float("nan"), index=frame.index, dtype="float64")
    else:
        share_by_part = _round1_share_by_participation(round_stats).rename("_round1_share")
        merged = frame[[COL_BOUT_ID, COL_FIGHTER_ID]].merge(
            share_by_part.reset_index(), on=[COL_BOUT_ID, COL_FIGHTER_ID], how="left"
        )
        per_bout = pd.Series(merged["_round1_share"].to_numpy(), index=frame.index)
    frame[ROUND1_SIG_STRIKE_SHARE_R3] = _prior_rolling_mean(per_bout, frame[COL_FIGHTER_ID])
    return frame
