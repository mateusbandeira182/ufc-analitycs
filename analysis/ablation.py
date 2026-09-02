"""Harness de medição por bloco: ablação com tudo mais fixo + bootstrap pareado.

A régua que a RF-06 da SPEC 009 (M8) exige e que o repositório não tinha. Até aqui, o
projeto comparava modelos por números registrados à mão (``analysis.metrics.PRE_M5_REFERENCE``
é um bootstrap feito fora do repositório e colado em código); medir sete sub-blocos de
features com honestidade estatística exige o harness em código, reexecutável e determinístico.

O método é ablação: constrói o dataset e o split temporal **uma vez** e treina dois modelos
que diferem **apenas** nas colunas do bloco sob medição -- mesmo dataset, mesmo holdout,
mesmo ``random_state``. A incerteza do delta vem de um **bootstrap pareado** sobre as linhas
do holdout (B=10.000, IC 95%): as mesmas linhas reamostradas alimentam os dois modelos, o
que remove a variância comum e mede exatamente o que se quer -- a diferença entre eles.

Convenção de sinal (a mesma de ``analysis.metrics.MetricsDelta`` e a mesma dos números
tabelados no PRD): ``delta = log_loss(com o bloco) - log_loss(sem o bloco)``. Log-loss menor
é melhor, logo **delta negativo significa que o bloco ajuda**, e o veredito da RF-07
(``ci_high < 0``) lê "todo o intervalo está do lado da melhora".

**Bloco que não paga é desfecho de sucesso, não falha** (RF-07): o registro do que não
funcionou é entregável da SPEC. Nenhum caminho deste módulo levanta exceção nem sai com
código diferente de zero por causa de um veredito negativo.

A cobertura por coluna é reportada **antes** do delta (RF-09) de propósito: uma coluna vazia
explica um delta nulo antes de ele ser interpretado como ausência de sinal -- foi exatamente
essa leitura invertida que escondeu 21 features do M5 por meses.

Há um bloco **especial**, ``janela-canto-fabricado`` (Slice 00B): ele não compara dois
conjuntos de colunas, e sim duas **variantes de dataset** -- a janela de treino histórica
(2010-01-01) contra a corrigida (2010-03-21). Como os dois braços têm holdouts de tamanhos
diferentes, ele não passa por ``measure_block``; o pareamento do bootstrap é feito pela
interseção de ``bout_id`` dos dois holdouts (ver ``paired_holdout_index``).

O harness é **read-only**: lê ``bout_features``, nunca escreve no banco e nunca commita.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from analysis.dataset import (
    COL_BOUT_ID,
    COL_EVENT_DATE,
    COL_TARGET,
    FIRST_RELIABLE_CORNER_DATE,
    Dataset,
    TemporalSplit,
    build_dataset,
    build_dataset_without_window_filter,
    read_bout_features,
    restrict_to_trainable_columns,
    temporal_split,
)
from analysis.model import ARTIFACTS_DIR, DEFAULT_TEST_FRACTION, RANDOM_STATE, train_model
from ingestion.features.division import (
    DIVISION_BASE_RATE_FEATURES,
    DIVISION_CATEGORY_FEATURES,
)
from ingestion.features.matchup import (
    FORMAT_FEATURES,
    STANCE_MATCHUP_FEATURES,
    STYLE_MATCHUP_FEATURES,
)
from ingestion.features.rolling import (
    BODY_ACCURACY_R3,
    CAREER_MINUTES_BEFORE,
    CLINCH_ACCURACY_R3,
    CONTROL_TIME_AVG_TREND,
    DISTANCE_ACCURACY_R3,
    FIVE_ROUND_BOUTS_BEFORE,
    GRAPPLING_AXIS_R3,
    GROUND_ACCURACY_R3,
    HEAD_ACCURACY_R3,
    KNOCKDOWNS_AVG_R3,
    KNOCKDOWNS_PM_R3,
    KO_LOSSES_PRIOR,
    LEG_ACCURACY_R3,
    OPPONENT_WIN_RATE_PRIOR_AVG,
    ROUND1_SIG_STRIKE_SHARE_R3,
    SHARE_BODY_R3,
    SHARE_CLINCH_R3,
    SHARE_DISTANCE_R3,
    SHARE_GROUND_R3,
    SHARE_HEAD_R3,
    SHARE_LEG_R3,
    SIG_STRIKE_ACCURACY_R3,
    SIG_STRIKES_ABSORBED_PM_TREND,
    SIG_STRIKES_LANDED_PM_TREND,
    SIMILAR_STYLE_WIN_RATE_PRIOR,
    SOUTHPAW_OPPONENTS_FACED_PRIOR,
    SUBMISSION_ATTEMPTS_AVG_R3,
    SUBMISSION_LOSSES_PRIOR,
    TAKEDOWN_ACCURACY_R3,
    TAKEDOWN_DEFENSE_TREND,
    TAKEDOWNS_LANDED_AVG_TREND,
    TOTAL_TO_SIG_STRIKE_RATIO_R3,
    VOLUME_AXIS_R3,
)
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

# Registro por bloco, sob o diretório de artefatos já ignorado pelo git (.gitignore:34).
ABLATION_DIR = ARTIFACTS_DIR / "ablation"
SCHEMA_VERSION = 1

# Convenção de ``ingestion.features.matchup``: toda base as-of numérica é pivotada nos dois
# cantos e ganha o diferencial. Uma feature bout-level não passa por aqui -- é coluna única.
_CORNER_SUFFIXES: tuple[str, ...] = ("_a", "_b", "_diff")

# B fixado pela RF-06. Não é parâmetro a calibrar: é o tamanho que dá resolução suficiente
# aos percentis de cauda do IC 95% sobre um holdout da ordem de milhares de linhas.
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 0
_CI_PERCENTILES: tuple[float, float] = (2.5, 97.5)

# Clip explícito das probabilidades antes do log. Não delegamos ao ``eps`` de nenhuma versão
# de sklearn: o valor mudou entre versões e a régua precisa ser estável ao longo da SPEC.
_PROB_EPS = 1e-15


@dataclass(frozen=True)
class BootstrapDelta:
    """Delta pontual de log-loss e o IC 95% do bootstrap pareado (B reamostragens)."""

    delta: float
    ci_low: float
    ci_high: float
    n_resamples: int
    seed: int


def _log_loss_per_row(y_true: Sequence[int], prob: Sequence[float]) -> list[float]:
    """Perda logarítmica de cada linha: ``-[y·ln(p) + (1-y)·ln(1-p)]``.

    O log-loss agregado é a **média** destas perdas -- é essa identidade que torna o
    bootstrap vetorizado exato (ver ``paired_bootstrap_log_loss_delta``).

    Aritmética em Python puro, sem numpy, e devolvendo ``list[float]``: numpy é fronteira
    dinâmica no ``mypy --strict`` (``follow_imports = "skip"``) e um array devolvido daqui
    propagaria ``Any`` para dentro do harness. Aqui isso não custa nada -- são duas passadas
    sobre o holdout por bloco (milhares de linhas), irrelevantes ao lado das B
    reamostragens, e é lá, no único trecho que de fato precisa de vetorização, que o numpy
    fica confinado.

    O clip é explícito (``_PROB_EPS``) para que uma probabilidade exatamente 0 ou 1 não
    produza ``-inf``, sem depender do ``eps`` de nenhuma versão de sklearn.
    """
    perdas: list[float] = []
    for rotulo, probabilidade in zip(y_true, prob, strict=True):
        p = min(max(float(probabilidade), _PROB_EPS), 1.0 - _PROB_EPS)
        perdas.append(-math.log(p) if rotulo == 1 else -math.log(1.0 - p))
    return perdas


def paired_bootstrap_log_loss_delta(
    y_true: Sequence[int],
    prob_com: Sequence[float],
    prob_sem: Sequence[float],
    *,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> BootstrapDelta:
    """IC 95% do delta ``log_loss(com) - log_loss(sem)`` no **mesmo** holdout.

    Negativo significa que o bloco melhora (log-loss menor é melhor). O pareamento é
    reamostrar **índices de linha** e aplicar os mesmos índices aos dois vetores de
    probabilidade: cada reamostragem compara os dois modelos exatamente nas mesmas lutas,
    o que cancela a variância comum e isola a diferença entre eles.

    Como o log-loss é a média das perdas por linha, a média das *diferenças* por linha
    sobre os índices reamostrados é identicamente o delta daquele reamostre. Daí a
    vetorização ser exata, e não uma aproximação: pré-calculamos o vetor de diferenças uma
    vez e reamostramos só índices, em vez de 2·B chamadas a ``log_loss``.

    Levanta ``ValueError`` se os três vetores tiverem comprimentos diferentes, se o holdout
    for vazio ou se ``n_resamples`` não for positivo -- todos são erro de chamada, não
    veredito negativo.
    """
    n = len(y_true)
    if not (n == len(prob_com) == len(prob_sem)):
        raise ValueError(
            f"Vetores de comprimentos diferentes: y_true={n}, "
            f"prob_com={len(prob_com)}, prob_sem={len(prob_sem)}."
        )
    if n == 0:
        raise ValueError("Holdout vazio: não há linhas para reamostrar.")
    if n_resamples <= 0:
        raise ValueError(f"n_resamples deve ser positivo; recebido: {n_resamples}.")

    per_row = np.asarray(_log_loss_per_row(y_true, prob_com), dtype=np.float64) - np.asarray(
        _log_loss_per_row(y_true, prob_sem), dtype=np.float64
    )
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, n, size=(n_resamples, n))
    deltas = per_row[indices].mean(axis=1)
    ci_low, ci_high = np.percentile(deltas, _CI_PERCENTILES)
    # Conversão na borda: numpy é fronteira dinâmica (``follow_imports = "skip"`` no
    # pyproject), e devolver um escalar numpy de função anotada ``-> float`` propagaria
    # ``Any`` para dentro do harness.
    return BootstrapDelta(
        delta=float(per_row.mean()),
        ci_low=float(ci_low),
        ci_high=float(ci_high),
        n_resamples=n_resamples,
        seed=seed,
    )


@dataclass(frozen=True)
class FeatureBlock:
    """Um bloco de features medido isoladamente contra a linha de base (RF-06).

    ``asof_bases`` são as bases point-in-time por lutador (viram o trio de canto);
    ``bout_level_features`` são as colunas de contexto de luta, que valem para os dois
    cantos e entram sem sufixo.
    """

    name: str
    asof_bases: tuple[str, ...]
    bout_level_features: tuple[str, ...] = ()
    description: str = ""


def block_columns(block: FeatureBlock) -> list[str]:
    """Colunas que o bloco ocupa na matriz de matchup, em ordem estável.

    Uma base as-of vira o trio ``_a``/``_b``/``_diff`` (convenção de
    ``ingestion.features.matchup``); uma feature bout-level é **coluna única**, sem sufixo
    de canto -- virar par produziria um ``_diff`` identicamente zero, ruído puro. É o
    caminho que a Slice 02 da SPEC 009 vai consumir.
    """
    colunas = [f"{base}{suffix}" for base in block.asof_bases for suffix in _CORNER_SUFFIXES]
    colunas.extend(block.bout_level_features)
    return colunas


BLOCK_REGISTRY: dict[str, FeatureBlock] = {
    block.name: block
    for block in (
        FeatureBlock(
            name="striking-profile",
            asof_bases=(
                SHARE_HEAD_R3,
                SHARE_BODY_R3,
                SHARE_LEG_R3,
                SHARE_DISTANCE_R3,
                SHARE_CLINCH_R3,
                SHARE_GROUND_R3,
            ),
            description="Perfil de striking point-in-time do M5 (Slice 06).",
        ),
        FeatureBlock(
            name="dinamica-por-round",
            asof_bases=(ROUND1_SIG_STRIKE_SHARE_R3,),
            description="Dinâmica por round point-in-time do M5 (Slice 06).",
        ),
        FeatureBlock(
            name="dimensoes-ausentes",
            # Os nomes vêm importados de ``rolling``, nunca redigitados: uma string duplicada
            # e desatualizada faria a feature sumir do bloco em silêncio, e o veredito
            # passaria a valer para um bloco menor do que o medido diz ser.
            asof_bases=(
                SIG_STRIKE_ACCURACY_R3,
                HEAD_ACCURACY_R3,
                BODY_ACCURACY_R3,
                LEG_ACCURACY_R3,
                DISTANCE_ACCURACY_R3,
                CLINCH_ACCURACY_R3,
                GROUND_ACCURACY_R3,
                TAKEDOWN_ACCURACY_R3,
                KNOCKDOWNS_PM_R3,
                KNOCKDOWNS_AVG_R3,
                SUBMISSION_ATTEMPTS_AVG_R3,
                TOTAL_TO_SIG_STRIKE_RATIO_R3,
            ),
            description=(
                "Bloco A da SPEC 009 (Slice 01): precisão por dimensão, poder, ameaça de "
                "grappling e trabalho fora do golpe significativo -- 12 bases as-of, 36 "
                "colunas, todas derivadas de colunas já persistidas no granular."
            ),
        ),
        # Bloco B1 da SPEC 009 (Slice 02), em DOIS registros independentes de propósito: a
        # categoria física e a taxa base histórica respondem perguntas diferentes e podem
        # pagar uma sem a outra. Medi-las juntas daria um veredito só, que não diria qual das
        # duas carregou o resultado -- e a RF-06 exige bloco medido isoladamente.
        FeatureBlock(
            name="divisao-categoria",
            asof_bases=(),
            bout_level_features=tuple(DIVISION_CATEGORY_FEATURES),
            description=(
                "Bloco B1a da SPEC 009 (Slice 02): a divisão como categoria física -- limite "
                "de peso real em libras mais o indicador de divisão feminina (as femininas "
                "colidem em peso com as masculinas em 125/135/145). Bout-level: coluna única, "
                "sem sufixo de canto."
            ),
        ),
        FeatureBlock(
            name="divisao-taxa-base",
            asof_bases=(),
            bout_level_features=tuple(DIVISION_BASE_RATE_FEATURES),
            description=(
                "Bloco B1b da SPEC 009 (Slice 02): a taxa de finalização histórica da divisão, "
                "point-in-time por data de evento estritamente anterior. É propriedade do "
                "estilo da divisão, não do alvo -- target encoding da divisão é proibido pela "
                "SPEC. Bout-level: coluna única, sem sufixo de canto."
            ),
        ),
        # Bloco B2 da SPEC 009 (Slice 03), em registro ÚNICO: as três features respondem a
        # mesma pergunta -- o formato da luta --, e a SPEC as agrupa num bloco só. Mistura os
        # dois caminhos de propósito: duas bout-level (coluna única) e uma base as-of por
        # lutador (trio de canto), 5 colunas ao todo.
        FeatureBlock(
            name="formato",
            asof_bases=(FIVE_ROUND_BOUTS_BEFORE,),
            bout_level_features=tuple(FORMAT_FEATURES),
            description=(
                "Bloco B2 da SPEC 009 (Slice 03): luta de título, rounds agendados e "
                "experiência prévia em cinco rounds -- distingue vinte e cinco minutos pela "
                "primeira vez de quem já passou por eles. ``scheduled_rounds`` é o formato "
                "AGENDADO, conhecido antes do gongo (RF-02), não o round em que a luta acabou."
            ),
        ),
        # Bloco C da SPEC 009 (Slice 04), em registro ÚNICO: C1 (como perde), C2
        # (quilometragem) e C3 (tendência) são a mesma tese -- qualificar o histórico além de
        # "quanto vence" -- e o PRD as agrupa. Medi-las em três vereditos separados diria
        # menos, não mais: a pergunta é se qualificar o histórico paga, não qual das três
        # qualificações paga. Tudo por lutador: 8 bases as-of, 24 colunas, zero bout-level.
        FeatureBlock(
            name="historico-qualificado",
            asof_bases=(
                KO_LOSSES_PRIOR,
                SUBMISSION_LOSSES_PRIOR,
                CAREER_MINUTES_BEFORE,
                SIG_STRIKES_LANDED_PM_TREND,
                SIG_STRIKES_ABSORBED_PM_TREND,
                TAKEDOWNS_LANDED_AVG_TREND,
                TAKEDOWN_DEFENSE_TREND,
                CONTROL_TIME_AVG_TREND,
            ),
            description=(
                "Bloco C da SPEC 009 (Slice 04): como o lutador perde (derrotas por nocaute e "
                "por finalização), quanta quilometragem acumulou (minutos de cativeiro) e se "
                "está subindo ou caindo (janela de 3 menos carreira). As gêmeas de carreira "
                "das cinco tendências são intermediárias NÃO emitidas -- emiti-las faria o "
                "bloco medir 'carreira + tendência' em vez de tendência."
            ),
        ),
        # Blocos D1 e D2 da SPEC 009 (Slice 05), em DOIS registros independentes: o confronto
        # de bases e o vetor de estilo respondem perguntas diferentes e podem pagar um sem o
        # outro. Medi-los juntos daria um veredito só, que não diria qual dos dois carregou o
        # resultado -- e a RF-06 exige bloco medido isoladamente. Os dois misturam os dois
        # caminhos: base as-of (trio de canto) e bout-level (coluna única).
        FeatureBlock(
            name="confronto-de-bases",
            asof_bases=(SOUTHPAW_OPPONENTS_FACED_PRIOR,),
            bout_level_features=tuple(STANCE_MATCHUP_FEATURES),
            description=(
                "Bloco D1 da SPEC 009 (Slice 05): base contra base -- duas binárias que "
                "cobrem as três categorias de confronto sem impor ordem falsa, mais a "
                "contagem point-in-time de canhotos já enfrentados. Limitação declarada: "
                "``fighters.stance`` é atributo lento NÃO versionado, então a base do "
                "adversário passado é a de hoje (ver ``rolling.add_stance_history_features``)."
            ),
        ),
        FeatureBlock(
            name="eixos-de-estilo",
            asof_bases=(GRAPPLING_AXIS_R3, VOLUME_AXIS_R3),
            bout_level_features=tuple(STYLE_MATCHUP_FEATURES),
            description=(
                "Bloco D2 da SPEC 009 (Slice 05): o vetor de estilo as-of de cada canto por "
                "fórmula fixa com divisores de domínio (sem PCA, sem fit aprendido), mais a "
                "distância euclidiana entre os dois. O desvio-padrão observado de "
                "``style_distance`` na janela é a aferição de sanidade de σ do bloco D3 -- "
                "reportado, JAMAIS usado para reajustar ``STYLE_KERNEL_SIGMA``."
            ),
        ),
        # Blocos D3 e D4 da SPEC 009 (Slice 06), em DOIS registros independentes: "como ele foi
        # contra gente parecida com este adversário" e "contra quem ele venceu" são perguntas
        # diferentes e podem pagar uma sem a outra. Medi-las juntas daria um veredito só, que
        # não diria qual das duas carregou o resultado -- e a RF-06 exige bloco medido
        # isoladamente. As duas são do LUTADOR (trio de canto), nenhuma é bout-level.
        FeatureBlock(
            name="estilo-semelhante",
            asof_bases=(SIMILAR_STYLE_WIN_RATE_PRIOR,),
            description=(
                "Bloco D3 da SPEC 009 (Slice 06): o histórico do lutador ponderado pela "
                "semelhança entre o adversário de cada luta anterior e o desta -- núcleo "
                "gaussiano que PONDERA e nunca filtra (RF-05), com o adversário mais "
                "dissemelhante possível mantendo 13,5% de peso. O estilo de cada adversário "
                "passado é lido as-of, da linha dele naquela luta (RF-04)."
            ),
        ),
        FeatureBlock(
            name="forca-de-calendario",
            asof_bases=(OPPONENT_WIN_RATE_PRIOR_AVG,),
            description=(
                "Bloco D4 da SPEC 009 (Slice 06): a média do cartel as-of dos adversários "
                "anteriores -- vencer cinco estreantes deixa de valer o mesmo que vencer cinco "
                "ranqueados. Adversário estreante fica fora do denominador (RF-03)."
            ),
        ),
    )
}


# Limiar da RF-09: abaixo disso a coluna é destacada no log. Feature que nasce quase vazia
# não é medida como as outras -- um delta nulo dela significa "não há dado", não "não há
# sinal", e o relatório precisa deixar isso na cara antes de o delta aparecer.
LOW_COVERAGE_THRESHOLD = 0.30

# Rótulo binário do canto vermelho, o mesmo de ``analysis.dataset._CORNER_TO_LABEL``.
_RED_LABEL = 1


@dataclass(frozen=True)
class ColumnCoverage:
    """Preenchimento e dispersão de uma coluna na janela medida (RF-09).

    ``std`` (SPEC 009, Slice 05) é o desvio-padrão observado da coluna na janela, uniforme
    para **todo** bloco -- sem caso especial para ``style_distance``, cuja dispersão é a
    aferição de sanidade de ``STYLE_KERNEL_SIGMA`` do bloco D3. Um campo a mais no relatório
    que já existe é mais simples e mais honesto que um gancho de aferição de uso único.
    O valor é **reportado, nunca usado para reajustar σ nem qualquer constante**: tuning pela
    métrica é não-objetivo declarado do PRD.

    ``None`` quando a dispersão não é medível -- menos de dois valores conhecidos. ``0.0``
    afirmaria "a coluna é constante", leitura diferente de "não dá para dizer", e uma
    aferição que inventa número não afere nada.
    """

    column: str
    n_non_null: int
    n_rows: int
    std: float | None

    @property
    def fraction(self) -> float:
        """Fração preenchida; janela vazia devolve 0,0 em vez de dividir por zero."""
        if self.n_rows == 0:
            return 0.0
        return self.n_non_null / self.n_rows

    @property
    def is_low(self) -> bool:
        """Abaixo do limiar da RF-09 -- destacada no log."""
        return self.fraction < LOW_COVERAGE_THRESHOLD


@dataclass(frozen=True)
class BlockMeasurement:
    """Resultado completo da medição de um bloco: contexto, cobertura, delta e veredito."""

    block: str
    measured_at: datetime
    window_start: date
    window_end: date
    boundary_date: date
    n_train: int
    n_test: int
    random_state: int
    n_features_with: int
    block_columns: list[str]
    missing_columns: list[str]
    coverage: list[ColumnCoverage]
    log_loss_with: float
    log_loss_without: float
    bootstrap: BootstrapDelta

    @property
    def delta(self) -> float:
        """Delta pontual do bootstrap: ``log_loss(com) - log_loss(sem)``."""
        return self.bootstrap.delta

    @property
    def adopted(self) -> bool:
        """Veredito pré-comprometido da RF-07: adota-se só o que não cruza zero.

        ``False`` é desfecho **de sucesso** do slice, não falha: o registro do que não pagou
        é entregável. Nada neste módulo levanta nem sai com código diferente de zero por
        causa deste valor.
        """
        return self.bootstrap.ci_high < 0.0


def _observed_std(values: pd.Series) -> float | None:
    """Desvio-padrão amostral da coluna, ou ``None`` quando não é medível.

    Conversão na borda: ``Series.std`` devolve escalar do numpy (fronteira dinâmica) e
    ``NaN`` quando há menos de dois valores conhecidos -- os dois casos precisam virar tipo
    Python antes de entrar no dataclass e no JSON, onde ``NaN`` sequer é válido.
    """
    valor = values.std()
    return None if pd.isna(valor) else float(valor)


def column_coverage(frame: pd.DataFrame, columns: Sequence[str]) -> list[ColumnCoverage]:
    """Cobertura e dispersão de cada coluna dada, na ordem em que foram pedidas.

    Medidas sobre a janela **inteira** (treino + holdout): a pergunta da RF-09 é "esta
    feature existe no dado, e com que espalhamento?", que independe de onde caiu a fronteira
    do split.
    """
    n_rows = len(frame)
    return [
        ColumnCoverage(
            column=column,
            n_non_null=int(frame[column].notna().sum()),
            n_rows=n_rows,
            std=_observed_std(frame[column]),
        )
        for column in columns
    ]


def _warn_low_coverage(block: FeatureBlock, coverage: Sequence[ColumnCoverage]) -> None:
    """Alarme de cobertura abaixo do limiar da RF-09, emitido na hora da medição.

    Só o destaque mora aqui, não a listagem completa: o aviso é alarme -- precisa sair alto
    para **qualquer** chamador de ``measure_block``, inclusive quem não passa pelo CLI --,
    enquanto a listagem coluna a coluna é parte do relatório ordenado e vive em
    ``_log_measurement``. Emitir as duas coisas nos dois lugares duplicaria a cobertura no
    log e a faria aparecer antes da janela, quebrando a ordem que o CA-06 exige.
    """
    for item in coverage:
        if item.is_low:
            logger.warning(
                "Cobertura baixa em %s.%s: %.1f%% (%d/%d linhas) -- abaixo do limiar de "
                "%.0f%%; um delta nulo aqui pode significar ausência de dado, não ausência "
                "de sinal.",
                block.name,
                item.column,
                item.fraction * 100.0,
                item.n_non_null,
                item.n_rows,
                LOW_COVERAGE_THRESHOLD * 100.0,
            )


def _log_loss_of(
    x_train: pd.DataFrame,
    split: TemporalSplit,
    columns: list[str],
    random_state: int,
) -> tuple[float, list[float]]:
    """Treina no subconjunto de colunas dado e devolve o log-loss e as probabilidades.

    A probabilidade da classe positiva é lida pelo **índice de ``model.classes_``**, nunca
    pela coluna 1 presumida -- é a mesma leitura de ``analysis.walk_forward._predict_step``.
    """
    model = train_model(x_train[columns], split.y_train, random_state)
    prob = model.predict_proba(split.x_test[columns])[:, list(model.classes_).index(_RED_LABEL)]
    prob_red = [float(valor) for valor in prob]
    perdas = _log_loss_per_row([int(rotulo) for rotulo in split.y_test], prob_red)
    return sum(perdas) / len(perdas), prob_red


def measure_block(
    split: TemporalSplit,
    block: FeatureBlock,
    *,
    random_state: int = RANDOM_STATE,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> BlockMeasurement:
    """Treina dois modelos que diferem **apenas** nas colunas do bloco (RF-06).

    Ordem interna, que é o contrato da RF-09: resolver as colunas presentes e ausentes ->
    medir a cobertura -> treinar com o bloco -> treinar sem ele -> log-loss dos dois ->
    bootstrap pareado sobre o mesmo holdout.

    Quando o bloco resolve para zero colunas presentes, os dois modelos são treinados
    **mesmo assim**, sem atalho: é o caminho real que prova o delta exatamente zero da
    CA-02. Um atalho ("se não há colunas, devolve zero") tornaria esse teste vazio, provando
    apenas que o atalho existe.
    """
    todas = list(split.x_train.columns)
    esperadas = block_columns(block)
    presentes = [coluna for coluna in esperadas if coluna in todas]
    ausentes = [coluna for coluna in esperadas if coluna not in todas]
    if ausentes:
        logger.warning(
            "Bloco %s: %d coluna(s) não estão no split e não podem ser medidas: %s.",
            block.name,
            len(ausentes),
            ", ".join(ausentes),
        )

    janela = pd.concat([split.x_train, split.x_test])
    coverage = column_coverage(janela, presentes)
    _warn_low_coverage(block, coverage)

    sem_o_bloco = [coluna for coluna in todas if coluna not in set(presentes)]
    log_loss_with, prob_com = _log_loss_of(split.x_train, split, todas, random_state)
    log_loss_without, prob_sem = _log_loss_of(split.x_train, split, sem_o_bloco, random_state)
    bootstrap = paired_bootstrap_log_loss_delta(
        [int(rotulo) for rotulo in split.y_test],
        prob_com,
        prob_sem,
        n_resamples=n_resamples,
        seed=seed,
    )
    return BlockMeasurement(
        block=block.name,
        measured_at=datetime.now(UTC),
        window_start=split.event_train.min(),
        window_end=split.event_test.max(),
        boundary_date=split.boundary_date,
        n_train=len(split.y_train),
        n_test=len(split.y_test),
        random_state=random_state,
        n_features_with=len(todas),
        block_columns=presentes,
        missing_columns=ausentes,
        coverage=coverage,
        log_loss_with=log_loss_with,
        log_loss_without=log_loss_without,
        bootstrap=bootstrap,
    )


def _measurement_payload(measurement: BlockMeasurement) -> dict[str, object]:
    """Payload do registro, com a cobertura **antes** das chaves de delta (RF-09).

    A ordem das chaves é contrato de leitura, não estética: quem abre o JSON encontra a
    cobertura de cada coluna antes de encontrar o delta, e um delta nulo já chega
    acompanhado da informação que o explica.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "block": measurement.block,
        "measured_at": measurement.measured_at.isoformat(),
        "window_start": measurement.window_start.isoformat(),
        "window_end": measurement.window_end.isoformat(),
        "boundary_date": measurement.boundary_date.isoformat(),
        "n_train": measurement.n_train,
        "n_test": measurement.n_test,
        "random_state": measurement.random_state,
        "n_features_with": measurement.n_features_with,
        "block_columns": measurement.block_columns,
        "missing_columns": measurement.missing_columns,
        "coverage": [
            {
                "column": item.column,
                "n_non_null": item.n_non_null,
                "n_rows": item.n_rows,
                "fraction": item.fraction,
                "std": item.std,
            }
            for item in measurement.coverage
        ],
        "log_loss_with": measurement.log_loss_with,
        "log_loss_without": measurement.log_loss_without,
        "delta": measurement.bootstrap.delta,
        "ci_low": measurement.bootstrap.ci_low,
        "ci_high": measurement.bootstrap.ci_high,
        "n_resamples": measurement.bootstrap.n_resamples,
        "seed": measurement.bootstrap.seed,
        "adopted": measurement.adopted,
    }


def save_measurement(measurement: BlockMeasurement, directory: Path = ABLATION_DIR) -> Path:
    """Persiste o registro da medição em ``<bloco>.json`` e devolve o caminho.

    **Sobrescreve de propósito** -- e é aqui que este módulo difere de
    ``analysis.walk_forward.save_run``, que nomeia o arquivo pelo instante justamente para
    acumular a série. A diferença não é descuido: uma curva de walk-forward só significa
    algo ao lado das anteriores, enquanto o registro de ablação é o **veredito corrente**
    daquele bloco sobre a base corrente. Duas medições do mesmo bloco sobre a mesma base são
    a mesma medição (o harness é determinístico); sobre bases diferentes, a antiga está
    obsoleta. Não "conserte" isto para a série do walk-forward.

    Veredito negativo (``adopted: false``) é gravado como qualquer outro: bloco que não paga
    é desfecho de sucesso do slice (RF-07), nunca erro.
    """
    return _write_record(directory, measurement.block, _measurement_payload(measurement))


def _write_record(directory: Path, block: str, payload: dict[str, object]) -> Path:
    """Grava ``<bloco>.json`` no diretório de registros e devolve o caminho.

    Um só lugar define o nome do arquivo e o formato do despejo, para os dois tipos de
    medição desta SPEC -- a de bloco de colunas e a da correção da janela. Registros com
    nomes ou codificações divergentes tornariam o diretório de artefatos ilegível como
    conjunto.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{block}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_ablation(
    session: Session,
    blocks: Sequence[FeatureBlock],
    *,
    test_fraction: float = DEFAULT_TEST_FRACTION,
    random_state: int = RANDOM_STATE,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> list[BlockMeasurement]:
    """Mede N blocos sobre o **mesmo** dataset e o **mesmo** split (RF-06). Read-only.

    O dataset e o split são construídos **uma vez**, fora do laço. Não é otimização: medir
    dois blocos sobre splits construídos separadamente violaria a RF-06 em silêncio -- os
    deltas deixariam de ser comparáveis entre si, que é justamente o que a régua existe para
    garantir.

    Não escreve nada no banco e não commita.
    """
    dataset = build_dataset(read_bout_features(session))
    if len(dataset.target) == 0:
        raise ValueError("bout_features não tem linhas com alvo definido; nada a medir.")
    split = restrict_to_trainable_columns(temporal_split(dataset, test_fraction))
    return [
        measure_block(split, block, random_state=random_state, n_resamples=n_resamples, seed=seed)
        for block in blocks
    ]


# --- Bloco especial: correção da janela do treino (Slice 00B) -----------------------------

# Nome do bloco especial. Não vive em ``BLOCK_REGISTRY`` porque não é um conjunto de colunas:
# é a comparação de duas **variantes de dataset**, com holdouts de tamanhos diferentes -- e é
# justamente por isso que não passa por ``measure_block``.
WINDOW_BLOCK_NAME = "janela-canto-fabricado"

# Janela anterior do filtro de treino (ADR 0006 antes da Emenda 1). Vive aqui, no harness de
# medição, e em nenhum caminho de produção: é registro **histórico** para reproduzir o braço
# "antes", não um corte alternativo que alguém possa escolher.
PREVIOUS_TRAIN_WINDOW_START: Final[date] = date(2010, 1, 1)

# Rótulo do canto vermelho na frame crua, o mesmo de ``analysis.dataset._CORNER_TO_LABEL``.
_RED_CORNER = "red"


@dataclass(frozen=True)
class _Arm:
    """Um braço da comparação: o dataset, o split e o holdout indexado por ``bout_id``."""

    dataset: Dataset
    split: TemporalSplit
    prob: pd.Series
    y_true: pd.Series


@dataclass(frozen=True)
class WindowCorrectionMeasurement:
    """Medição da correção da janela do treino: dois braços, holdout pareado por ``bout_id``.

    Os dois log-losses são medidos no **holdout pareado**, não cada um no seu holdout: só
    assim ``log_loss_after - log_loss_before`` é identicamente ``delta``. Reportar cada braço
    sobre o seu próprio recorte daria dois números cuja diferença não é o efeito medido.
    """

    measured_at: datetime
    window_before: date
    window_after: date
    removed_bout_ids: list[int]
    red_win_rate_removed: float | None
    n_dataset_before: int
    n_dataset_after: int
    n_train_before: int
    n_train_after: int
    n_test_before: int
    n_test_after: int
    n_paired_holdout: int
    n_features_before: int
    n_features_after: int
    boundary_date_before: date
    boundary_date_after: date
    random_state: int
    log_loss_before: float
    log_loss_after: float
    bootstrap: BootstrapDelta

    @property
    def n_removed(self) -> int:
        """Quantas lutas a correção tira do dataset."""
        return len(self.removed_bout_ids)

    @property
    def delta(self) -> float:
        """``log_loss(com a correção) - log_loss(sem)``; negativo = a correção melhora."""
        return self.bootstrap.delta

    @property
    def ci_low(self) -> float:
        """Percentil 2,5 do delta reamostrado."""
        return self.bootstrap.ci_low

    @property
    def ci_high(self) -> float:
        """Percentil 97,5 do delta reamostrado."""
        return self.bootstrap.ci_high

    @property
    def adopted(self) -> bool:
        """Veredito **métrico** da RF-07 (``ci_high < 0``) -- e nada além disso.

        **Não é a decisão de adoção da janela.** A correção permanece com IC cruzando zero:
        não se treina com rótulo que sabemos ser falso, e a decisão é de princípio, idêntica
        à da ADR 0006 original e fechada na SPEC 009 antes de qualquer número. Este campo
        registra o que a métrica diz, para que o que ela diz fique no arquivo.
        """
        return self.bootstrap.ci_high < 0.0


def paired_holdout_index(y_true_before: pd.Series, y_true_after: pd.Series) -> pd.Index:
    """Lutas presentes nos **dois** holdouts, com o alvo conferido (CA-04).

    O bootstrap pareado exige as mesmas linhas nos dois braços, e os dois splits não
    coincidem: remover lutas do começo da série empurra lutas do holdout para o treino da
    variante corrigida. Medir sobre elas compararia um modelo que as treinou com um que não
    as treinou -- pareamento falso, com aparência de rigor. A interseção por ``bout_id`` as
    exclui de propósito.

    Levanta ``ValueError`` se não houver interseção (o pareamento é impossível) ou se a mesma
    ``bout_id`` tiver alvo diferente nos dois braços (a medição não estaria comparando a mesma
    luta). Nenhum dos dois é veredito negativo: são medição inválida, e falham alto.
    """
    comuns = y_true_before.index.intersection(y_true_after.index)
    if len(comuns) == 0:
        raise ValueError(
            "Holdouts sem interseção de bout_id: o pareamento do bootstrap é impossível."
        )
    if not y_true_before.loc[comuns].equals(y_true_after.loc[comuns]):
        raise ValueError(
            "Alvo divergente entre os braços para a mesma luta; medição inválida -- os dois "
            "braços precisam concordar sobre quem venceu para que o pareamento signifique algo."
        )
    return comuns


def _build_arm(rows: pd.DataFrame, test_fraction: float, random_state: int) -> _Arm:
    """Um braço: dataset -> split temporal -> modelo -> probabilidade do holdout por ``bout_id``.

    Recebe as linhas **já recortadas** pela janela daquele braço e usa o caminho sem filtro:
    a variante histórica é menos restritiva que a de produção e não se obtém filtrando a
    saída de ``build_dataset`` por cima.
    """
    dataset = build_dataset_without_window_filter(rows)
    split = restrict_to_trainable_columns(temporal_split(dataset, test_fraction))
    _, prob = _log_loss_of(split.x_train, split, list(split.x_train.columns), random_state)
    # ``bout_id`` compartilha o índice do dataset, e o split preserva os rótulos desse índice
    # nas duas fatias -- é o que permite reindexar o holdout pela chave natural da luta, que
    # é a única coisa comparável entre dois datasets de tamanhos diferentes.
    ids_do_teste = [int(valor) for valor in dataset.bout_id.loc[split.y_test.index]]
    return _Arm(
        dataset=dataset,
        split=split,
        prob=pd.Series(prob, index=ids_do_teste),
        y_true=pd.Series([int(rotulo) for rotulo in split.y_test], index=ids_do_teste),
    )


def _removed_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """Lutas **decididas** do intervalo que a correção remove: ``[2010-01-01, 2010-03-21)``.

    Só as decididas: as de alvo nulo (NC/empate) não estão no dataset de nenhum dos braços,
    então não são removidas por esta correção. É o que faz
    ``n_dataset_before - n_dataset_after`` bater com a contagem reportada.
    """
    no_intervalo = (rows[COL_EVENT_DATE] >= PREVIOUS_TRAIN_WINDOW_START) & (
        rows[COL_EVENT_DATE] < FIRST_RELIABLE_CORNER_DATE
    )
    return rows[no_intervalo & rows[COL_TARGET].notna()]


def measure_window_correction(
    raw: pd.DataFrame,
    *,
    test_fraction: float = DEFAULT_TEST_FRACTION,
    random_state: int = RANDOM_STATE,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> WindowCorrectionMeasurement:
    """Mede a correção da janela do treino de 2010-01-01 para 2010-03-21 (CA-04, CA-05).

    Braço "antes": a variante **histórica**, com a janela da ADR 0006 original -- não a
    ausência de janela. Braço "depois": o dataset de produção, que já aplica a janela
    corrigida. Os dois são construídos e treinados sempre, mesmo quando nada há a remover:
    um atalho ("sem removidas, delta zero") tornaria vazio o teste que prova o delta
    degenerado.

    O pareamento é a interseção por ``bout_id`` dos dois holdouts (ver
    ``paired_holdout_index``), e os dois log-losses são medidos nela.
    """
    antes = _build_arm(
        raw[raw[COL_EVENT_DATE] >= PREVIOUS_TRAIN_WINDOW_START], test_fraction, random_state
    )
    depois = _build_arm(
        raw[raw[COL_EVENT_DATE] >= FIRST_RELIABLE_CORNER_DATE], test_fraction, random_state
    )

    comuns = paired_holdout_index(antes.y_true, depois.y_true)
    y_true = [int(rotulo) for rotulo in depois.y_true.loc[comuns]]
    prob_sem = [float(valor) for valor in antes.prob.loc[comuns]]
    prob_com = [float(valor) for valor in depois.prob.loc[comuns]]
    bootstrap = paired_bootstrap_log_loss_delta(
        y_true, prob_com, prob_sem, n_resamples=n_resamples, seed=seed
    )

    removidas = _removed_rows(raw)
    n_removidas = len(removidas)
    return WindowCorrectionMeasurement(
        measured_at=datetime.now(UTC),
        window_before=PREVIOUS_TRAIN_WINDOW_START,
        window_after=FIRST_RELIABLE_CORNER_DATE,
        # Ordenado: ``read_bout_features`` não tem ``ORDER BY`` e a ordem das linhas cruas não
        # é estável entre execuções. O delta não depende dela (o split ordena por
        # ``(event_date, bout_id)``), mas o registro em disco depende -- e a RF-11 pede que
        # duas execuções produzam o mesmo arquivo, não só o mesmo IC.
        removed_bout_ids=sorted(int(valor) for valor in removidas[COL_BOUT_ID]),
        red_win_rate_removed=(
            None
            if n_removidas == 0
            else float((removidas[COL_TARGET] == _RED_CORNER).sum()) / n_removidas
        ),
        n_dataset_before=len(antes.dataset.target),
        n_dataset_after=len(depois.dataset.target),
        n_train_before=len(antes.split.y_train),
        n_train_after=len(depois.split.y_train),
        n_test_before=len(antes.split.y_test),
        n_test_after=len(depois.split.y_test),
        n_paired_holdout=len(comuns),
        n_features_before=len(antes.split.x_train.columns),
        n_features_after=len(depois.split.x_train.columns),
        boundary_date_before=antes.split.boundary_date,
        boundary_date_after=depois.split.boundary_date,
        random_state=random_state,
        log_loss_before=_mean_log_loss(y_true, prob_sem),
        log_loss_after=_mean_log_loss(y_true, prob_com),
        bootstrap=bootstrap,
    )


def _mean_log_loss(y_true: Sequence[int], prob: Sequence[float]) -> float:
    """Log-loss agregado: a média das perdas por linha, no recorte que lhe for dado."""
    perdas = _log_loss_per_row(y_true, prob)
    return sum(perdas) / len(perdas)


def run_window_correction(
    session: Session,
    *,
    test_fraction: float = DEFAULT_TEST_FRACTION,
    random_state: int = RANDOM_STATE,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> WindowCorrectionMeasurement:
    """Lê ``bout_features`` e mede a correção da janela. Read-only, não commita."""
    return measure_window_correction(
        read_bout_features(session),
        test_fraction=test_fraction,
        random_state=random_state,
        n_resamples=n_resamples,
        seed=seed,
    )


def _window_payload(measurement: WindowCorrectionMeasurement) -> dict[str, object]:
    """Payload do registro da correção da janela, na ordem em que o CA-06 manda reportar."""
    return {
        "schema_version": SCHEMA_VERSION,
        "block": WINDOW_BLOCK_NAME,
        "measured_at": measurement.measured_at.isoformat(),
        "window_before": measurement.window_before.isoformat(),
        "window_after": measurement.window_after.isoformat(),
        "n_removed": measurement.n_removed,
        "removed_bout_ids": measurement.removed_bout_ids,
        "red_win_rate_removed": measurement.red_win_rate_removed,
        "n_dataset_before": measurement.n_dataset_before,
        "n_dataset_after": measurement.n_dataset_after,
        "n_train_before": measurement.n_train_before,
        "n_train_after": measurement.n_train_after,
        "n_test_before": measurement.n_test_before,
        "n_test_after": measurement.n_test_after,
        "n_paired_holdout": measurement.n_paired_holdout,
        "n_features_before": measurement.n_features_before,
        "n_features_after": measurement.n_features_after,
        "boundary_date_before": measurement.boundary_date_before.isoformat(),
        "boundary_date_after": measurement.boundary_date_after.isoformat(),
        "random_state": measurement.random_state,
        "log_loss_before": measurement.log_loss_before,
        "log_loss_after": measurement.log_loss_after,
        "delta": measurement.delta,
        "ci_low": measurement.ci_low,
        "ci_high": measurement.ci_high,
        "n_resamples": measurement.bootstrap.n_resamples,
        "seed": measurement.bootstrap.seed,
        "adopted": measurement.adopted,
    }


def save_window_measurement(
    measurement: WindowCorrectionMeasurement, directory: Path = ABLATION_DIR
) -> Path:
    """Persiste o registro da correção da janela em ``janela-canto-fabricado.json``."""
    return _write_record(directory, WINDOW_BLOCK_NAME, _window_payload(measurement))


def _log_window_measurement(measurement: WindowCorrectionMeasurement) -> None:
    """Reporta a medição da janela na ordem exigida pelo CA-06.

    Janela antes/depois -> lutas removidas (contagem e taxa do vermelho) -> tamanhos de
    dataset/treino/holdout -> holdout pareado -> log-loss dos dois braços -> delta -> IC 95%
    -> veredito. A taxa do vermelho nas removidas vem antes do delta pelo mesmo motivo que a
    cobertura vem antes nos blocos de coluna: é ela que sustenta o diagnóstico, e um delta
    lido sem ela é um número sem causa.
    """
    logger.info(
        "Janela do treino do bloco %s: antes %s, depois %s.",
        WINDOW_BLOCK_NAME,
        measurement.window_before.isoformat(),
        measurement.window_after.isoformat(),
    )
    taxa = (
        "sem lutas no intervalo"
        if measurement.red_win_rate_removed is None
        else f"{measurement.red_win_rate_removed * 100.0:.1f}% de vitórias do vermelho"
    )
    logger.info(
        "Lutas removidas pelo bloco %s: %d (%s).", WINDOW_BLOCK_NAME, measurement.n_removed, taxa
    )
    logger.info(
        "Dataset %d -> %d, treino %d -> %d, holdout %d -> %d; fronteira do split %s -> %s; "
        "features treináveis %d -> %d.",
        measurement.n_dataset_before,
        measurement.n_dataset_after,
        measurement.n_train_before,
        measurement.n_train_after,
        measurement.n_test_before,
        measurement.n_test_after,
        measurement.boundary_date_before.isoformat(),
        measurement.boundary_date_after.isoformat(),
        measurement.n_features_before,
        measurement.n_features_after,
    )
    logger.info(
        "Holdout pareado (interseção de bout_id dos dois braços): %d luta(s); %d luta(s) do "
        "holdout histórico migraram para o treino da variante corrigida e ficam de fora.",
        measurement.n_paired_holdout,
        measurement.n_test_before - measurement.n_paired_holdout,
    )
    logger.info(
        "Bloco %s: log-loss antes=%.6f, depois=%.6f (ambos no holdout pareado).",
        WINDOW_BLOCK_NAME,
        measurement.log_loss_before,
        measurement.log_loss_after,
    )
    logger.info(
        "Delta do bloco %s: %+.6f (negativo = a correção melhora).",
        WINDOW_BLOCK_NAME,
        measurement.delta,
    )
    logger.info(
        "IC 95%% do bloco %s: [%+.6f, %+.6f] (bootstrap pareado, B=%d, semente=%d).",
        WINDOW_BLOCK_NAME,
        measurement.ci_low,
        measurement.ci_high,
        measurement.bootstrap.n_resamples,
        measurement.bootstrap.seed,
    )
    if measurement.adopted:
        veredito = "IC 95% inteiro abaixo de zero -- a correção também paga pela métrica."
    else:
        veredito = (
            "IC 95% cruza zero. Desfecho válido da medição (RF-07), não falha -- e a correção "
            "da janela PERMANECE: não se treina com rótulo que sabemos ser falso. A adoção é "
            "decisão de princípio (ADR 0006, SPEC 009), nunca da métrica."
        )
    logger.info("Veredito do bloco %s: %s", WINDOW_BLOCK_NAME, veredito)


def _log_measurement(measurement: BlockMeasurement) -> None:
    """Reporta a medição na ordem exigida pelo CA-06.

    Janela -> cobertura -> nº de colunas -> log-loss com/sem -> delta -> IC 95% -> veredito.
    A cobertura vem **antes** do delta de propósito (RF-09): coluna vazia explica um delta
    nulo antes de ele ser interpretado como ausência de sinal.
    """
    logger.info(
        "Janela medida do bloco %s: [%s, %s], fronteira do split em %s "
        "(n_train=%d, n_test=%d, random_state=%d).",
        measurement.block,
        measurement.window_start.isoformat(),
        measurement.window_end.isoformat(),
        measurement.boundary_date.isoformat(),
        measurement.n_train,
        measurement.n_test,
        measurement.random_state,
    )
    _log_coverage_report(measurement)
    logger.info(
        "Colunas do bloco %s: %d presente(s) de %d no split, %d ausente(s)%s.",
        measurement.block,
        len(measurement.block_columns),
        measurement.n_features_with,
        len(measurement.missing_columns),
        "" if not measurement.missing_columns else f": {', '.join(measurement.missing_columns)}",
    )
    logger.info(
        "Bloco %s: log-loss com=%.6f, sem=%.6f.",
        measurement.block,
        measurement.log_loss_with,
        measurement.log_loss_without,
    )
    logger.info(
        "Delta do bloco %s: %+.6f (negativo = o bloco melhora).",
        measurement.block,
        measurement.delta,
    )
    logger.info(
        "IC 95%% do bloco %s: [%+.6f, %+.6f] (bootstrap pareado, B=%d, semente=%d).",
        measurement.block,
        measurement.bootstrap.ci_low,
        measurement.bootstrap.ci_high,
        measurement.bootstrap.n_resamples,
        measurement.bootstrap.seed,
    )
    if measurement.adopted:
        veredito = "ADOTADO -- o IC 95% fica inteiro abaixo de zero."
    else:
        veredito = (
            "NÃO ADOTADO -- o IC 95% cruza zero. Desfecho válido da medição (RF-07), "
            "não falha: o registro do que não pagou é entregável."
        )
    logger.info("Veredito do bloco %s: %s", measurement.block, veredito)


def _log_coverage_report(measurement: BlockMeasurement) -> None:
    """Lista cobertura e dispersão coluna a coluna, entre a janela e o delta (RF-09, CA-06).

    É a única listagem completa da cobertura no log -- o alarme de coluna abaixo do limiar
    sai antes, na medição (``_warn_low_coverage``), e aqui reaparece apenas como o marcador
    ``BAIXA`` ao lado do número.

    O desvio-padrão observado sai na **mesma linha** e para todo bloco: é onde a aferição de
    sanidade de σ do bloco D3 aparece quando o bloco medido é ``eixos-de-estilo``, sem que o
    relatório precise de um caminho especial para ele. Aferição, não insumo de ajuste.
    """
    if not measurement.coverage:
        logger.info("Cobertura do bloco %s: nenhuma coluna presente no split.", measurement.block)
        return
    for item in measurement.coverage:
        dispersao = (
            "desvio-padrão indeterminado"
            if item.std is None
            else f"desvio-padrão observado {item.std:.6f}"
        )
        logger.info(
            "Cobertura de %s.%s: %.1f%% (%d/%d linhas), %s%s",
            measurement.block,
            item.column,
            item.fraction * 100.0,
            item.n_non_null,
            item.n_rows,
            dispersao,
            " -- BAIXA" if item.is_low else "",
        )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de ``python -m analysis.ablation``."""
    parser = argparse.ArgumentParser(
        description=(
            "Mede o ganho de um bloco de features por ablação, com tudo mais fixo, e "
            "quantifica a incerteza por bootstrap pareado (IC 95% do delta de log-loss)."
        ),
    )
    alvo = parser.add_mutually_exclusive_group(required=True)
    alvo.add_argument(
        "--block",
        choices=sorted([*BLOCK_REGISTRY, WINDOW_BLOCK_NAME]),
        help="Nome do bloco a medir. Um nome inválido é erro de uso, listado pelo argparse.",
    )
    alvo.add_argument(
        "--all",
        action="store_true",
        help=(
            "Mede todos os blocos registrados sobre o mesmo dataset e o mesmo split, mais o "
            "bloco especial da correção da janela do treino."
        ),
    )
    parser.add_argument(
        "--test-fraction",
        type=float,
        default=DEFAULT_TEST_FRACTION,
        help="Fração final da série reservada como holdout temporal.",
    )
    parser.add_argument(
        "--random-state",
        type=int,
        default=RANDOM_STATE,
        help="Semente do classificador; a mesma nos dois modelos de cada bloco.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """``python -m analysis.ablation --block <nome>`` ou ``--all``.

    Persiste primeiro, reporta depois -- mesma ordem de ``analysis.walk_forward.main``: uma
    falha de apresentação não pode custar minutos de medição já feita.

    Sai sempre com código 0 quando a medição roda. Bloco não adotado é desfecho válido, não
    falha (RF-07). Read-only: nenhuma escrita no banco, nenhum ``commit``.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    # O bloco da janela é o **único** especial (compara variantes de dataset, não conjuntos de
    # colunas), então o despacho é um ``if`` por nome. Um registro polimórfico de "estratégia
    # de bloco" seria abstração de uso único -- YAGNI.
    mede_janela = bool(args.all) or args.block == WINDOW_BLOCK_NAME
    blocos: list[FeatureBlock] = []
    if args.all:
        blocos = [BLOCK_REGISTRY[nome] for nome in sorted(BLOCK_REGISTRY)]
    elif args.block != WINDOW_BLOCK_NAME:
        blocos = [BLOCK_REGISTRY[args.block]]

    with SessionLocal() as session:
        medicoes = (
            run_ablation(
                session,
                blocos,
                test_fraction=args.test_fraction,
                random_state=args.random_state,
                n_resamples=BOOTSTRAP_RESAMPLES,
            )
            if blocos
            else []
        )
        janela = (
            run_window_correction(
                session,
                test_fraction=args.test_fraction,
                random_state=args.random_state,
                n_resamples=BOOTSTRAP_RESAMPLES,
            )
            if mede_janela
            else None
        )
    caminhos = [save_measurement(medicao, ABLATION_DIR) for medicao in medicoes]
    for medicao, caminho in zip(medicoes, caminhos, strict=True):
        _log_measurement(medicao)
        logger.info("Registro do bloco %s salvo em: %s", medicao.block, caminho)
    if janela is not None:
        caminho_da_janela = save_window_measurement(janela, ABLATION_DIR)
        _log_window_measurement(janela)
        logger.info("Registro do bloco %s salvo em: %s", WINDOW_BLOCK_NAME, caminho_da_janela)


if __name__ == "__main__":
    main()
