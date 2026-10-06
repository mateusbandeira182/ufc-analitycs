"""Serving do modelo preditivo: probabilidade de vitória em confrontos hipotéticos A vs B.

Peça de servir da fase 2. ``predict_card`` recebe N pares de ``fighter_id`` e devolve a
probabilidade de cada canto vencer confrontos **hipotéticos** "as-of agora" (não lutas
persistidas); ``predict_matchup`` é o caso de um par só, delegando ao lote.

**O caminho de produção lê o cache as-of** (SPEC 011, Slice 01). Até o M8, cada chamada
reconstruía a pipeline point-in-time inteira sobre todo o granular -- e a neutralização de
canto a chamava duas vezes, o que custava os ~67s que a RF-04 existe para eliminar. Agora
``predict_card`` faz um ``SELECT`` em ``fighter_asof_features`` (duas linhas por par),
monta a matriz de confronto do payload, deriva a feature do PAR do ``style_history`` e alinha
a ``feature_names``. A neutralização de canto **permanece** onde está (camada de API): com o
cache ela custa microssegundos, e é decisão de correção, não de desempenho.

O cache é construído por ``ingestion.features.asof``, que injeta uma luta sintética **por
lutador** e roda a mesma cadeia as-of. ``_asof_matchup_rows`` -- a reconstrução ao vivo, que
injeta uma sintética **por par** -- continua no módulo como **implementação de referência**:
é contra ela que a paridade da RF-03 é medida em ``tests/features/test_asof_paridade.py``.
Não é código morto; é a fonte da verdade do teste que impede o cache de divergir em silêncio.

**Um lutador por lote.** A restrição de um único par por lutador permanece: ela é espelhada em
``apps.predictions.services._repeated_fighter_ids`` e sustenta o contrato do card. Com o cache
ela deixou de ser tecnicamente necessária no caminho quente, mas continua sendo o contrato --
e ``_asof_matchup_rows``, que ainda depende do ``shift(1)``, continua precisando dela.

Convenção fixa A = red, B = blue (ADR 0001): o modelo prevê ``P(winner_corner == red)``, que
aqui é ``P(A vence)``. O modelo cru é sensível ao canto (aprendeu a vantagem do vermelho); a
neutralização vive na camada de API, que chama o serving nas duas ordens de canto. Anti-leakage
/``safe_ratio`` (denominador zero -> ``NaN``, nunca ``inf``) e a degradação para ``NaN`` de
features não-backfilladas (round-a-round) são herdados da pipeline; o
``HistGradientBoostingClassifier`` trata ``NaN`` nativamente. Nada é escrito no banco (leitura
pura sobre o granular).

**Degradação declarada do serving com as features da SPEC 009 (M8), CA-16.** A cadeia as-of --
tanto a de referência (``_asof_matchup_rows``) quanto a materializada (``asof.enrich_asof``) --
chama ``pivot_corners``/``add_differentials`` direto, sem o orquestrador
``matchup.build_matchup_matrix``. Consequência, explícita e testada (e preservada de propósito
pela SPEC 011: materializar a mais mudaria em silêncio o vetor que o modelo come):

- as features **bout-level de contexto** (``weight_class_lbs``, ``is_womens_division``,
  ``division_finish_rate_prior``, ``is_title_bout``, ``scheduled_rounds``,
  ``is_open_stance_matchup``, ``involves_switch_stance``, ``style_distance``) e
  ``southpaw_opponents_faced_prior`` **não** são calculadas: viram ``NaN`` pelo ``reindex`` de
  ``_align_features``. Qual divisão, formato ou base tem um confronto **hipotético** é decisão
  de produto, não de engenharia de features -- um default aqui resolveria o sintoma e
  esconderia a pergunta. A propagação do contexto ao serving já está registrada como PRD
  próprio posterior;
- as features **as-of por lutador** continuam íntegras, inclusive os blocos D3 e D4 da Slice 06
  (``similar_style_win_rate_prior``, ``opponent_win_rate_prior_avg``), porque nascem dentro de
  ``rolling.add_recent_form_features``, que o serving chama. O D3 depende da **ordem posicional
  dentro do grupo do lutador** (o prefixo é "tudo antes desta linha") e as linhas sintéticas
  são concatenadas no fim da frame; ``add_recent_form_features`` reordena defensivamente pela
  chave canônica, e a sintética -- datada de hoje -- cai por último no grupo, de modo que o
  prefixo continua sendo o passado real do lutador.

O alinhamento a ``feature_names`` permanece correto em ambos os casos: é limitação aceita e
coberta por teste, não ponta solta.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from analysis.model import ARTIFACTS_DIR, LoadedModel, load_artifact
from apps.bouts.enums import BoutMethod, Corner
from apps.features.models import FighterAsOfFeatures
from apps.features.selectors import read_asof_features
from ingestion.features.long_frame import LONG_FRAME_COLUMNS
from ingestion.features.matchup import (
    COL_BOUT_ID,
    COL_CORNER,
    COL_RESULT,
    SUFFIX_A,
    SUFFIX_B,
    add_differentials,
    numeric_feature_bases,
    pivot_corners,
)
from ingestion.features.rolling import (
    COL_FIGHTER_ID,
    GRAPPLING_AXIS_R3,
    SIMILAR_STYLE_WIN_RATE_PRIOR,
    STYLE_HISTORY_COLUMNS,
    VOLUME_AXIS_R3,
    add_recent_form_features,
    add_round_dynamics_features,
    similar_style_prior_for_opponent,
)
from ingestion.features.trajectory import (
    COL_EVENT_DATE,
    add_trajectory_features,
    load_fighters_bio,
    load_round_stats,
)

logger = logging.getLogger(__name__)

# Sentinela da primeira luta hipotética: ids negativos nunca colidem com ids reais
# (seriais > 0). A i-ésima sintética do lote desce a partir daqui (-1, -2, -3...).
_HYPOTHETICAL_BOUT_ID = -1
_HYPOTHETICAL_EVENT_ID = -1

# Alvo binário do modelo: canto vermelho = 1 (ver ``analysis.dataset``). A = red, logo a
# probabilidade de A vencer é a da classe 1.
_RED_LABEL = 1


@dataclass(frozen=True)
class MatchupPrediction:
    """Resultado da predição de um confronto hipotético A vs B.

    ``prob_a_wins``/``prob_b_wins`` são complementares (somam 1); ``predicted_winner_id`` é o
    ``fighter_id`` do canto de maior probabilidade.
    """

    prob_a_wins: float
    prob_b_wins: float
    predicted_winner_id: int


@dataclass(frozen=True)
class CardMatchupPrediction:
    """Predição de um par do lote na ordem dada (A = red, B = blue).

    ``prob_a_wins`` é ``None`` quando o par não pôde ser predito; nesse caso
    ``missing_history_ids`` traz os lutadores sem histórico no granular que o impediram. É a
    diferença de contrato em relação a ``predict_matchup``: no lote, a ausência de histórico é
    **estado de um item** (uma luta impredizível não derruba o card), não erro da chamada.
    """

    fighter_a_id: int
    fighter_b_id: int
    prob_a_wins: float | None
    missing_history_ids: tuple[int, ...]


@dataclass(frozen=True)
class CardPrediction:
    """Saída de uma passada de predição em lote, com a identidade do modelo usado.

    ``model_version`` é o ``trained_at`` cru do artefato (chave do registro de predições) e
    ``n_features`` o tamanho do vetor consumido pelo modelo. ``matchups`` segue exatamente a
    ordem dos pares recebidos, inclusive os impredizíveis.
    """

    model_version: str
    n_features: int
    matchups: list[CardMatchupPrediction]


def _synthetic_bout_id(index: int) -> int:
    """Id sentinela da i-ésima luta sintética do lote: negativo, nunca colide com serial real."""
    return _HYPOTHETICAL_BOUT_ID - index


def _missing_history_message(fighter_id: int, label: str) -> str:
    """Mensagem única de "lutador sem histórico" (a que o router do matchup traduz para 422)."""
    return (
        f"Lutador {label} (id={fighter_id}) não tem histórico de lutas no granular; "
        f"sem lutas passadas não há features as-of para predizer."
    )


def _hypothetical_rows(
    fighter_a_id: int, fighter_b_id: int, bout_id: int, as_of: date
) -> list[dict[str, object]]:
    """Duas linhas long (A=red, B=blue) de uma luta sintética, datadas em ``as_of``.

    Só identidade/canto/data/resultado importam: o box-score da luta corrente é ``None`` (a
    luta é futura) e é descartado pelo ``shift(1)`` da pipeline -- as features da linha
    sintética vêm apenas das lutas passadas do lutador. ``result`` é ``no_contest`` (placeholder
    inofensivo: também excluído da própria linha pelo ``shift(1)``).
    """
    base: dict[str, object] = dict.fromkeys(LONG_FRAME_COLUMNS)
    base.update(
        {
            COL_BOUT_ID: bout_id,
            "event_id": _HYPOTHETICAL_EVENT_ID,
            "event_name": "hypothetical",
            COL_EVENT_DATE: as_of,
            COL_RESULT: "no_contest",
            "method": BoutMethod.NO_CONTEST.value,
            "source": "prediction",
        }
    )
    red = {**base, COL_FIGHTER_ID: fighter_a_id, "fighter_name": "A", COL_CORNER: Corner.RED}
    blue = {**base, COL_FIGHTER_ID: fighter_b_id, "fighter_name": "B", COL_CORNER: Corner.BLUE}
    return [red, blue]


def _require_one_bout_per_fighter(pairs: Sequence[tuple[int, int]]) -> None:
    """Recusa o lote em que um ``fighter_id`` participa de mais de uma luta sintética.

    Invariante do batching: as features são ``groupby(fighter_id).shift(1)``, então duas
    sintéticas do mesmo lutador na mesma passada fariam a segunda consumir a primeira (que tem
    box-score todo nulo), corrompendo as janelas. Falhar aqui é preferível a servir uma
    predição silenciosamente errada.
    """
    repetidos = sorted(
        fighter_id
        for fighter_id, ocorrencias in Counter(
            fighter_id for pair in pairs for fighter_id in pair
        ).items()
        if ocorrencias > 1
    )
    if repetidos:
        raise ValueError(
            f"Lutadores {repetidos} aparecem em mais de uma luta do lote; a predição em lote "
            f"exige uma única luta sintética por lutador."
        )


def _asof_matchup_rows(
    session: Session,
    long: pd.DataFrame,
    pairs: Sequence[tuple[int, int]],
    bout_ids: Sequence[int],
    as_of: date,
) -> pd.DataFrame:
    """Constrói as linhas bout-level (``*_a``/``*_b``/``*_diff``) dos pares, as-of agora.

    **Implementação de referência, não caminho de produção** (SPEC 011, Slice 01). Desde o M9 o
    serving lê ``fighter_asof_features`` via ``_cached_matchup_rows``; esta função permanece
    como a fonte da verdade contra a qual a paridade da RF-03 é medida
    (``tests/features/test_asof_paridade.py``). Remover-na cegaria o teste que impede o cache
    de divergir do que o modelo espera.

    Uma única execução da cadeia de features para os N pares: injeta uma luta sintética por par
    (identificada pelo ``bout_id`` sentinela correspondente) na frame longa já lida, roda a
    pipeline point-in-time (forma recente + trajetória + dinâmica por round) e pivota apenas as
    sintéticas -- o mesmo formato que ``matchup`` entrega ao treino, sem o alvo (as lutas são
    hipotéticas). A coluna ``bout_id`` sobrevive ao pivô e é o que liga cada linha ao seu par
    (nunca a ordem de saída do merge).
    """
    synthetic = pd.DataFrame(
        [
            row
            for (fighter_a_id, fighter_b_id), bout_id in zip(pairs, bout_ids, strict=True)
            for row in _hypothetical_rows(fighter_a_id, fighter_b_id, bout_id, as_of)
        ]
    )
    combined = pd.concat([long, synthetic], ignore_index=True)

    combined = add_recent_form_features(combined)
    fighters_bio = load_fighters_bio(session.connection())
    combined = add_trajectory_features(combined, fighters_bio)
    round_stats = load_round_stats(session.connection())
    combined = add_round_dynamics_features(combined, round_stats)

    only_synthetic = combined[combined[COL_BOUT_ID].isin(list(bout_ids))]
    pivoted = pivot_corners(only_synthetic)
    return add_differentials(pivoted, numeric_feature_bases(pivoted))


def _style_axis(row: FighterAsOfFeatures, column: str) -> float | None:
    """Eixo de estilo as-of do lutador, lido do payload cacheado (ausência vira ``None``)."""
    value = row.features.get(column)
    return None if value is None else float(value)


def _cached_matchup_rows(
    cached: dict[int, FighterAsOfFeatures],
    pairs: Sequence[tuple[int, int]],
    bout_ids: Sequence[int],
) -> pd.DataFrame:
    """Matriz de confronto (``*_a``/``*_b``/``*_diff``) montada de 2 linhas do cache por par.

    Caminho de produção desde a Slice 01 da SPEC 011: substitui a reconstrução da pipeline
    as-of por uma leitura. As bases vêm das **chaves do payload** -- a ordem delas não é
    contrato (o ``jsonb`` do Postgres não preserva ordem de inserção), e nem precisa ser:
    ``_align_features`` reindexa por ``feature_names``. Os diferenciais reusam
    ``add_differentials``/``numeric_feature_bases``, as mesmas funções do treino.

    ``similar_style_win_rate_prior`` é calculada **aqui**, e não lida do payload: é feature do
    PAR (D-15 da SPEC 011). O lado A usa o ``style_history`` de A contra os eixos as-of de B,
    e vice-versa -- a fórmula continua sendo a de ``rolling``, nunca reimplementada.
    """
    records: list[dict[str, object]] = []
    for (fighter_a_id, fighter_b_id), bout_id in zip(pairs, bout_ids, strict=True):
        record: dict[str, object] = {COL_BOUT_ID: bout_id}
        a, b = cached[fighter_a_id], cached[fighter_b_id]
        for suffix, lado, adversario in ((SUFFIX_A, a, b), (SUFFIX_B, b, a)):
            for base, value in lado.features.items():
                record[f"{base}{suffix}"] = value
            record[f"{SIMILAR_STYLE_WIN_RATE_PRIOR}{suffix}"] = similar_style_prior_for_opponent(
                pd.DataFrame(lado.style_history, columns=STYLE_HISTORY_COLUMNS),
                _style_axis(adversario, GRAPPLING_AXIS_R3),
                _style_axis(adversario, VOLUME_AXIS_R3),
            )
        records.append(record)

    frame = pd.DataFrame(records)
    # O payload atravessa o JSONB como ``object`` (valores nativos e ``None`` misturados);
    # converter aqui é o que faz ``numeric_feature_bases`` reconhecer as bases e emitir os
    # ``*_diff``. Sem isso a degradação seria silenciosa: diferencial nenhum, nenhum erro.
    feature_columns = [column for column in frame.columns if column != COL_BOUT_ID]
    frame[feature_columns] = frame[feature_columns].apply(pd.to_numeric)
    return add_differentials(frame, numeric_feature_bases(frame))


def _align_features(matchup_row: pd.DataFrame, feature_names: list[str]) -> pd.DataFrame:
    """Alinha as linhas de confronto às ``feature_names`` do modelo (mesma ordem, ``float64``).

    Espelha ``analysis.dataset.build_dataset``: seleciona exatamente as colunas do treino (as
    ausentes -- ex.: feature 100% ``NaN`` descartada no treino -- viram coluna ``NaN``),
    converte para numérico e ``float64``, preservando o ``NaN`` explícito (sem imputação). O
    ``HistGradientBoostingClassifier`` trata a ausência nativamente.
    """
    aligned = matchup_row.reindex(columns=feature_names)
    numeric: pd.DataFrame = aligned.apply(pd.to_numeric).astype("float64")
    return numeric


def _prob_a_wins_by_bout(loaded: LoadedModel, matchup_rows: pd.DataFrame) -> dict[int, float]:
    """Probabilidade de o canto vermelho vencer, indexada pelo ``bout_id`` sintético.

    A leitura é feita por ``bout_id`` -- e não pela posição da linha -- porque a ordem de saída
    do pivô/merge não é contrato.
    """
    features = _align_features(matchup_rows, loaded.feature_names)
    proba = loaded.model.predict_proba(features)
    red_index = list(loaded.model.classes_).index(_RED_LABEL)
    bout_ids = [int(bout_id) for bout_id in matchup_rows[COL_BOUT_ID]]
    return {bout_id: float(proba[row, red_index]) for row, bout_id in enumerate(bout_ids)}


def predict_card(
    session: Session,
    pairs: Sequence[tuple[int, int]],
    directory: Path = ARTIFACTS_DIR,
) -> CardPrediction:
    """Prediz N confrontos hipotéticos "as-of agora" numa única passada da pipeline.

    Lê ``fighter_asof_features`` uma vez (um ``SELECT`` pelos ids do lote) e monta a matriz de
    confronto do payload cacheado -- sem reconstruir a pipeline. As probabilidades são
    idênticas às de N chamadas individuais, porque as features de um lutador dependem só do
    histórico dele. Lutador **sem linha no cache** volta com ``prob_a_wins`` nulo e o id em
    ``missing_history_ids``, sem impedir a predição dos demais.

    Cache **nunca materializado** deixa todo par indisponível, o que é indistinguível de "todo
    mundo é estreante" só pela predição: quem separa as duas causas é o ``features_freshness``
    da mesma resposta (``n_bouts_source = 0``, ``stale = true``).

    Levanta ``ValueError`` se um ``fighter_id`` aparece em mais de um par (contrato do card,
    espelhado em ``apps.predictions.services``) e ``FileNotFoundError`` se não há artefato
    treinado.
    """
    loaded: LoadedModel = load_artifact(directory)
    _require_one_bout_per_fighter(pairs)
    n_features = len(loaded.feature_names)

    if not pairs:
        return CardPrediction(model_version=loaded.trained_at, n_features=n_features, matchups=[])

    cached = read_asof_features(session, [fighter_id for pair in pairs for fighter_id in pair])
    faltantes = [
        tuple(fighter_id for fighter_id in pair if fighter_id not in cached) for pair in pairs
    ]
    # Só os pares elegíveis são injetados; o bout_id sentinela de cada um é a posição dele na
    # injeção, e é por ele (não pela ordem de saída do pivô) que a probabilidade é lida depois.
    elegiveis = [indice for indice, ausentes in enumerate(faltantes) if not ausentes]
    bout_id_por_par = {
        indice: _synthetic_bout_id(posicao) for posicao, indice in enumerate(elegiveis)
    }

    prob_por_bout: dict[int, float] = {}
    if elegiveis:
        matchup_rows = _cached_matchup_rows(
            cached,
            [pairs[indice] for indice in elegiveis],
            [bout_id_por_par[indice] for indice in elegiveis],
        )
        prob_por_bout = _prob_a_wins_by_bout(loaded, matchup_rows)

    matchups = [
        CardMatchupPrediction(
            fighter_a_id=fighter_a_id,
            fighter_b_id=fighter_b_id,
            prob_a_wins=(
                prob_por_bout[bout_id_por_par[indice]] if indice in bout_id_por_par else None
            ),
            missing_history_ids=faltantes[indice],
        )
        for indice, (fighter_a_id, fighter_b_id) in enumerate(pairs)
    ]
    return CardPrediction(model_version=loaded.trained_at, n_features=n_features, matchups=matchups)


def predict_matchup(
    session: Session,
    fighter_a_id: int,
    fighter_b_id: int,
    directory: Path = ARTIFACTS_DIR,
) -> MatchupPrediction:
    """Prediz a probabilidade de A vencer um confronto hipotético A vs B "as-of agora".

    Caso de um par só de ``predict_card``: carrega o modelo persistido (``directory``),
    constrói o vetor de features do confronto reusando a engenharia point-in-time (estado
    1..N de cada lutador via luta sintética) e devolve as probabilidades complementares e o
    vencedor previsto. Convenção A = red: a probabilidade da classe 1 (canto vermelho) é a de
    A vencer. Levanta ``ValueError`` se um dos lutadores não tem histórico -- aqui a ausência
    é erro da chamada, não estado de um item --, e ``FileNotFoundError`` se não há artefato.
    """
    card = predict_card(session, [(fighter_a_id, fighter_b_id)], directory)
    only = card.matchups[0]
    # Ordem de checagem preservada (A antes de B): a mensagem identifica o primeiro lado sem
    # histórico, que é o que o router traduz para 422.
    if fighter_a_id in only.missing_history_ids:
        raise ValueError(_missing_history_message(fighter_a_id, "A"))
    if fighter_b_id in only.missing_history_ids:
        raise ValueError(_missing_history_message(fighter_b_id, "B"))

    prob_a_wins = only.prob_a_wins
    if prob_a_wins is None:  # pragma: no cover - defesa de tipo; sem faltante há probabilidade
        raise ValueError(f"Confronto {fighter_a_id} vs {fighter_b_id} não pôde ser predito.")
    prob_b_wins = float(1.0 - prob_a_wins)
    predicted_winner_id = fighter_a_id if prob_a_wins >= prob_b_wins else fighter_b_id
    return MatchupPrediction(
        prob_a_wins=prob_a_wins,
        prob_b_wins=prob_b_wins,
        predicted_winner_id=predicted_winner_id,
    )
