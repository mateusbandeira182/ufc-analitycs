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
COL_TAKEDOWNS_LANDED = "takedowns_landed"
COL_TAKEDOWNS_ATTEMPTED = "takedowns_attempted"
COL_CONTROL_TIME_SECONDS = "control_time_seconds"

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

RECENT_FORM_FEATURES: list[str] = [
    WIN_RATE_PRIOR,
    FINISH_RATE_PRIOR,
    WIN_STREAK_PRIOR,
    SIG_STRIKES_LANDED_PM_R3,
    SIG_STRIKES_ABSORBED_PM_R3,
    TAKEDOWNS_LANDED_AVG_R3,
    TAKEDOWN_DEFENSE_R3,
    CONTROL_TIME_AVG_R3,
]


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


def add_recent_form_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Devolve cópia da frame longa com as colunas de forma recente point-in-time.

    Assume a frame já ordenada por ``(fighter_id, event_date, bout_id)`` (contrato da
    Slice 01). As features usam apenas as lutas 1..N-1 do mesmo lutador; a estreia produz
    ``NaN`` explícito em todas as features. A frame de entrada não é mutada.
    """
    frame = frame.copy()
    _add_expanding_features(frame)
    _add_rolling_features(frame)
    return frame
