"""Cache as-of por lutador: materialização idempotente de ``fighter_asof_features``.

Núcleo da Slice 01 da SPEC 011 (M9). Irmão de ``ingestion.features.materialize`` no grão
**lutador**: aquele persiste a matriz de confronto por luta, este persiste o vetor de features
"as-of agora" de cada lutador. O granular (``bouts``/``bout_fighters``) permanece a fonte de
verdade (ADR 0001) e nunca é tocado aqui -- a tabela pode ser dropada e reconstruída a
qualquer momento.

**Por que existe.** Hoje cada request de ``/predict/matchup`` reconstrói a pipeline as-of
inteira sobre as 8.837 lutas, duas vezes (as duas ordens de canto da neutralização): ~67s de
resposta. Com o cache, o serving faz um ``SELECT`` de duas linhas e monta a matriz em
microssegundos -- a neutralização de canto **permanece**, porque é decisão de correção, não de
desempenho (RF-04).

**Como a linha as-of é construída.** Injeta-se **uma luta sintética por lutador** (``bout_id``
sentinela negativo, sem adversário) na frame longa, datada em ``as_of``, e roda-se a mesma
cadeia de features point-in-time. Como toda feature usa ``shift(1)`` (ou o prefixo estrito, no
caso do D3), a linha sintética é calculada a partir de **todas** as lutas passadas do lutador
-- exatamente o estado "agora".

**A invariante que torna o cache por lutador correto**: nenhuma feature as-of de um lutador
depende da identidade do adversário da luta sintética -- **exceto** ``similar_style_win_rate_prior``
(o bloco D3), que é feature do PAR. Por isso ela não entra no payload ``features`` e sim é
derivada no confronto, a partir do ``style_history`` cacheado (D-15 da SPEC 011). A invariante
é fixada por teste em ``tests/features/test_asof_paridade.py``, nunca suposta.

**Deriva de calendário, declarada.** ``age_years`` e ``layoff_days`` são medidos em ``as_of``:
um request feito dias depois lê a idade e o layoff da data da materialização. A guarda desta
SPEC é por **contagem de lutas** (RF-05), não por calendário -- não inventar uma segunda. A
visibilidade vem de ``materialized_at``, que a API expõe em ``features_freshness``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.features.models import FeaturePayload, FighterAsOfFeatures, StyleHistory
from ingestion.features.long_frame import LONG_FRAME_COLUMNS, build_long_frame, read_granular
from ingestion.features.matchup import COL_BOUT_ID, COL_CORNER, COL_RESULT, asof_feature_columns
from ingestion.features.materialize import SOURCE, to_json_value
from ingestion.features.rolling import (
    COL_FIGHTER_ID,
    SIMILAR_STYLE_WIN_RATE_PRIOR,
    STYLE_HISTORY_COLUMNS,
    add_recent_form_features,
    add_round_dynamics_features,
    opponent_asof_style_history,
)
from ingestion.features.trajectory import (
    COL_EVENT_DATE,
    add_trajectory_features,
    load_fighters_bio,
    load_round_stats,
)

logger = logging.getLogger(__name__)

# Comando que reconstrói ``fighter_asof_features`` (grão LUTADOR). Irmão de
# ``materialize.MATERIALIZE_COMMAND``, e deliberadamente distinto dele: ``"--stage materialize"``
# é PREFIXO de ``"--stage materialize-asof"``, então mandar o usuário rodar o estágio errado é
# uma falha que só um teste com asserção NEGATIVA acusa (CA-P1 do Plano 011-01).
MATERIALIZE_ASOF_COMMAND = "python -m ingestion.features build --stage materialize-asof"

# Sentinela da luta sintética as-of. Negativa como em ``analysis.predict``: ids reais são
# seriais positivos, então não há colisão possível. Também é o que mantém a sintética **antes**
# de uma luta real datada no mesmo dia, na ordem canônica -- a mesma convenção dos dois lados
# do teste de paridade.
_SYNTHETIC_BOUT_ID = -1
_SYNTHETIC_EVENT_ID = -1

# Features do PAR: dependem de quem é o adversário e por isso **jamais** entram no vetor por
# lutador (D-15). O insumo delas é ``style_history``, em coluna própria.
_PAIR_FEATURES: frozenset[str] = frozenset({SIMILAR_STYLE_WIN_RATE_PRIOR})


@dataclass(frozen=True)
class AsOfFighterRow:
    """Linha as-of de um lutador, pronta para o JSONB (payload já convertido na borda)."""

    fighter_id: int
    features: FeaturePayload
    style_history: StyleHistory


@dataclass(frozen=True)
class AsOfMaterialization:
    """Saída da construção as-of: as linhas mais os metadados da guarda de defasagem.

    ``n_bouts_source`` é a contagem de lutas que a pipeline **consumiu** nesta execução --
    o denominador da guarda barata de defasagem (RF-05). Gravado por execução, e não por
    linha, o que o mantém correto mesmo se um lutador sair do granular.
    """

    as_of: date
    n_bouts_source: int
    rows: list[AsOfFighterRow]


def enrich_asof(session: Session, long: pd.DataFrame) -> pd.DataFrame:
    """Cadeia as-of comum: forma recente -> trajetória -> dinâmica por round.

    Exatamente a cadeia que ``analysis.predict._asof_matchup_rows`` aplica hoje -- **sem**
    ``add_stance_history_features`` e **sem** as features bout-level de
    ``matchup.build_matchup_matrix``, porque a degradação declarada do serving (CA-16 da SPEC
    009) está fora do escopo desta SPEC: propagar o contexto ao serving é PRD próprio
    posterior. Materializar a mais mudaria em silêncio o vetor que o modelo come, que é
    exatamente o defeito que esta SPEC existe para não repetir.

    Ponto único da cadeia: se ela existisse em dois lugares, a paridade da RF-03 seria
    coincidência em vez de estrutura.
    """
    frame = add_recent_form_features(long)
    frame = add_trajectory_features(frame, load_fighters_bio(session.connection()))
    return add_round_dynamics_features(frame, load_round_stats(session.connection()))


def asof_payload_columns(enriched: pd.DataFrame) -> list[str]:
    """Colunas que entram no payload ``features`` do cache as-of.

    As bases as-of de ``matchup.asof_feature_columns`` **menos** as features do PAR: o D3
    depende de quem é o adversário e não pode ser pré-calculado por lutador (D-15). Fonte
    única do conjunto, consumida tanto pela materialização quanto pelo teste de paridade --
    uma lista redigitada num dos dois lados tornaria a RF-03 vacuamente verdadeira.
    """
    return [column for column in asof_feature_columns(enriched) if column not in _PAIR_FEATURES]


def _synthetic_bout_id(index: int) -> int:
    """Id sentinela da i-ésima luta sintética as-of: negativo, nunca colide com serial real."""
    return _SYNTHETIC_BOUT_ID - index


def _fighters_with_past_bouts(long: pd.DataFrame, as_of: date) -> list[int]:
    """Lutadores com ao menos uma luta **anterior** a ``as_of``, em ordem determinística.

    Estar no granular não basta: uma luta já cadastrada mas ainda não realizada (o card que se
    quer prever) não produz feature as-of nenhuma -- todo o box-score dela é nulo e o
    ``shift(1)`` a descarta da própria linha. Sem esse recorte, um estreante escalado no card
    ganharia linha no cache e o serving devolveria um palpite fabricado sobre um vetor
    inteiramente ``NaN``.
    """
    past = long[pd.to_datetime(long[COL_EVENT_DATE]) < pd.Timestamp(as_of)]
    return sorted(int(fighter_id) for fighter_id in past[COL_FIGHTER_ID].unique())


def _synthetic_rows(fighter_ids: list[int], as_of: date) -> pd.DataFrame:
    """Uma luta sintética por lutador (sem adversário), datada em ``as_of``.

    Espelha ``analysis.predict._hypothetical_rows`` no que importa para as features: só
    identidade, canto, data e resultado são preenchidos; o box-score é ``None`` (a luta é
    futura) e o ``shift(1)`` o descarta da própria linha. **Sem adversário de propósito**: o
    pareamento de ``_opponent_stats`` não encontra par e devolve nulo, o que é o mesmo efeito
    que o ``shift(1)`` já produz -- e é o que mantém o vetor cacheado independente de quem
    seria o rival.
    """
    base: dict[str, object] = dict.fromkeys(LONG_FRAME_COLUMNS)
    base.update(
        {
            "event_id": _SYNTHETIC_EVENT_ID,
            "event_name": "as-of",
            COL_EVENT_DATE: as_of,
            COL_CORNER: Corner.RED,
            COL_RESULT: "no_contest",
            "method": BoutMethod.NO_CONTEST.value,
            "source": SOURCE,
        }
    )
    return pd.DataFrame(
        [
            {
                **base,
                COL_BOUT_ID: _synthetic_bout_id(index),
                COL_FIGHTER_ID: fighter_id,
                "fighter_name": "as-of",
            }
            for index, fighter_id in enumerate(fighter_ids)
        ]
    )


def _style_history_by_fighter(
    enriched: pd.DataFrame, synthetic_bout_ids: dict[int, int]
) -> dict[int, StyleHistory]:
    """Histórico de estilos enfrentados de cada lutador, até a luta sintética dele.

    O recorte é o **prefixo posicional** dentro do grupo do lutador -- exatamente o que
    ``rolling._similar_style_weighted_prior`` considera "tudo antes desta linha". Derivá-lo de
    outro jeito (por data, por exemplo) casaria por acaso hoje e divergiria no primeiro
    empate de calendário; a frame já está na ordem canônica
    ``(fighter_id, event_date, bout_id)``, imposta por ``add_recent_form_features``.
    """
    style = opponent_asof_style_history(enriched)
    bout_ids = enriched[COL_BOUT_ID].to_numpy()
    # O id sai do próprio array, e não da chave do ``groupby``: a chave é ``Hashable`` para o
    # mypy (o dtype da coluna é dinâmico), e converter o elemento do array mantém a tipagem
    # honesta sem ``cast``.
    fighter_column = enriched[COL_FIGHTER_ID].to_numpy()
    history: dict[int, StyleHistory] = {}
    for positions in enriched.groupby(COL_FIGHTER_ID, sort=False).indices.values():
        fighter_id = int(fighter_column[positions[0]])
        synthetic = synthetic_bout_ids.get(fighter_id)
        if synthetic is None:
            continue
        marcador = np.flatnonzero(bout_ids[positions] == synthetic)
        prefix = positions[: int(marcador[0])]
        history[fighter_id] = [
            {column: to_json_value(record[column]) for column in STYLE_HISTORY_COLUMNS}
            for record in style.iloc[prefix].to_dict(orient="records")
        ]
    return history


def build_asof_materialization(session: Session, as_of: date) -> AsOfMaterialization:
    """Monta a linha as-of de cada lutador com ao menos uma luta anterior a ``as_of``.

    Injeta **uma** luta sintética por lutador (sem adversário) na frame longa e roda
    ``enrich_asof`` numa única passada -- o custo é o de uma execução da pipeline, não o de
    uma por lutador. Lutador sem luta passada não entra: sem histórico não há feature as-of, e
    um vetor inteiramente nulo viraria palpite fabricado sobre um estreante.

    ``similar_style_win_rate_prior`` fica **fora** do payload e o insumo dela vai para
    ``style_history`` (D-15). Sobre a deriva de calendário de ``age_years``/``layoff_days``,
    ver o docstring do módulo: é limitação declarada, não defeito.
    """
    frames = read_granular(session)
    long = build_long_frame(frames)
    fighter_ids = _fighters_with_past_bouts(long, as_of)
    n_bouts_source = len(frames.bouts)
    if not fighter_ids:
        logger.info("Materialização as-of: nenhum lutador com luta anterior a %s.", as_of)
        return AsOfMaterialization(as_of=as_of, n_bouts_source=n_bouts_source, rows=[])

    synthetic_bout_ids = {
        fighter_id: _synthetic_bout_id(index) for index, fighter_id in enumerate(fighter_ids)
    }
    combined = pd.concat([long, _synthetic_rows(fighter_ids, as_of)], ignore_index=True)
    enriched = enrich_asof(session, combined)

    columns = asof_payload_columns(enriched)
    history = _style_history_by_fighter(enriched, synthetic_bout_ids)
    synthetic = enriched[enriched[COL_BOUT_ID].isin(list(synthetic_bout_ids.values()))]
    rows = [
        AsOfFighterRow(
            fighter_id=int(record[COL_FIGHTER_ID]),
            features={column: to_json_value(record[column]) for column in columns},
            style_history=history[int(record[COL_FIGHTER_ID])],
        )
        for record in synthetic.to_dict(orient="records")
    ]
    return AsOfMaterialization(as_of=as_of, n_bouts_source=n_bouts_source, rows=rows)


def materialize_asof_features(
    session: Session, materialization: AsOfMaterialization, source: str = SOURCE
) -> int:
    """Upsert idempotente por ``fighter_id`` (PK): ``INSERT ... ON CONFLICT DO UPDATE``.

    Mesmo padrão de ``materialize.materialize_features`` -- a PK é não-nula, então o upsert
    por conflito é limpo e determinístico. Reexecutar sobre o mesmo granular mantém contagem e
    conteúdo; só ``materialized_at`` é refrescado (RF-02). Escreve **apenas** em
    ``fighter_asof_features``. Não commita: a transação é do chamador. Devolve o número de
    linhas materializadas nesta execução.
    """
    if not materialization.rows:
        logger.info("Materialização as-of: nenhuma linha a persistir.")
        return 0

    materialized_at = datetime.now(UTC)
    records: list[dict[str, object]] = [
        {
            "fighter_id": row.fighter_id,
            "features": row.features,
            "style_history": row.style_history,
            "n_bouts_source": materialization.n_bouts_source,
            "source": source,
            "materialized_at": materialized_at,
        }
        for row in materialization.rows
    ]

    stmt = insert(FighterAsOfFeatures).values(records)
    stmt = stmt.on_conflict_do_update(
        index_elements=["fighter_id"],
        set_={
            "features": stmt.excluded.features,
            "style_history": stmt.excluded.style_history,
            "n_bouts_source": stmt.excluded.n_bouts_source,
            "source": stmt.excluded.source,
            "materialized_at": stmt.excluded.materialized_at,
        },
    )
    session.execute(stmt)
    session.flush()

    logger.info(
        "Materializadas %d linhas em fighter_asof_features (source=%s, %d lutas consideradas).",
        len(records),
        source,
        materialization.n_bouts_source,
    )
    return len(records)
