"""Matriz de confronto (matchup) bout-level com diferenciais, alvo e baseline.

Núcleo da Slice 04 da SPEC 005 (M4 -- prontidão preditiva). Pivota a frame longa
por lutador-luta (já enriquecida pelas Slices 02/03) de volta para **uma linha por
bout**, com as features as-of dos dois cantos (``*_a`` = red, ``*_b`` = blue), os
diferenciais (A menos B, ``*_diff``), a coluna-alvo ``winner_corner`` **separada**
das features, e o **baseline ingênuo** (taxa de vitória do corner vermelho) --
estatística descritiva, sem treino nem split.

Convenção fixa: **A = red, B = blue** (vernáculo do octógono; ADR 0001). O baseline
mede exatamente ``P(winner_corner == "R")`` -- o ~0,58 esperado depende dessa
convenção.

Contrato de entrada (Slice 01, reconciliado contra o código real): a frame longa
carrega ``result`` por canto (win/loss/no_contest/draw), **não** ``winner_id`` (ver
``LONG_FRAME_COLUMNS``). O alvo é derivado de ``result_a`` -- que já distingue
NC/draw -- em vez de comparar ``winner_id`` com ``fighter_id`` (o snippet do plano
foi escrito contra um contrato presumido). NC/draw são excluídos e contabilizados
(decisão #4 da SPEC).

Column-agnóstico para features: o módulo não lista feature por feature. Deriva os
diferenciais e as colunas de feature das colunas que sobrevivem ao pivô, excluindo
um conjunto conhecido e estável de identidade/contexto/desfecho (``_NON_FEATURE_BASES``)
-- assim sobrevive a Slices 02/03 acrescentarem/renomearem features as-of. O
box-score bruto da própria luta (golpes, quedas, control time do bout corrente) é
desfecho, não predição as-of: fica fora das features (anti-leakage, RF-05/RNF).

Caminho **bout-level** (SPEC 009, Slice 02): atributo que vale para a luta inteira -- e
não para um canto -- é colapsado do par degenerado ``*_a``/``*_b`` em coluna única
(``collapse_bout_context_columns``) antes dos diferenciais, e as derivadas dele entram
sem sufixo de canto. O conhecimento de domínio dessas derivadas mora **fora** daqui
(``ingestion.features.division``), para preservar o column-agnosticismo acima: este
módulo pivota, classifica e ordena; quem sabe o que é uma divisão é o outro módulo.

Duas famílias bout-level, porém, moram aqui de propósito (SPEC 009, Slice 05): o confronto
de bases (D1) e a distância de estilo (D2). Elas não traduzem um rótulo de domínio -- são
**relações entre os dois cantos**, que só existem depois do pivô e em lugar nenhum antes
dele. Um módulo próprio para cada uma seria abstração de uso único; o mapa de divisões, ao
contrário, é conhecimento de domínio com vida própria.

**Degradação declarada no serving** (SPEC 009, CA-16, e "Decisões fechadas" item 2):
``analysis.predict`` reconstrói a cadeia de features à mão em ``_asof_matchup_rows`` e
**não** chama este orquestrador, então nenhuma feature bout-level existe no confronto
hipotético -- todas viram ``NaN`` pelo ``reindex`` de ``_align_features``, e o
``HistGradientBoostingClassifier`` trata a ausência nativamente. Não é ponta solta escondida:
é limitação aceita e testada (``tests/analysis/test_predict.py``), com a propagação do
contexto ao serving já decidida como PRD próprio posterior. Os eixos de estilo por lutador,
esses, **chegam** ao serving de graça -- nascem dentro de ``rolling.add_recent_form_features``,
que ``predict`` chama.

O DataFrame do Pandas é fronteira dinâmica (``pyproject.toml`` marca ``pandas.*``
como ``follow_imports=skip``): as funções públicas recebem/devolvem ``pd.DataFrame``
tipado. Esta slice **não** persiste nada -- a materialização é a Slice 05.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd
from pandas.api.types import is_numeric_dtype

from apps.bouts.enums import Corner
from apps.fighters.enums import Stance
from ingestion.features.division import (
    DIVISION_BASE_RATE_FEATURES,
    DIVISION_CATEGORY_FEATURES,
    DIVISION_FINISH_RATE_PRIOR,
    division_category_features,
    division_finish_rate_prior,
    normalize_division_keys,
)

logger = logging.getLogger(__name__)

# Convenção do octógono: canto A = red, canto B = blue.
_SUFFIX_A = "_a"
_SUFFIX_B = "_b"
_SUFFIX_DIFF = "_diff"

# Colunas-chave da frame longa consumidas por esta slice (contrato estável Slice 01).
COL_BOUT_ID = "bout_id"
COL_CORNER = "corner"
COL_RESULT = "result"
COL_WEIGHT_CLASS = "weight_class"
COL_TITLE_BOUT = "title_bout"
# Formato AGENDADO da luta. O nome da coluna crua é HOMÔNIMO do da feature emitida abaixo
# (``SCHEDULED_ROUNDS``) de propósito -- ver o comentário da allowlist bout-level.
COL_SCHEDULED_ROUNDS = "scheduled_rounds"

# Coluna-alvo, separada das features (domínio "R" | "B"; NA em NC/draw -> excluído).
TARGET_COLUMN = "winner_corner"
_CORNER_RED = "R"
_CORNER_BLUE = "B"

# Valores da coluna ``result`` (ver ``ingestion.features.long_frame.BoutResult``).
_RESULT_WIN = "win"
_RESULT_LOSS = "loss"

# ``result_a``/``result_b`` após o pivô (o alvo é lido do canto vermelho, A).
_COL_RESULT_A = f"{COL_RESULT}{_SUFFIX_A}"
# Data e método após o pivô. Os dois cantos carregam o mesmo valor por construção (vêm da
# mesma linha de ``bouts``); ler o canto A é convenção, não escolha entre valores.
_COL_EVENT_DATE_A = f"event_date{_SUFFIX_A}"
_COL_METHOD_A = f"method{_SUFFIX_A}"

# Bases que NÃO são features preditivas as-of e por isso ficam fora dos diferenciais
# e de ``feature_columns``. Fonte única do conhecimento sobre o que é ruído/leakage.
_IDENTITY_BASES: frozenset[str] = frozenset({COL_BOUT_ID, "fighter_id", "fighter_name"})
_EVENT_CONTEXT_BASES: frozenset[str] = frozenset({"event_id", "event_name", "event_date", "source"})
# Desfecho da própria luta (não é predição as-of -- leakage se usado como feature).
# Inclui os splits de golpe wide do M5 (Sprint 02): são box-score da luta corrente, não
# predição. Só as derivadas as-of (``share_*_r3``, ``round1_*_r3``) entram como feature.
_OUTCOME_BASES: frozenset[str] = frozenset(
    {
        COL_RESULT,
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
        "total_strikes_landed",
        "total_strikes_attempted",
        "head_landed",
        "head_attempted",
        "body_landed",
        "body_attempted",
        "leg_landed",
        "leg_attempted",
        "distance_landed",
        "distance_attempted",
        "clinch_landed",
        "clinch_attempted",
        "ground_landed",
        "ground_attempted",
        "reversals",
    }
)
# Contexto da luta conhecido ANTES do gongo (RF-02 da SPEC 009). Também sai das features
# **cruas** -- o modelo lê as derivadas de coluna única (``weight_class_lbs``,
# ``division_finish_rate_prior``...), não o rótulo bruto --, mas jamais pode ser colapsado
# com ``_OUTCOME_BASES``: ``scheduled_rounds`` é o formato agendado, ``round`` é onde a luta
# acabou. Confundir os dois transformaria contexto legítimo em vazamento (ou o contrário,
# excluindo do treino um preditor válido -- o modo de falha que custou 21 features no M5).
# As duas entram em ``_NON_FEATURE_BASES`` pelo mesmo caminho; a separação é semântica e é
# o que sustenta os blocos B1 (divisão) e B2 (formato).
_BOUT_CONTEXT_BASES: frozenset[str] = frozenset(
    {COL_WEIGHT_CLASS, COL_TITLE_BOUT, COL_SCHEDULED_ROUNDS}
)
_NON_FEATURE_BASES: frozenset[str] = (
    _IDENTITY_BASES | _EVENT_CONTEXT_BASES | _OUTCOME_BASES | _BOUT_CONTEXT_BASES
)

# Features bout-level de formato (SPEC 009, bloco B2 -- Slice 03), emitidas como coluna
# ÚNICA. O conhecimento de domínio aqui é uma tradução de tipo (booleano -> 0/1, inteiro
# nullable -> float), não um mapa como o das divisões: fica neste módulo, sem justificar um
# ``format.py`` de duas linhas (YAGNI).
IS_TITLE_BOUT = "is_title_bout"
# Nome deliberadamente IGUAL ao da coluna crua ``scheduled_rounds`` (batismo da SPEC 009):
# 3 ou 5 rounds é o mesmo número nas duas leituras, e inventar um alias só para fugir da
# colisão criaria dois nomes para o mesmo fato. A coluna crua vive como par
# ``scheduled_rounds_a``/``_b`` até o colapso e está em ``_BOUT_CONTEXT_BASES``; a feature é
# a coluna única que o colapso produz, convertida para float. Quem separa as duas é a
# precedência da allowlist em ``_feature_columns`` -- sem ela, ``_base_of`` casaria com a
# base excluída e a feature sumiria do payload SEM ERRO NENHUM.
SCHEDULED_ROUNDS = "scheduled_rounds"
FORMAT_FEATURES: list[str] = [IS_TITLE_BOUT, SCHEDULED_ROUNDS]

# Colunas da frame longa **enriquecida** consumidas pelos blocos D1 e D2 (SPEC 009, Slice
# 05). Diferentes das ``COL_*`` acima: não são cruas de ``read_granular``, nascem depois --
# ``stance`` em ``trajectory.add_physical_attributes`` e os eixos de estilo em
# ``rolling.add_recent_form_features``. Os nomes são declarados aqui, e não importados dos
# módulos de origem, pelo mesmo motivo de ``trajectory.COL_ROUND_NUMBER``: evitar acoplar os
# módulos de feature entre si. A duplicação deliberada só é segura com o teste-guarda de
# igualdade (``tests/features/test_eixos_de_estilo.py``) -- sem ele, um rename silencioso em
# ``rolling``/``trajectory`` esvaziaria estas features sem erro nenhum.
COL_STANCE = "stance"
COL_GRAPPLING_AXIS = "grappling_axis_r3"
COL_VOLUME_AXIS = "volume_axis_r3"

# Bases conhecidas: exatamente os três valores do enum ``Stance``. Rótulos da fonte oficial
# fora dele (``Open Stance``, ``Sideways``, medidos no M7) **zeram** a base do lutador no
# banco, então aqui só existem estes três ou ausência -- e ausência vira nulo, não categoria.
_KNOWN_STANCES: frozenset[str] = frozenset(base.value for base in Stance)

# Features bout-level do confronto de bases (bloco D1). Duas binárias e não um ordinal de
# três categorias: um encoding ordinal imporia uma ordem falsa entre "mesma base", "bases
# opostas" e "alternante", e nenhuma das três é maior que a outra.
IS_OPEN_STANCE_MATCHUP = "is_open_stance_matchup"
INVOLVES_SWITCH_STANCE = "involves_switch_stance"
STANCE_MATCHUP_FEATURES: list[str] = [IS_OPEN_STANCE_MATCHUP, INVOLVES_SWITCH_STANCE]

# Feature bout-level dos eixos de estilo (bloco D2): a distância entre os vetores de estilo
# as-of dos dois cantos. Bout-level por natureza -- é uma relação entre os cantos, não um
# atributo de nenhum deles.
STYLE_DISTANCE = "style_distance"
STYLE_MATCHUP_FEATURES: list[str] = [STYLE_DISTANCE]

# Allowlist das features bout-level: colunas de feature que valem para a luta inteira e por
# isso entram SEM sufixo de canto. Consultada por **nome completo** (nunca por base) antes da
# exclusão por base, o que é o único jeito de uma feature homônima de uma base excluída
# sobreviver. Fonte única também do log demonstrável do estágio ``matchup``
# (``ingestion.features.cli``): duas listas do mesmo fato divergiriam na próxima slice.
BOUT_LEVEL_FEATURE_COLUMNS: tuple[str, ...] = (
    *DIVISION_CATEGORY_FEATURES,
    *DIVISION_BASE_RATE_FEATURES,
    *FORMAT_FEATURES,
    *STANCE_MATCHUP_FEATURES,
    *STYLE_MATCHUP_FEATURES,
)
_BOUT_LEVEL_FEATURE_SET: frozenset[str] = frozenset(BOUT_LEVEL_FEATURE_COLUMNS)


@dataclass(frozen=True)
class MatchupMatrix:
    """Contrato de saída da matriz de confronto (consumido pelo CLI e pela Slice 05).

    ``frame`` tem uma linha por bout decidido: ``*_a`` / ``*_b`` / ``*_diff`` mais a
    coluna-alvo ``winner_corner``. ``feature_columns`` lista as colunas de feature
    (``*_a`` / ``*_b`` / ``*_diff``) e **não** inclui o alvo. ``excluded_no_result`` é
    o número de lutas NC/draw removidas. ``red_corner_win_rate`` é o baseline ingênuo.
    """

    frame: pd.DataFrame
    feature_columns: list[str]
    target_column: str
    excluded_no_result: int
    red_corner_win_rate: float


def pivot_corners(long: pd.DataFrame) -> pd.DataFrame:
    """Uma linha por lutador-luta -> uma linha por bout, canto A = red / B = blue.

    Separa a frame por canto, descarta a coluna ``corner`` e junta os dois cantos por
    ``bout_id`` com sufixos ``(_a, _b)`` e ``validate="one_to_one"``: uma luta
    bem-formada tem exatamente um red e um blue; um bout com canto duplicado levanta
    ``MergeError`` (dado corrompido falha visível, alinhado ao risco de série
    fragmentada da SPEC).
    """
    red = long[long[COL_CORNER] == Corner.RED].drop(columns=[COL_CORNER])
    blue = long[long[COL_CORNER] == Corner.BLUE].drop(columns=[COL_CORNER])
    return red.merge(blue, on=COL_BOUT_ID, suffixes=(_SUFFIX_A, _SUFFIX_B), validate="one_to_one")


def collapse_bout_context_columns(matrix: pd.DataFrame) -> pd.DataFrame:
    """Colapsa o par degenerado ``*_a``/``*_b`` do contexto de luta em coluna única.

    O contexto vem da MESMA linha de ``bouts`` para os dois cantos: o pivô o duplica em
    ``weight_class_a``/``weight_class_b`` idênticos, cujo ``_diff`` seria zero em toda linha
    (ruído puro) e cuja versão string seria descartada em silêncio por
    ``analysis.dataset._numeric_feature_columns``. Colapsar aqui é o que permite às features
    bout-level derivadas nascerem como coluna única (CA-14 da SPEC 009).

    Sem guarda silenciosa de coluna ausente: ``LONG_FRAME_COLUMNS`` é contrato, e um par
    faltando é violação dele -- falha visível com ``KeyError``, mesma disciplina do
    ``validate="one_to_one"`` do pivô. A frame de entrada não é mutada.
    """
    matrix = matrix.copy()
    for base in sorted(_BOUT_CONTEXT_BASES):
        column_a = f"{base}{_SUFFIX_A}"
        column_b = f"{base}{_SUFFIX_B}"
        if column_b not in matrix.columns:
            raise KeyError(column_b)
        matrix[base] = matrix[column_a]
        matrix = matrix.drop(columns=[column_a, column_b])
    return matrix


def numeric_feature_bases(matrix: pd.DataFrame) -> list[str]:
    """Bases com par ``*_a``/``*_b`` numérico que são features (fora de identidade/desfecho).

    Percorre as colunas ``*_a``, confirma a existência do par ``*_b``, exige dtype
    numérico nas duas e exclui as bases conhecidas de identidade/contexto/desfecho. A
    ordem de descoberta (ordem das colunas) é preservada -- determinística.
    """
    bases: list[str] = []
    for column in matrix.columns:
        if not column.endswith(_SUFFIX_A):
            continue
        base = column[: -len(_SUFFIX_A)]
        if base in _NON_FEATURE_BASES:
            continue
        column_b = f"{base}{_SUFFIX_B}"
        if column_b not in matrix.columns:
            continue
        if is_numeric_dtype(matrix[column]) and is_numeric_dtype(matrix[column_b]):
            bases.append(base)
    return bases


def add_differentials(matrix: pd.DataFrame, bases: list[str]) -> pd.DataFrame:
    """Adiciona ``<base>_diff = <base>_a - <base>_b`` para cada base numérica.

    Subtração vetorizada coluna a coluna; tipos nullable (``Int64``) propagam ``NA``
    numa estreia (feature as-of ausente). A frame de entrada não é mutada.
    """
    matrix = matrix.copy()
    for base in bases:
        matrix[f"{base}{_SUFFIX_DIFF}"] = (
            matrix[f"{base}{_SUFFIX_A}"] - matrix[f"{base}{_SUFFIX_B}"]
        )
    return matrix


def add_division_features(matrix: pd.DataFrame) -> pd.DataFrame:
    """Features bout-level de divisão (B1a + B1b) como colunas ÚNICAS, sem sufixo de canto.

    Normaliza o rótulo cru uma vez e alimenta com a chave canônica tanto a categoria física
    quanto a taxa base point-in-time -- as duas falam a mesma chave, o que impede que
    ``Lightweight`` e ``lightweight`` virem divisões diferentes em um lado e iguais no outro.

    A data e o método vêm do canto A: os dois cantos carregam o mesmo valor por construção do
    pivô. A frame de entrada não é mutada.
    """
    keys = normalize_division_keys(matrix[COL_WEIGHT_CLASS])
    features = division_category_features(keys)
    features[DIVISION_FINISH_RATE_PRIOR] = division_finish_rate_prior(
        keys, matrix[_COL_EVENT_DATE_A], matrix[_COL_METHOD_A]
    )
    return matrix.assign(**features)


def add_format_features(matrix: pd.DataFrame) -> pd.DataFrame:
    """Features bout-level de formato (B2) como colunas ÚNICAS, sem sufixo de canto.

    Lê o contexto **já colapsado** (``collapse_bout_context_columns`` roda antes): o pivô
    duplica o formato nos dois cantos porque ele vem da mesma linha de ``bouts``, e o colapso
    já resolveu o par degenerado.

    ``title_bout`` é booleano **nullable** e chega como ``object``/``boolean`` com ``None``;
    ``.map`` é o que devolve ``NaN`` para a ausência sem ramificação -- ``astype(float)``
    levantaria ou produziria ``0.0`` para o nulo, imputando "não vale cinturão" sobre uma luta
    cujo formato ninguém registrou (RF-03).

    ``scheduled_rounds`` é convertida **no lugar**: a feature é o mesmo número da coluna crua,
    só que em ``float64`` (o dtype que atravessa ``materialize._to_json_value`` mapeando
    ausência para ``null`` no JSONB). ``errors="raise"`` faz um valor inesperado falhar
    visível em vez de virar ``NaN`` em silêncio e esvaziar a feature.

    Contexto conhecido **antes do gongo** (RF-02), não desfecho: ``scheduled_rounds`` é o
    formato agendado, ``round`` é onde a luta acabou. A frame de entrada não é mutada.
    """
    title = matrix[COL_TITLE_BOUT].map({True: 1.0, False: 0.0}).astype("float64")
    rounds = pd.to_numeric(matrix[COL_SCHEDULED_ROUNDS], errors="raise").astype("float64")
    return matrix.assign(**{IS_TITLE_BOUT: title, SCHEDULED_ROUNDS: rounds})


def _binary_where_known(flag: pd.Series, known: pd.Series) -> pd.Series:
    """Indicador booleano -> ``float64``, **nulo** onde a base de algum canto é desconhecida.

    O ``fillna(False)`` antes da conversão não imputa nada: a comparação de uma coluna de
    dtype ``string`` (nullable) devolve ``boolean`` com ``pd.NA``, que ``astype("float64")``
    recusa converter, e toda linha assim marcada é justamente uma linha que o ``where(known)``
    seguinte vai anular. Sem os dois passos, a alternativa seria ``0.0`` -- afirmar "não" sobre
    o que ninguém registrou, que a RF-03 proíbe.
    """
    return flag.fillna(False).astype("float64").where(known)


def add_stance_matchup_features(matrix: pd.DataFrame) -> pd.DataFrame:
    """Features bout-level do confronto de bases (D1) como colunas ÚNICAS, sem sufixo.

    ``is_open_stance_matchup`` marca o clássico destro contra canhoto, nos dois sentidos (a
    feature é da luta, não do canto); ``involves_switch_stance`` marca a presença de um
    alternante em qualquer canto. Mesma base nos dois cantos zera as duas -- é a terceira
    categoria, representada sem coluna própria e sem a ordem falsa que um encoding ordinal de
    três categorias imporia.

    Base **desconhecida ou ausente em qualquer canto** anula as duas (RF-03). Só os três
    valores do enum ``Stance`` contam como conhecidos: um rótulo fora dele é ignorância sobre
    a base, não uma quarta categoria.

    Requer a frame já pivotada e o par ``stance_a``/``stance_b``, que existe porque a frame
    longa passou por ``trajectory.add_physical_attributes``. A frame de entrada não é mutada.
    """
    stance_a = matrix[f"{COL_STANCE}{_SUFFIX_A}"]
    stance_b = matrix[f"{COL_STANCE}{_SUFFIX_B}"]
    known = stance_a.isin(_KNOWN_STANCES) & stance_b.isin(_KNOWN_STANCES)
    open_stance = ((stance_a == Stance.ORTHODOX) & (stance_b == Stance.SOUTHPAW)) | (
        (stance_a == Stance.SOUTHPAW) & (stance_b == Stance.ORTHODOX)
    )
    switch = (stance_a == Stance.SWITCH) | (stance_b == Stance.SWITCH)
    return matrix.assign(
        **{
            IS_OPEN_STANCE_MATCHUP: _binary_where_known(open_stance, known),
            INVOLVES_SWITCH_STANCE: _binary_where_known(switch, known),
        }
    )


def add_style_distance_feature(matrix: pd.DataFrame) -> pd.DataFrame:
    """``style_distance`` bout-level: distância euclidiana entre os vetores de estilo as-of.

    Os dois eixos de ``rolling`` vivem no quadrado unitário, então a distância fica em
    ``[0, √2]`` -- intervalo fechado que é o que sustenta a escolha de ``STYLE_KERNEL_SIGMA``
    no bloco D3 (Slice 06). Qualquer eixo nulo em qualquer canto propaga nulo pela própria
    aritmética (RF-03): não há média parcial nem distância sobre vetor incompleto, que não
    seria comparável com a das demais lutas.

    Calculada direto dos pares ``*_a``/``*_b``, **nunca** das colunas ``*_diff``: a existência
    e a ordem de criação dos diferenciais em ``build_matchup_matrix`` não são contrato deste
    cálculo, e depender delas amarraria a feature à ordem do encadeamento. A frame de entrada
    não é mutada.
    """
    delta_grappling = (
        matrix[f"{COL_GRAPPLING_AXIS}{_SUFFIX_A}"] - matrix[f"{COL_GRAPPLING_AXIS}{_SUFFIX_B}"]
    )
    delta_volume = matrix[f"{COL_VOLUME_AXIS}{_SUFFIX_A}"] - matrix[f"{COL_VOLUME_AXIS}{_SUFFIX_B}"]
    distancia = (delta_grappling**2 + delta_volume**2) ** 0.5
    return matrix.assign(**{STYLE_DISTANCE: distancia.astype("float64")})


def derive_target(matrix: pd.DataFrame) -> pd.DataFrame:
    """Adiciona ``winner_corner`` (R/B) a partir do resultado do canto vermelho (A).

    ``result_a == "win"`` -> ``R``; ``result_a == "loss"`` -> ``B``; qualquer outro
    valor (no-contest/draw) -> ``NA`` (excluído a jusante). A frame de entrada não é
    mutada.
    """
    result_a = matrix[_COL_RESULT_A]
    corner = pd.Series(pd.NA, index=matrix.index, dtype="string")
    corner[result_a == _RESULT_WIN] = _CORNER_RED
    corner[result_a == _RESULT_LOSS] = _CORNER_BLUE
    return matrix.assign(**{TARGET_COLUMN: corner})


def red_corner_win_rate(matrix: pd.DataFrame) -> float:
    """Baseline ingênuo: fração de lutas em que o corner vermelho vence (``== "R"``).

    Calculado sobre a matriz **decidida** (pós-exclusão de NC/draw) -- NC/draw nunca
    entram no denominador. É estatística descritiva: usa só o alvo, jamais as features.
    """
    return float((matrix[TARGET_COLUMN] == _CORNER_RED).mean())


def _feature_columns(matrix: pd.DataFrame) -> list[str]:
    """Colunas de feature: as bout-level da allowlist + as as-of de base não excluída.

    A allowlist é consultada por **nome completo e antes** da exclusão por base, e a ordem
    é load-bearing: ``_base_of("scheduled_rounds") == "scheduled_rounds"``, que está em
    ``_BOUT_CONTEXT_BASES`` e portanto em ``_NON_FEATURE_BASES``. Filtrando primeiro por base,
    a feature bout-level homônima seria descartada **sem erro nenhum** -- exatamente o modo de
    falha silencioso que escondeu 21 features do M5 por meses.
    """
    return [
        column
        for column in matrix.columns
        if column != TARGET_COLUMN
        and (column in _BOUT_LEVEL_FEATURE_SET or _base_of(column) not in _NON_FEATURE_BASES)
    ]


def _base_of(column: str) -> str:
    """Remove o sufixo de canto/diferencial (``_a``/``_b``/``_diff``) para obter a base."""
    for suffix in (_SUFFIX_DIFF, _SUFFIX_A, _SUFFIX_B):
        if column.endswith(suffix):
            return column[: -len(suffix)]
    return column


def build_matchup_matrix(long: pd.DataFrame) -> MatchupMatrix:
    """Encadeia pivô -> colapso -> divisão -> formato -> bases -> estilo -> diff -> alvo.

    A entrada é a frame longa **enriquecida** (rolling + trajetória), não a crua de
    ``build_long_frame``: além das colunas de ``LONG_FRAME_COLUMNS``, esta função consome
    ``stance`` (de ``trajectory.add_physical_attributes``) e os eixos de estilo (de
    ``rolling.add_recent_form_features``). Faltando qualquer uma, falha alto com ``KeyError``
    -- mesma disciplina do ``validate="one_to_one"`` do pivô: um contrato quebrado não vira
    feature vazia em silêncio.

    A ordem é load-bearing, não estética:

    - o **colapso vem antes dos diferenciais**, senão o par degenerado do contexto de luta
      sobreviveria e voltaria a gerar ``*_diff`` identicamente zero (CA-14);
    - as **features de divisão vêm antes da exclusão de NC/draw**, porque uma luta sem
      resultado aconteceu naquela divisão e conta no denominador da taxa base como
      não-finalização -- excluí-la antes mudaria o denominador em silêncio;
    - as **bout-level de base e estilo vêm antes dos diferenciais** apenas para ficarem no
      mesmo lugar das demais bout-level; elas leem pares ``*_a``/``*_b``, nunca ``*_diff``.

    Excluir NC/draw acontece **antes** do baseline (não poluem o denominador). O
    resultado é o dataclass ``MatchupMatrix`` consumido pelo CLI e (na Slice 05) pela
    materialização.
    """
    matrix = pivot_corners(long)
    matrix = collapse_bout_context_columns(matrix)
    matrix = add_division_features(matrix)
    matrix = add_format_features(matrix)
    matrix = add_stance_matchup_features(matrix)
    matrix = add_style_distance_feature(matrix)
    matrix = add_differentials(matrix, numeric_feature_bases(matrix))
    matrix = derive_target(matrix)

    total = len(matrix)
    decided = matrix[matrix[TARGET_COLUMN].notna()].reset_index(drop=True)
    excluded = total - len(decided)

    return MatchupMatrix(
        frame=decided,
        feature_columns=_feature_columns(decided),
        target_column=TARGET_COLUMN,
        excluded_no_result=excluded,
        red_corner_win_rate=red_corner_win_rate(decided),
    )
