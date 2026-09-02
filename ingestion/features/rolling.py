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

import numpy as np
import pandas as pd

from apps.bouts.enums import BoutMethod
from apps.fighters.enums import Stance

logger = logging.getLogger(__name__)

# Janela rolling curta: últimas 3 lutas (decisão #2 da SPEC confirmada no Plano 005-02).
WINDOW_RECENT: int = 3

# Minutos por round completo no UFC (base do cálculo de ``fight_minutes``).
_ROUND_MINUTES: int = 5

# Colunas de entrada esperadas na frame longa (Slice 01). Centralizadas aqui como fonte
# única do mapeamento com o contrato da Sprint 01 -- nenhuma string mágica espalhada.
COL_FIGHTER_ID = "fighter_id"
COL_BOUT_ID = "bout_id"
# Data do evento: junto de ``fighter_id`` e ``bout_id``, a chave da ordem canônica da frame
# longa. Mantida igual ao ``trajectory.COL_EVENT_DATE`` (declarada aqui em vez de importada,
# para não acoplar os módulos de feature -- mesmo padrão de ``COL_STANCE``); guardada por teste.
COL_EVENT_DATE = "event_date"
COL_RESULT = "result"
COL_METHOD = "method"
# Round de TÉRMINO da luta na frame longa (uma linha por lutador-luta).
COL_FIGHT_END_ROUND = "round"
COL_ENDING_TIME_SECONDS = "ending_time_seconds"
COL_SIG_STRIKES_LANDED = "sig_strikes_landed"
COL_SIG_STRIKES_ATTEMPTED = "sig_strikes_attempted"
COL_KNOCKDOWNS = "knockdowns"
COL_SUBMISSION_ATTEMPTS = "submission_attempts"
COL_TOTAL_STRIKES_LANDED = "total_strikes_landed"
# NÚMERO do round na frame round-a-round (``load_round_stats``). String distinta de
# ``COL_FIGHT_END_ROUND`` de propósito: as duas frames têm semânticas diferentes de "round" e
# nunca devem colidir caso sejam unidas no futuro.
COL_ROUND_NUMBER = "round_number"
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
# Splits de golpe TENTADO (M5 Sprint 02), base da precisão por dimensão (SPEC 009, bloco A).
COL_HEAD_ATTEMPTED = "head_attempted"
COL_BODY_ATTEMPTED = "body_attempted"
COL_LEG_ATTEMPTED = "leg_attempted"
COL_DISTANCE_ATTEMPTED = "distance_attempted"
COL_CLINCH_ATTEMPTED = "clinch_attempted"
COL_GROUND_ATTEMPTED = "ground_attempted"
# Formato AGENDADO da luta (3 ou 5 rounds), contexto conhecido antes do gongo -- distinto de
# ``COL_FIGHT_END_ROUND``, que é onde a luta acabou (desfecho). Projetada na frame longa
# desde a Slice 02 da SPEC 009; consumida aqui pelo bloco B2 (Slice 03).
COL_SCHEDULED_ROUNDS = "scheduled_rounds"
# Base (guarda) do lutador. **Não** é coluna crua da frame longa: nasce em
# ``trajectory.add_physical_attributes``, que roda DEPOIS de ``add_recent_form_features`` --
# por isso ela só é consumida por ``add_stance_history_features``, encadeada mais tarde.
# Mantida igual ao ``trajectory.STANCE`` (declarada aqui em vez de importada, para não acoplar
# os módulos de feature -- mesmo padrão de ``COL_ROUND_NUMBER``); guardada por teste.
COL_STANCE = "stance"

# Ordem canônica da frame longa (contrato da Slice 01). Reforçada defensivamente em
# ``add_recent_form_features`` -- ver a docstring de ``_similar_style_weighted_prior``.
_SORT_KEY: list[str] = [COL_FIGHTER_ID, COL_EVENT_DATE, COL_BOUT_ID]

# Valores da coluna ``result`` (ver ``ingestion.features.long_frame.BoutResult``).
_RESULT_WIN = "win"
_RESULT_LOSS = "loss"

# Vitória=1, derrota=0, no contest/empate=``NaN`` -- fora do numerador E do denominador de
# toda média de resultado (``win_rate_prior`` e o D3 partilham a convenção).
_WON_BY_RESULT: dict[str, float] = {_RESULT_WIN: 1.0, _RESULT_LOSS: 0.0}

# Formato de cinco rounds: o card principal e as disputas de cinturão. É a diferença entre
# quinze e vinte e cinco minutos de cativeiro -- a experiência que o bloco B2 mede.
_FIVE_ROUND_FORMAT: int = 5

# Métodos que caracterizam uma finalização (para o ``finish_rate``).
_FINISH_METHODS: frozenset[str] = frozenset({BoutMethod.KO_TKO.value, BoutMethod.SUBMISSION.value})

# Bases conhecidas: exatamente os três valores do enum ``Stance``. Rótulo fora dele é
# ignorância sobre a base do lutador, não uma quarta categoria.
_KNOWN_STANCES: frozenset[str] = frozenset(base.value for base in Stance)

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
# Precisão point-in-time por dimensão (SPEC 009, bloco A1): as oito razões
# ``conectado/tentado`` que já estavam persistidas no granular e nunca chegavam ao treino.
SIG_STRIKE_ACCURACY_R3 = "sig_strike_accuracy_r3"
HEAD_ACCURACY_R3 = "head_accuracy_r3"
BODY_ACCURACY_R3 = "body_accuracy_r3"
LEG_ACCURACY_R3 = "leg_accuracy_r3"
DISTANCE_ACCURACY_R3 = "distance_accuracy_r3"
CLINCH_ACCURACY_R3 = "clinch_accuracy_r3"
GROUND_ACCURACY_R3 = "ground_accuracy_r3"
TAKEDOWN_ACCURACY_R3 = "takedown_accuracy_r3"
# Poder point-in-time (SPEC 009, bloco A2): os knockdowns por minuto de cativeiro e por
# luta. As duas convivem de propósito -- por minuto premia o nocauteador precoce, por luta
# descreve o volume de quedas provocadas; qual delas carrega sinal é o que a ablação mede.
KNOCKDOWNS_PM_R3 = "knockdowns_pm_r3"
KNOCKDOWNS_AVG_R3 = "knockdowns_avg_r3"
# Ameaça de grappling (A3) e trabalho fora do golpe significativo (A4). A razão
# total/significativo separa quem trabalha no clinch e no solo com golpe não-significativo
# de quem só troca em pé -- dimensão que nenhuma feature anterior descrevia.
SUBMISSION_ATTEMPTS_AVG_R3 = "submission_attempts_avg_r3"
TOTAL_TO_SIG_STRIKE_RATIO_R3 = "total_to_sig_strike_ratio_r3"
# Experiência em cinco rounds (SPEC 009, bloco B2): quantas lutas anteriores do lutador foram
# **agendadas** para cinco rounds. Distingue vinte e cinco minutos pela primeira vez de quem
# já passou por eles -- dimensão que ``career_bouts_before`` trata como igual.
FIVE_ROUND_BOUTS_BEFORE = "five_round_bouts_before"
# Como o lutador perde (SPEC 009, bloco C1): contagens point-in-time de derrota por nocaute e
# por finalização na carreira anterior. **Contagem, não taxa** -- o argumento de domínio é
# "já foi nocauteado N vezes, o queixo não se recupera"; a normalização por carreira o modelo
# obtém de ``career_bouts_before``, que já é feature desde o M4.
KO_LOSSES_PRIOR = "ko_losses_prior"
SUBMISSION_LOSSES_PRIOR = "submission_losses_prior"
# Quilometragem acumulada (SPEC 009, bloco C2): minutos de cativeiro das lutas anteriores.
# Distingue vinte decisões de vinte nocautes no primeiro round, que ``career_bouts_before``
# trata como carreiras iguais. Também é o denominador das gêmeas de carreira por minuto (C3),
# e por isso é calculada ANTES delas.
CAREER_MINUTES_BEFORE = "career_minutes_before"
# Tendência (SPEC 009, bloco C3): ``<base>_r3 - <base>_carreira`` para as cinco métricas de
# forma recente do M4. Positivo = em ascensão. As gêmeas de carreira que servem de subtraendo
# são intermediárias LOCAIS, nunca colunas -- emiti-las dobraria o bloco e o faria medir
# "carreira + tendência" em vez de tendência. O conjunto é fixo nessas cinco de propósito:
# estendê-lo às precisões do bloco A acoplaria dois blocos que precisam ser medidos separados.
SIG_STRIKES_LANDED_PM_TREND = "sig_strikes_landed_pm_trend"
SIG_STRIKES_ABSORBED_PM_TREND = "sig_strikes_absorbed_pm_trend"
TAKEDOWNS_LANDED_AVG_TREND = "takedowns_landed_avg_trend"
TAKEDOWN_DEFENSE_TREND = "takedown_defense_trend"
CONTROL_TIME_AVG_TREND = "control_time_avg_trend"
# Confronto de bases (SPEC 009, bloco D1): quantos adversários canhotos o lutador já
# enfrentou antes desta luta. Quem já cruzou com cinco perde a desvantagem que o canhoto
# tem contra quem nunca viu um. Calculada por ``add_stance_history_features``, **fora** de
# ``add_recent_form_features``: depende de ``stance``, que só existe depois da trajetória.
SOUTHPAW_OPPONENTS_FACED_PRIOR = "southpaw_opponents_faced_prior"
# Desempenho contra estilo semelhante (SPEC 009, bloco D3): a média das lutas anteriores
# ponderada pela semelhança entre o adversário de cada uma e o adversário desta luta.
SIMILAR_STYLE_WIN_RATE_PRIOR = "similar_style_win_rate_prior"

# Largura do núcleo gaussiano de semelhança de estilo do D3.
#
# **EMENDA à SPEC 009 (decisão do humano, 2026-09-02).** A SPEC no painel ainda escreve
# ``0.5``, mas com a fórmula normativa ``exp(-d**2 / (2*sigma**2))`` esse valor daria
# ``exp(-2/0.5) = exp(-4) = 0,0183`` ao adversário mais dissemelhante possível (``d**2 = 2``,
# porque os dois eixos vivem em ``[0, 1]``) -- 1,8%, reprovado pelo CA-09 (``> 0,1``) e
# incompatível com os 13,5% que a própria RF-05 argumenta. Os 13,5% são ``exp(-2)``, o que
# exige ``2*sigma**2 = 1``, isto é ``sigma = 1/sqrt(2)``: a conta original da SPEC dividiu por
# ``2*sigma`` em vez de ``2*sigma**2``. **A fórmula NÃO muda; só o valor da constante.** A SPEC
# do painel não foi corrigida porque ``update-spec`` resetaria a aprovação -- a divergência com
# o painel é esta emenda, não defeito.
#
# O valor é fixado **por argumento** e jamais ajustado pela métrica: tuning é não-objetivo
# declarado da SPEC. O desvio-padrão observado de ``style_distance`` na janela é reportado pela
# ablação como aferição de sanidade, e só isso.
STYLE_KERNEL_SIGMA: float = 0.7071067811865476  # 1/sqrt(2) -> 2*sigma**2 == 1.0

# Força de calendário (SPEC 009, bloco D4): a média simples do ``win_rate_prior`` dos
# adversários anteriores, cada um medido na data daquela luta. Hoje vencer cinco estreantes e
# vencer cinco ranqueados produzem o mesmo ``win_rate_prior`` -- esta feature é o que separa
# os dois calendários.
OPPONENT_WIN_RATE_PRIOR_AVG = "opponent_win_rate_prior_avg"
# Eixos de estilo point-in-time (SPEC 009, bloco D2): o vetor de estilo do lutador na data da
# luta, em duas dimensões. Dois eixos e não um: um eixo único colapsaria perfis opostos -- o
# striker de baixo volume e alto poder e o de pressão constante são iguais no grappling e
# opostos no volume. São o insumo declarado de ``matchup.style_distance`` e do bloco D3.
GRAPPLING_AXIS_R3 = "grappling_axis_r3"
VOLUME_AXIS_R3 = "volume_axis_r3"

# Divisores de normalização dos eixos de estilo. Constantes de **domínio** (o valor de um
# praticante notoriamente dominante naquela dimensão), escolhidas por argumento e **jamais
# ajustadas pela métrica**: elas põem grandezas de escalas incompatíveis (quedas, segundos,
# golpes por minuto, frações) no mesmo intervalo [0, 1] sem estimar média ou desvio sobre o
# dataset -- qualquer estatística global seria um fit sobre dado futuro, o mesmo defeito que
# a ADR 0002 rejeitou nas médias de carreira do snapshot de 2025.
GRAPPLING_TAKEDOWNS_DIVISOR: float = 3.0  # 3 quedas por luta = grappler dominante
GRAPPLING_CONTROL_SECONDS_DIVISOR: float = 300.0  # 5 min de controle por luta = domínio total
VOLUME_SIG_STRIKES_PM_DIVISOR: float = 6.0  # 6 golpes significativos/min = volume alto

# Número de componentes de cada eixo. Nomeados porque são o divisor da média explícita, e a
# média explícita é justamente o que impede a média parcial de ``DataFrame.mean(axis=1)``.
_GRAPPLING_AXIS_COMPONENTS: float = 3.0
_VOLUME_AXIS_COMPONENTS: float = 2.0

# (feature, coluna conectada, coluna tentada). Tabela de dados, não abstração: oito usos
# concretos da mesma forma -- razão de somas na janela, jamais média de razões por luta.
_ACCURACY_PAIRS: tuple[tuple[str, str, str], ...] = (
    (SIG_STRIKE_ACCURACY_R3, COL_SIG_STRIKES_LANDED, COL_SIG_STRIKES_ATTEMPTED),
    (HEAD_ACCURACY_R3, COL_HEAD_LANDED, COL_HEAD_ATTEMPTED),
    (BODY_ACCURACY_R3, COL_BODY_LANDED, COL_BODY_ATTEMPTED),
    (LEG_ACCURACY_R3, COL_LEG_LANDED, COL_LEG_ATTEMPTED),
    (DISTANCE_ACCURACY_R3, COL_DISTANCE_LANDED, COL_DISTANCE_ATTEMPTED),
    (CLINCH_ACCURACY_R3, COL_CLINCH_LANDED, COL_CLINCH_ATTEMPTED),
    (GROUND_ACCURACY_R3, COL_GROUND_LANDED, COL_GROUND_ATTEMPTED),
    (TAKEDOWN_ACCURACY_R3, COL_TAKEDOWNS_LANDED, COL_TAKEDOWNS_ATTEMPTED),
)

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
    *(feature for feature, _landed, _attempted in _ACCURACY_PAIRS),
    KNOCKDOWNS_PM_R3,
    KNOCKDOWNS_AVG_R3,
    SUBMISSION_ATTEMPTS_AVG_R3,
    TOTAL_TO_SIG_STRIKE_RATIO_R3,
    FIVE_ROUND_BOUTS_BEFORE,
    KO_LOSSES_PRIOR,
    SUBMISSION_LOSSES_PRIOR,
    CAREER_MINUTES_BEFORE,
    SIG_STRIKES_LANDED_PM_TREND,
    SIG_STRIKES_ABSORBED_PM_TREND,
    TAKEDOWNS_LANDED_AVG_TREND,
    TAKEDOWN_DEFENSE_TREND,
    CONTROL_TIME_AVG_TREND,
    GRAPPLING_AXIS_R3,
    VOLUME_AXIS_R3,
    SIMILAR_STYLE_WIN_RATE_PRIOR,
    OPPONENT_WIN_RATE_PRIOR_AVG,
]

# Features de dinâmica por round (conjunto mínimo -- YAGNI): dependem de round_stats
# (``load_round_stats``), diferente das demais features (só a frame longa).
ROUND_DYNAMICS_FEATURES: list[str] = [ROUND1_SIG_STRIKE_SHARE_R3]

# Features de histórico de bases (bloco D1): conjunto próprio pelo mesmo motivo do anterior
# -- dependem de uma coluna (``stance``) que ``add_recent_form_features`` ainda não tem.
STANCE_HISTORY_FEATURES: list[str] = [SOUTHPAW_OPPONENTS_FACED_PRIOR]

# Box-score do adversário projetado pelas features de forma recente: o striking que o lutador
# absorve e as quedas que ele enfrenta. Uma lista só para os dois callsites (janela de 3 e
# gêmea de carreira), que precisam projetar exatamente o mesmo conjunto -- se divergissem, a
# tendência compararia pontas calculadas sobre dados diferentes.
_OPPONENT_BOX_SCORE_COLUMNS: list[str] = [
    COL_SIG_STRIKES_LANDED,
    COL_TAKEDOWNS_LANDED,
    COL_TAKEDOWNS_ATTEMPTED,
]

# Estado **as-of** do adversário naquela luta -- já calculado na linha dele pelos estágios
# anteriores, e por isso point-in-time por construção (RF-04). É o que torna os blocos D3/D4
# baratos: nenhum recálculo, só leitura da linha do adversário.
_OPPONENT_ASOF_COLUMNS: list[str] = [WIN_RATE_PRIOR, GRAPPLING_AXIS_R3, VOLUME_AXIS_R3]


def _prior_expanding_mean(values: pd.Series, group: pd.Series) -> pd.Series:
    """Média acumulada das lutas anteriores por grupo: ``shift(1).expanding().mean()``.

    O ``shift(1)`` exclui a luta corrente (point-in-time); ``expanding().mean()`` ignora os
    ``NaN`` (valores fora do denominador, ex.: no contest/empate no win rate). Na estreia o
    ``shift`` produz ``NaN`` e a média sobre ``[NaN]`` permanece ``NaN``.
    """
    return values.groupby(group).transform(lambda s: s.shift(1).expanding().mean())


def _prior_expanding_sum(values: pd.Series, group: pd.Series) -> pd.Series:
    """Soma acumulada das lutas anteriores por grupo: ``shift(1).expanding().sum()``.

    Gêmea de ``_prior_expanding_mean``: ``shift(1)`` exclui a luta corrente (point-in-time) e
    ``expanding().sum()`` acumula sobre a carreira anterior inteira. Os ``NaN`` da janela são
    ignorados, e o resultado só é ``NaN`` quando **nenhum** valor conhecido entrou nela --
    estreia, ou histórico inteiro sem o dado. É essa propriedade que mantém a ausência
    distinta do zero (RF-03), sem ramificação explícita.
    """
    return values.groupby(group).transform(lambda s: s.shift(1).expanding().sum())


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
    return (frame[COL_FIGHT_END_ROUND] - 1) * _ROUND_MINUTES + frame[COL_ENDING_TIME_SECONDS] / 60.0


def _opponent_stats(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Colunas do canto oposto de cada luta, alinhadas linha a linha à frame de entrada.

    Pareia os dois cantos da **mesma** luta (merge por ``bout_id``, excluindo o próprio
    lutador) e projeta as ``columns`` pedidas do adversário: o box-score (o striking que o
    lutador absorve, os takedowns que ele enfrenta) para as features de forma recente, e a
    base do adversário para o histórico de bases. Dados sujos (canto faltante ou mais de um
    oponente) degradam de forma visível via log.

    As colunas a projetar são argumento, e não uma lista fixa, porque a regra que importa --
    parear por ``bout_id``, descartar o próprio lutador, avisar sobre dado sujo e realinhar
    ao índice de entrada -- é a mesma para qualquer coluna do adversário e precisa viver num
    lugar só. Duplicá-la para projetar ``stance`` faria a segunda cópia divergir na primeira
    correção.
    """
    keys = frame.reset_index(names="_row_id")[["_row_id", COL_BOUT_ID, COL_FIGHTER_ID]]
    opponent_cols = frame[[COL_BOUT_ID, COL_FIGHTER_ID, *columns]]
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

    won = result.map(_WON_BY_RESULT)
    frame[WIN_RATE_PRIOR] = _prior_expanding_mean(won, fighter)

    # Finalização = vitória por KO/TKO ou finalização; demais lutas contam no denominador.
    is_finish = (result == _RESULT_WIN) & frame[COL_METHOD].isin(_FINISH_METHODS)
    frame[FINISH_RATE_PRIOR] = _prior_expanding_mean(is_finish.astype(float), fighter)

    frame[WIN_STREAK_PRIOR] = result.groupby(fighter).transform(_prior_streak)


def safe_ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Razão com denominador zero mapeado para ``NaN`` (nunca ``inf``).

    Um denominador zero (minutos anteriores nulos, tentativas de queda enfrentadas zero)
    torna a taxa indefinida. Sem esta guarda, ``x/0`` com ``x>0`` produziria ``inf`` -- que
    não é JSON válido e quebraria a materialização em JSONB. ``NaN`` explícito é coerente
    com o tratamento da estreia (decisão #3 da SPEC) e vira ``null`` na Slice 05.

    Pública desde a Slice 02 da SPEC 009: a RF-03 manda **reusar** esta guarda, e
    ``ingestion.features.division`` é o primeiro consumidor fora deste módulo. A alternativa
    seria duplicar a regra de denominador zero em dois lugares.
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
    opponent = _opponent_stats(frame, _OPPONENT_BOX_SCORE_COLUMNS)

    frame[SIG_STRIKES_LANDED_PM_R3] = safe_ratio(
        _prior_rolling_sum(frame[COL_SIG_STRIKES_LANDED], fighter), prior_minutes
    )
    frame[SIG_STRIKES_ABSORBED_PM_R3] = safe_ratio(
        _prior_rolling_sum(opponent[COL_SIG_STRIKES_LANDED], fighter), prior_minutes
    )
    frame[TAKEDOWNS_LANDED_AVG_R3] = _prior_rolling_mean(frame[COL_TAKEDOWNS_LANDED], fighter)
    conceded = _prior_rolling_sum(opponent[COL_TAKEDOWNS_LANDED], fighter)
    faced = _prior_rolling_sum(opponent[COL_TAKEDOWNS_ATTEMPTED], fighter)
    frame[TAKEDOWN_DEFENSE_R3] = 1.0 - safe_ratio(conceded, faced)
    frame[CONTROL_TIME_AVG_R3] = _prior_rolling_mean(frame[COL_CONTROL_TIME_SECONDS], fighter)


def _add_striking_profile_features(frame: pd.DataFrame) -> None:
    """Adiciona as shares de striking point-in-time por alvo e por posição.

    Cada share é a razão de somas na janela (``sum(componente)/sum(total)``) das lutas
    **anteriores** -- não média de razões por luta -- reusando ``_prior_rolling_sum``
    (``shift(1)`` exclui a luta corrente). O denominador de alvo é ``cabeça+corpo+perna``
    conectados; o de posição é ``distância+clinch+solo`` -- assim cada trio soma 1 quando
    definido, e ``safe_ratio`` mapeia denominador zero (nenhum golpe conectado antes) para
    ``NaN``, nunca ``inf`` (que quebraria o JSONB). Na estreia o ``shift`` produz ``NaN``.
    """
    fighter = frame[COL_FIGHTER_ID]
    head = _prior_rolling_sum(frame[COL_HEAD_LANDED], fighter)
    body = _prior_rolling_sum(frame[COL_BODY_LANDED], fighter)
    leg = _prior_rolling_sum(frame[COL_LEG_LANDED], fighter)
    target_total = head + body + leg
    frame[SHARE_HEAD_R3] = safe_ratio(head, target_total)
    frame[SHARE_BODY_R3] = safe_ratio(body, target_total)
    frame[SHARE_LEG_R3] = safe_ratio(leg, target_total)

    distance = _prior_rolling_sum(frame[COL_DISTANCE_LANDED], fighter)
    clinch = _prior_rolling_sum(frame[COL_CLINCH_LANDED], fighter)
    ground = _prior_rolling_sum(frame[COL_GROUND_LANDED], fighter)
    position_total = distance + clinch + ground
    frame[SHARE_DISTANCE_R3] = safe_ratio(distance, position_total)
    frame[SHARE_CLINCH_R3] = safe_ratio(clinch, position_total)
    frame[SHARE_GROUND_R3] = safe_ratio(ground, position_total)


def _add_accuracy_features(frame: pd.DataFrame) -> None:
    """Adiciona a precisão point-in-time de cada dimensão de golpe e da queda.

    Cada precisão é a **razão de somas** na janela das 3 lutas anteriores
    (``sum(conectado)/sum(tentado)``, via ``_prior_rolling_sum``) -- nunca a média das
    razões por luta, que daria peso igual a uma luta de 1 tentativa e a uma de 40 e
    distorceria o lutador de nocaute rápido. Mesmo padrão de
    ``_add_striking_profile_features``. Sem nenhuma tentativa anterior o denominador é zero
    e ``safe_ratio`` produz ``NaN`` (nunca ``inf``, que quebraria o JSONB); na estreia o
    ``shift(1)`` já produz ``NaN``.
    """
    fighter = frame[COL_FIGHTER_ID]
    for feature, landed, attempted in _ACCURACY_PAIRS:
        frame[feature] = safe_ratio(
            _prior_rolling_sum(frame[landed], fighter),
            _prior_rolling_sum(frame[attempted], fighter),
        )


def _add_power_and_threat_features(frame: pd.DataFrame) -> None:
    """Adiciona poder (A2), ameaça de grappling (A3) e trabalho total (A4) do bloco A.

    ``knockdowns_pm_r3`` é razão de somas na janela (``sum(knockdowns)/sum(minutos)``,
    mesmos minutos de ``_fight_minutes`` usados pelas taxas de striking) e
    ``knockdowns_avg_r3`` é a média por luta -- as duas leituras do mesmo dado, medidas
    juntas pela ablação. ``submission_attempts_avg_r3`` é média por luta;
    ``total_to_sig_strike_ratio_r3`` é razão de somas do conectado total sobre o conectado
    significativo. Denominador zero (nenhum minuto, nenhum golpe significativo anterior)
    torna a razão indefinida (``safe_ratio`` -> ``NaN``, nunca ``inf``); na estreia o
    ``shift(1)`` já produz ``NaN``. Uma luta anterior sem ``total_strikes_landed`` (backfill
    parcial do M5/M7) sai da soma sem anular a janela inteira (``min_periods=1``).
    """
    fighter = frame[COL_FIGHTER_ID]
    prior_minutes = _prior_rolling_sum(_fight_minutes(frame), fighter)
    frame[KNOCKDOWNS_PM_R3] = safe_ratio(
        _prior_rolling_sum(frame[COL_KNOCKDOWNS], fighter), prior_minutes
    )
    frame[KNOCKDOWNS_AVG_R3] = _prior_rolling_mean(frame[COL_KNOCKDOWNS], fighter)
    frame[SUBMISSION_ATTEMPTS_AVG_R3] = _prior_rolling_mean(frame[COL_SUBMISSION_ATTEMPTS], fighter)
    frame[TOTAL_TO_SIG_STRIKE_RATIO_R3] = safe_ratio(
        _prior_rolling_sum(frame[COL_TOTAL_STRIKES_LANDED], fighter),
        _prior_rolling_sum(frame[COL_SIG_STRIKES_LANDED], fighter),
    )


def _add_five_round_experience(frame: pd.DataFrame) -> None:
    """Adiciona quantas lutas anteriores do lutador foram agendadas para cinco rounds.

    Zero é valor **legítimo** (o lutador tem histórico e nunca fez cinco rounds); ``NaN``
    significa ausência de histórico **conhecido** -- estreia, ou nenhuma anterior com o
    formato registrado.

    O indicador nasce ``NaN`` quando ``scheduled_rounds`` é nulo, e não ``0``: tratar formato
    desconhecido como "não foi de cinco rounds" seria imputação, proibida pela RF-03, e
    afirmaria sobre a luta um formato que ninguém registrou (``scheduled_rounds`` está
    preenchido em 93,1% do banco real). Como ``_prior_expanding_sum`` ignora ``NaN`` na
    janela, a contagem fica definida assim que **alguma** anterior for conhecida e permanece
    nula enquanto nenhuma for.

    ``errors="raise"`` na conversão: um valor inesperado na coluna precisa falhar visível, em
    vez de virar ``NaN`` em silêncio e esvaziar a feature sem ninguém notar.
    """
    scheduled = pd.to_numeric(frame[COL_SCHEDULED_ROUNDS], errors="raise")
    is_five = (scheduled == _FIVE_ROUND_FORMAT).astype("float64").where(scheduled.notna())
    frame[FIVE_ROUND_BOUTS_BEFORE] = _prior_expanding_sum(is_five, frame[COL_FIGHTER_ID])


def _add_qualified_history_features(frame: pd.DataFrame) -> None:
    """Adiciona o histórico qualificado dos blocos C1 e C2 da SPEC 009.

    ``ko_losses_prior`` e ``submission_losses_prior`` são **contagens** das derrotas
    anteriores por cada método, via ``_prior_expanding_sum`` (o ``shift(1)`` exclui a luta
    corrente). ``0`` é valor legítimo -- o lutador tem histórico e nunca perdeu daquele jeito
    --, e ``NaN`` é só a estreia, onde a janela contém apenas o ``NaN`` do shift.

    Uma derrota com **método desconhecido** não incrementa nenhuma das duas (``False`` no
    indicador). É degradação aceitável e coerente com ``finish_rate_prior``, que já trata
    método ausente como não-finalização; inventar um terceiro estado diria mais do que o dado
    permite. Derrota por decisão fica fora das duas de propósito: a SPEC proíbe categoria
    própria para ela.

    ``career_minutes_before`` (C2) é a soma expanding dos minutos de cativeiro anteriores,
    reusando a mesma duração de ``_fight_minutes`` já usada pelas taxas por minuto. Uma luta
    anterior com ``round`` ou ``ending_time_seconds`` nulo sai da soma sem anular a carreira
    inteira -- **assimetria conhecida e herdada do M4**: os golpes dessa luta continuam
    entrando no numerador das taxas por minuto, ainda que os minutos dela não entrem no
    denominador. Corrigi-la está fora do escopo desta slice; fica registrada aqui.
    """
    fighter = frame[COL_FIGHTER_ID]
    lost = frame[COL_RESULT] == _RESULT_LOSS
    method = frame[COL_METHOD]
    frame[KO_LOSSES_PRIOR] = _prior_expanding_sum(
        (lost & (method == BoutMethod.KO_TKO.value)).astype("float64"), fighter
    )
    frame[SUBMISSION_LOSSES_PRIOR] = _prior_expanding_sum(
        (lost & (method == BoutMethod.SUBMISSION.value)).astype("float64"), fighter
    )
    frame[CAREER_MINUTES_BEFORE] = _prior_expanding_sum(_fight_minutes(frame), fighter)


def _add_trend_features(frame: pd.DataFrame) -> None:
    """Emite ``<base>_trend = <base>_r3 - <base>_carreira`` (positivo = em ascensão).

    A gêmea de carreira de cada métrica usa **a mesma fórmula** da ponta recente, com
    ``expanding`` no lugar de ``rolling(WINDOW_RECENT)`` -- razão de somas onde a ponta
    recente é razão de somas, média por luta onde ela é média por luta. É essa simetria que
    faz a diferença ser exatamente zero quando as três lutas anteriores são a carreira
    inteira, e o que torna o sinal legível.

    As gêmeas são **Séries locais, nunca colunas** (mesmo precedente de
    ``_round1_share_by_participation``): emiti-las dobraria as colunas do bloco e o faria
    medir "carreira + tendência" em vez de tendência, acoplando num veredito só duas coisas
    que a RF-06 manda medir separadas.

    Qualquer ponta ausente propaga ``NaN`` para a diferença (RF-03) -- ausência nunca vira a
    outra ponta nem ``0``, que o modelo leria como "estável". Denominador zero passa por
    ``safe_ratio`` e vira ``NaN``, jamais ``inf`` (que não é JSON válido).

    **Ordem de encadeamento é pré-condição**: roda depois de ``_add_rolling_features`` (lê as
    colunas ``_r3``) e depois de ``_add_qualified_history_features`` (lê
    ``career_minutes_before``, o denominador das gêmeas por minuto). Invertê-la produziria
    ``KeyError`` ou coluna inteira nula.
    """
    fighter = frame[COL_FIGHTER_ID]
    opponent = _opponent_stats(frame, _OPPONENT_BOX_SCORE_COLUMNS)
    career_minutes = frame[CAREER_MINUTES_BEFORE]

    landed = safe_ratio(
        _prior_expanding_sum(frame[COL_SIG_STRIKES_LANDED], fighter), career_minutes
    )
    absorbed = safe_ratio(
        _prior_expanding_sum(opponent[COL_SIG_STRIKES_LANDED], fighter), career_minutes
    )
    takedowns = _prior_expanding_mean(frame[COL_TAKEDOWNS_LANDED], fighter)
    defense = 1.0 - safe_ratio(
        _prior_expanding_sum(opponent[COL_TAKEDOWNS_LANDED], fighter),
        _prior_expanding_sum(opponent[COL_TAKEDOWNS_ATTEMPTED], fighter),
    )
    control = _prior_expanding_mean(frame[COL_CONTROL_TIME_SECONDS], fighter)

    frame[SIG_STRIKES_LANDED_PM_TREND] = frame[SIG_STRIKES_LANDED_PM_R3] - landed
    frame[SIG_STRIKES_ABSORBED_PM_TREND] = frame[SIG_STRIKES_ABSORBED_PM_R3] - absorbed
    frame[TAKEDOWNS_LANDED_AVG_TREND] = frame[TAKEDOWNS_LANDED_AVG_R3] - takedowns
    frame[TAKEDOWN_DEFENSE_TREND] = frame[TAKEDOWN_DEFENSE_R3] - defense
    frame[CONTROL_TIME_AVG_TREND] = frame[CONTROL_TIME_AVG_R3] - control


def _normalized(values: pd.Series, divisor: float) -> pd.Series:
    """Componente em ``[0, 1]``: ``min(valor / divisor, 1)``; ``NaN`` permanece ``NaN``.

    O truncamento é o que impede o dominante extremo de dominar a média do eixo: acima do
    valor de referência de domínio, "mais" deixa de significar "mais grappler".
    """
    return (values / divisor).clip(upper=1.0)


def _add_style_axis_features(frame: pd.DataFrame) -> None:
    """Adiciona os dois eixos de estilo as-of por fórmula fixa (sem PCA, sem fit aprendido).

    Fórmula normativa da SPEC 009 (bloco D2), com os divisores em constantes de domínio.
    Escolha deliberada de "à mão em vez de aprendido": é interpretável, defensável, e
    testável point-in-time linha a linha -- uma redução de dimensionalidade ajustada sobre o
    dataset seria um fit sobre dado futuro, exatamente o que a SPEC proíbe.

    A **soma explícita dividida pelo número de componentes** é load-bearing:
    ``DataFrame.mean(axis=1)`` ignora ``NaN`` e devolveria a média dos componentes que
    existem. O eixo sairia definido, mas calculado sobre um subconjunto diferente de
    componentes por lutador -- e comparar eixos entre lutadores é exatamente o que
    ``matchup.style_distance`` (e o bloco D3 da Slice 06) fazem. Com a soma, qualquer
    componente nulo anula o eixo (RF-03), que é o comportamento correto.

    Point-in-time por **herança**: os cinco componentes já passaram por ``shift(1)`` em
    ``_add_rolling_features`` e ``_add_striking_profile_features``; aqui não há ``shift``
    novo, só aritmética de coluna. **Ordem de encadeamento é pré-condição**: roda depois das
    duas, senão levanta ``KeyError``.
    """
    frame[GRAPPLING_AXIS_R3] = (
        _normalized(frame[TAKEDOWNS_LANDED_AVG_R3], GRAPPLING_TAKEDOWNS_DIVISOR)
        + _normalized(frame[CONTROL_TIME_AVG_R3], GRAPPLING_CONTROL_SECONDS_DIVISOR)
        + frame[SHARE_GROUND_R3]
    ) / _GRAPPLING_AXIS_COMPONENTS
    frame[VOLUME_AXIS_R3] = (
        _normalized(frame[SIG_STRIKES_LANDED_PM_R3], VOLUME_SIG_STRIKES_PM_DIVISOR)
        + frame[SHARE_DISTANCE_R3]
    ) / _VOLUME_AXIS_COMPONENTS


def _similar_style_weighted_prior(
    won: pd.Series,
    opponent_grappling: pd.Series,
    opponent_volume: pd.Series,
    group: pd.Series,
) -> pd.Series:
    """Média das lutas anteriores ponderada pela semelhança de estilo do adversário (D3).

    Para a luta corrente ``i`` de um lutador e cada luta anterior ``j`` dele
    (``j`` antes de ``i`` na ordem cronológica do grupo)::

        d2 = (g_j - g_i)**2 + (v_j - v_i)**2
        w  = exp(-d2 / (2 * STYLE_KERNEL_SIGMA**2))

    Nenhuma luta anterior é descartada por limiar (RF-05) -- só por **inelegibilidade**: eixo
    nulo do adversário de ``j`` **ou** de ``i`` (o ``NaN`` propaga por ``w``) e no
    contest/empate (``won`` nulo). Denominador zero vira ``NaN``, jamais ``0.0`` (RF-03).

    Esta é a **primeira feature do projeto que não usa janela do Pandas**: o peso depende da
    linha corrente, então nenhuma ``rolling``/``expanding`` a expressa. O point-in-time vem do
    **prefixo estrito** dentro do grupo -- ``np.tril(..., k=-1)`` com a diagonal **excluída**,
    que é o que mantém a luta corrente fora do próprio cálculo (um ``k=0`` por engano seria
    vazamento direto do desfecho).

    Por isso a **ordem posicional dentro do grupo é pré-condição load-bearing**: "tudo antes
    desta linha" só é o passado real com a frame na ordem canônica
    ``(fighter_id, event_date, bout_id)``. ``add_recent_form_features`` a reforça antes de
    calcular qualquer coisa -- com frame desordenada o D3 leria o futuro **em silêncio**, o
    modo de falha mais caro possível.

    Custo: a matriz de distâncias é por lutador, e ``n`` é a carreira dele (mediana 4, máximo
    da ordem de 30 lutas) -- irrelevante diante do resto da pipeline.
    """
    values: np.typing.NDArray[np.float64] = np.full(len(won), np.nan, dtype="float64")
    grappling = opponent_grappling.to_numpy(dtype="float64")
    volume = opponent_volume.to_numpy(dtype="float64")
    outcome = won.to_numpy(dtype="float64")
    two_sigma_squared = 2.0 * STYLE_KERNEL_SIGMA**2

    for positions in group.groupby(group, sort=False).indices.values():
        axis_g = grappling[positions]
        axis_v = volume[positions]
        result = outcome[positions]
        squared_distance = (axis_g[:, None] - axis_g[None, :]) ** 2 + (
            axis_v[:, None] - axis_v[None, :]
        ) ** 2
        weight = np.exp(-squared_distance / two_sigma_squared)
        eligible = (
            np.tril(np.ones(weight.shape, dtype=bool), k=-1)
            & np.isfinite(weight)
            & np.isfinite(result)[None, :]
        )
        numerator = np.where(eligible, weight * result, 0.0).sum(axis=1)
        denominator = np.where(eligible, weight, 0.0).sum(axis=1)
        # ``np.divide`` com ``where`` em vez de ``np.where``: este avaliaria ``0/0`` no ramo
        # descartado e emitiria ``RuntimeWarning`` em toda linha sem luta elegível -- ruído em
        # log é o que faz o aviso que importa passar despercebido.
        values[positions] = np.divide(
            numerator,
            denominator,
            out=np.full(denominator.shape, np.nan, dtype="float64"),
            where=denominator > 0.0,
        )
    return pd.Series(values, index=won.index)


def _add_opponent_history_features(frame: pd.DataFrame) -> None:
    """Adiciona os blocos D3 e D4: o histórico de A qualificado pelo estado as-of dos rivais.

    Os dois leem o estado do adversário **na linha dele naquela luta** -- o
    ``win_rate_prior`` e os eixos de estilo que ele tinha naquela data, não os de hoje
    (RF-04). É o que torna o bloco barato: nenhum recálculo, só um segundo pareamento por
    ``bout_id``.

    ``similar_style_win_rate_prior`` (D3) pondera todas as lutas anteriores pelo núcleo
    gaussiano da semelhança de estilo (ver ``_similar_style_weighted_prior``).

    ``opponent_win_rate_prior_avg`` (D4) é a média simples do cartel as-of dos adversários
    anteriores. ``_prior_expanding_mean`` faz o trabalho inteiro: o ``shift(1)`` exclui a luta
    corrente e o ``expanding().mean()`` ignora ``NaN``, de modo que o adversário estreante --
    sem cartel anterior -- fica fora do **denominador** sem nenhuma ramificação (RF-03). Zero
    seria uma afirmação forte sobre um calendário desconhecido; ausência é o que o dado
    permite.

    O pareamento do adversário acontece **duas vezes** na pipeline e isso é proposital, não
    duplicação: ``_add_rolling_features`` pareia o box-score **cru** da mesma luta (o que o
    lutador absorve), e este estágio pareia o estado **as-of**, que só existe depois dos
    estágios anteriores terem rodado. Unificá-los é impossível -- o primeiro roda antes de as
    features as-of existirem.

    **Ordem de encadeamento é pré-condição**: roda por último, depois de
    ``_add_expanding_features`` (``win_rate_prior``) e de ``_add_style_axis_features`` (os dois
    eixos). Adiantá-lo produziria ``KeyError`` no pareamento as-of.
    """
    fighter = frame[COL_FIGHTER_ID]
    opponent = _opponent_stats(frame, _OPPONENT_ASOF_COLUMNS)
    frame[SIMILAR_STYLE_WIN_RATE_PRIOR] = _similar_style_weighted_prior(
        frame[COL_RESULT].map(_WON_BY_RESULT),
        opponent[GRAPPLING_AXIS_R3],
        opponent[VOLUME_AXIS_R3],
        fighter,
    )
    frame[OPPONENT_WIN_RATE_PRIOR_AVG] = _prior_expanding_mean(opponent[WIN_RATE_PRIOR], fighter)


def add_recent_form_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Devolve cópia da frame longa com as colunas de forma recente point-in-time.

    A frame é **reordenada defensivamente** para a ordem canônica
    ``(fighter_id, event_date, bout_id)`` (sort estável), mesmo padrão de
    ``trajectory.add_trajectory_features``. Deixou de ser pré-condição presumida do caller na
    Slice 06 da SPEC 009: o bloco D3 é calculado por **prefixo posicional** dentro do grupo do
    lutador, e uma frame desordenada o faria ler lutas futuras **em silêncio** -- ao contrário
    de um ``KeyError``, esse defeito não acusa. As features usam apenas as lutas 1..N-1 do
    mesmo lutador; a estreia produz ``NaN`` explícito em todas elas. A frame de entrada não é
    mutada.

    A **ordem das chamadas é significativa** na cauda: ``_add_trend_features`` lê as colunas
    ``_r3`` de ``_add_rolling_features`` e a coluna ``career_minutes_before`` de
    ``_add_qualified_history_features``, e por isso vem depois das duas.
    ``_add_style_axis_features`` lê as ``_r3`` de ``_add_rolling_features`` e as shares de
    ``_add_striking_profile_features``. ``_add_opponent_history_features`` fecha a sequência
    porque lê, da **linha do adversário**, features que os estágios anteriores acabaram de
    produzir (``win_rate_prior`` e os dois eixos de estilo). Adiantar qualquer uma produziria
    ``KeyError`` -- ou, pior, coluna inteira nula em silêncio, que é o modo de falha que a
    SPEC 009 existe para não repetir.
    """
    frame = frame.sort_values(by=_SORT_KEY, kind="stable").reset_index(drop=True)
    _add_expanding_features(frame)
    _add_rolling_features(frame)
    _add_striking_profile_features(frame)
    _add_accuracy_features(frame)
    _add_power_and_threat_features(frame)
    _add_five_round_experience(frame)
    _add_qualified_history_features(frame)
    _add_trend_features(frame)
    _add_style_axis_features(frame)
    _add_opponent_history_features(frame)
    return frame


def add_stance_history_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Devolve cópia da frame longa com ``southpaw_opponents_faced_prior`` point-in-time.

    Contagem acumulada de adversários canhotos que o lutador enfrentou **antes** desta luta:
    quem já cruzou com cinco perde a desvantagem que o canhoto tem contra quem nunca viu um.
    ``0`` é valor legítimo (tem histórico, nunca enfrentou canhoto) e ``NaN`` é a estreia --
    a mesma distinção de ``five_round_bouts_before``.

    **Ordem de encadeamento é pré-condição**: roda depois de
    ``trajectory.add_trajectory_features``, que é quem traz ``stance`` para a frame longa
    (``add_physical_attributes``). Chamada antes, levanta ``KeyError``. É também por isso que
    esta feature não cabe dentro de ``add_recent_form_features``: aquela roda **antes** da
    trajetória e a coluna ainda não existe lá.

    **Limitação registrada, não violação silenciosa da RF-04**: ``fighters.stance`` é atributo
    lento e **não versionado** no banco -- ``add_physical_attributes`` mescla o valor de hoje
    em toda linha histórica desde o M4 (o mesmo vale para altura e alcance). A base do
    adversário passado é, portanto, a de hoje, e a RF-04 é declarada **inatingível** para
    ``stance`` enquanto o banco não guardar histórico de atributo lento. A RF-04 continua
    valendo integralmente para os eixos de estilo do bloco D2 e para os blocos D3/D4, que
    derivam de box-score histórico e não de atributo de cadastro.

    **Degradação declarada no serving** (SPEC 009, CA-16): ``analysis.predict`` reconstrói a
    cadeia de features à mão e **não** chama esta função, então
    ``southpaw_opponents_faced_prior`` vira ``NaN`` no confronto hipotético, pelo ``reindex``
    de ``_align_features``. Limitação aceita e testada, não ponta solta -- propagar o contexto
    ao serving é PRD próprio posterior.

    **Adversário sem base registrada** não é contado como destro nem anula a contagem: o
    indicador dele é ``NaN`` e ``_prior_expanding_sum`` o ignora -- a feature é "canhotos
    enfrentados **entre os adversários de base conhecida**". Zerar afirmaria "era destro",
    que ninguém sabe (RF-03); anular a contagem inteira destruiria a cobertura de quem tem
    histórico bom e um único adversário sem cadastro.

    A frame de entrada não é mutada.
    """
    frame = frame.copy()
    opponent_stance = _opponent_stats(frame, [COL_STANCE])[COL_STANCE]
    known = opponent_stance.isin(_KNOWN_STANCES)
    is_southpaw = (opponent_stance == Stance.SOUTHPAW).fillna(False).astype("float64").where(known)
    frame[SOUTHPAW_OPPONENTS_FACED_PRIOR] = _prior_expanding_sum(is_southpaw, frame[COL_FIGHTER_ID])
    return frame


def _round1_share_by_participation(round_stats: pd.DataFrame) -> pd.Series:
    """Fração dos golpes conectados no round 1 sobre o total do bout, por participação.

    Agrega ``round_stats`` (uma linha por canto-por-round) por ``(bout_id, fighter_id)``:
    o numerador é o conectado no round 1, o denominador é a soma de todos os rounds do
    bout. ``safe_ratio`` mapeia denominador zero para ``NaN`` (nunca ``inf``). Devolve uma
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
    return safe_ratio(round1, total)


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
