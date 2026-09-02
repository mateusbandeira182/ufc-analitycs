"""Carregamento do dataset preditivo a partir de ``bout_features`` e split temporal.

Núcleo de dados da fase 2 (modelo preditivo). Lê o cache reconstrutível ``bout_features``
(M4) juntando a data do evento (``bout_features`` -> ``bouts`` -> ``events``), expande o
payload JSONB ``features`` em colunas **numéricas** (X) e mapeia o alvo
``target_winner_corner`` para binário (vermelho=1, azul=0), descartando as lutas de alvo
nulo (NC/empate) e as anteriores a ``FIRST_RELIABLE_CORNER_DATE`` (2010-03-21), cujo canto o
Kaggle fabricou (alvo falso; ver ADR 0006 e a Emenda 1 da SPEC 009).

Invariante load-bearing (mesma disciplina anti-leakage do M4): o split é **temporal**,
nunca aleatório. Ordena por data de evento e reserva as lutas mais recentes como holdout
de teste -- nenhuma luta de teste pode ter data anterior a uma luta de treino. Sem isso, o
modelo veria o futuro no treino e as métricas seriam otimistas e inúteis em produção.

Fronteira dinâmica tipada: o DataFrame do Pandas é borda dinâmica (``pandas.*`` tem
``follow_imports=skip`` no ``pyproject.toml``); a leitura converte ``bout_id`` para ``int``
e o alvo para ``str``/``None`` na borda, e as colunas categóricas (``stance_*``) ficam fora
de X -- o classificador consome apenas numérico, com ``NaN`` explícito preservado (o
``HistGradientBoostingClassifier`` trata ausência nativamente, sem imputação especulativa).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.models import Bout
from apps.events.models import Event
from apps.features.models import BoutFeatures

logger = logging.getLogger(__name__)

COL_BOUT_ID = "bout_id"
COL_EVENT_DATE = "event_date"
COL_TARGET = "target_winner_corner"
COL_FEATURES = "features"

# Alvo binário: canto vermelho = 1 (o baseline ingênuo prevê sempre 1), azul = 0.
_CORNER_TO_LABEL: dict[str, int] = {"red": 1, "blue": 0}

# Data do primeiro evento cujo canto o Kaggle registra de verdade: 2010-03-21, UFC Live:
# Vera vs Jones. Antes dela o canto do dataset é **fabricado**: a taxa de vitória do vermelho
# é exatamente 100,0% em TODOS os anos de 1994 a 2009 (1.249 lutas decididas), e passa a
# oscilar entre 53,8% e 63,2% depois do corte. O corte é seco, não gradiente -- naquelas
# linhas a coluna de canto foi preenchida com a ordem do resultado, porque o ufcstats da época
# não registrava canto. Confirmação independente: a convenção "o primeiro nome do título do
# evento é o canto vermelho" acerta 99,4% (478/481) de 2010 em diante e só 64,3% (18/28)
# antes disso.
#
# O corte original da ADR 0006 era 2010-01-01 e ficou **80 dias frouxo**: medido no banco de
# produção em 2026-09-02, as 40 lutas de 2010-01-01 a 2010-03-20 -- UFC 108 (10), UFC Fight
# Night: Maynard vs Diaz (10), UFC 109 (11) e UFC 110 (9) -- têm 40 de 40 vitórias do vermelho
# (100%, e 100% em cada evento isoladamente), contra 113/210 (53,8%) no resto de 2010. Mesmo
# alvo fabricado, mesmo remédio. Ver ADR 0006, Emenda 1 (SPEC 009, Slice 00B).
#
# É constante fixa, não parâmetro: não é ajuste de tuning que se calibra, é limitação
# conhecida da fonte -- não vira argumento, variável de ambiente nem flag de linha de comando.
# Coincide em valor com ``ingestion.ufc_official.OFFICIAL_WINDOW_START`` por **medição
# convergente**: são constantes distintas, com propósitos, módulos e consumidores distintos,
# e continuam proibidas de serem unificadas (ver o comentário lá).
FIRST_RELIABLE_CORNER_DATE = date(2010, 3, 21)

# As 8 médias de carreira do ``fighter_details.csv`` são um **snapshot de 2025**: cada uma
# resume a carreira inteira do lutador, inclusive as lutas posteriores àquela que se quer
# prever. Usá-las como preditor é vazamento de futuro puro -- foi por isso que o dataset
# orientado a apostas foi preterido no ADR 0002, e a SPEC 007 (CA-11) transformou a
# proscrição em guarda executável.
#
# A guarda mora aqui, no único chokepoint por onde ``run_training`` e o walk-forward passam,
# e é um conjunto **explícito** de nomes (nu + as três variantes da convenção do M4), não um
# stripping heurístico de sufixo: uma feature legítima que por acaso comece com o mesmo
# prefixo não pode ser descartada por engano.
PROSCRIBED_FEATURE_BASES: frozenset[str] = frozenset(
    {"splm", "str_acc", "sapm", "str_def", "td_avg", "td_avg_acc", "td_def", "sub_avg"}
)
_PROSCRIBED_COLUMNS: frozenset[str] = frozenset(
    f"{base}{suffix}" for base in PROSCRIBED_FEATURE_BASES for suffix in ("", "_a", "_b", "_diff")
)


@dataclass(frozen=True)
class Dataset:
    """Dataset preditivo pronto para o split temporal.

    ``features`` (X) só contém colunas numéricas; ``target`` (y) é binário (0/1);
    ``event_date`` e ``bout_id`` acompanham cada linha (alinhados por índice) para
    ordenar o split temporal de forma determinística.
    """

    features: pd.DataFrame
    target: pd.Series
    event_date: pd.Series
    bout_id: pd.Series
    feature_names: list[str]


@dataclass(frozen=True)
class TemporalSplit:
    """Resultado do split temporal treino/teste, com a fronteira de datas explícita.

    ``boundary_date`` é a data máxima do treino; por construção, ``event_test.min()`` é
    maior ou igual a ela -- a prova de ausência de vazamento temporal.
    """

    x_train: pd.DataFrame
    x_test: pd.DataFrame
    y_train: pd.Series
    y_test: pd.Series
    event_train: pd.Series
    event_test: pd.Series
    boundary_date: date


def read_bout_features(session: Session) -> pd.DataFrame:
    """Lê ``bout_features`` juntando a data do evento; devolve a frame crua.

    Junta ``bout_features`` -> ``bouts`` -> ``events`` para obter a data por luta. Cada
    linha carrega ``bout_id`` (int), ``event_date`` (data de calendário), o alvo como
    ``str``/``None`` (``"red"``/``"blue"``) e ``features`` como ``dict`` -- a expansão em
    colunas fica para ``build_dataset``.
    """
    stmt = (
        select(
            BoutFeatures.bout_id.label(COL_BOUT_ID),
            Event.date.label(COL_EVENT_DATE),
            BoutFeatures.target_winner_corner.label(COL_TARGET),
            BoutFeatures.features.label(COL_FEATURES),
        )
        .join(Bout, Bout.id == BoutFeatures.bout_id)
        .join(Event, Event.id == Bout.event_id)
    )
    records: list[dict[str, object]] = [
        {
            COL_BOUT_ID: int(row.bout_id),
            COL_EVENT_DATE: row.event_date,
            COL_TARGET: None if row.target_winner_corner is None else str(row.target_winner_corner),
            COL_FEATURES: dict(row.features),
        }
        for row in session.execute(stmt).all()
    ]
    return pd.DataFrame.from_records(
        records,
        columns=[COL_BOUT_ID, COL_EVENT_DATE, COL_TARGET, COL_FEATURES],
    )


def _label_for(value: object) -> int:
    """Mapeia o alvo ``"red"``/``"blue"`` para 1/0; um valor inesperado falha visível."""
    label = _CORNER_TO_LABEL.get(str(value))
    if label is None:
        aceitos = ", ".join(sorted(_CORNER_TO_LABEL))
        raise ValueError(f"Alvo winner_corner inesperado: {value!r}; valores aceitos: {aceitos}")
    return label


def _numeric_feature_columns(expanded: pd.DataFrame) -> list[str]:
    """Colunas de feature numéricas: exclui as que carregam qualquer valor string.

    As categóricas do M4 (``stance_a``/``stance_b``) guardam strings (``"orthodox"``...)
    e ficam fora de X -- o classificador consome apenas numérico. Uma coluna numérica toda
    nula (100% ``NaN``) **não** é inofensiva: quebra o ``HistGradientBoostingClassifier``
    no ``fit``; ela é filtrada depois, em ``_drop_all_nan_feature_columns``.
    """
    numeric: list[str] = []
    for column in expanded.columns:
        has_string = bool(expanded[column].map(lambda value: isinstance(value, str)).any())
        if not has_string:
            numeric.append(str(column))
    return numeric


def all_nan_columns(features: pd.DataFrame) -> list[str]:
    """Nomes das colunas 100% ``NaN`` da frame dada, em ordem alfabética.

    Pública porque o loop walk-forward aplica a mesma regra **por passo**, sobre a fatia
    anterior ao corte daquele evento -- o mesmo motivo que fez ``restrict_to_trainable_columns``
    existir para o split, sem que haja um ``TemporalSplit`` no loop para reusá-la.
    """
    return sorted(str(column) for column in features.columns if bool(features[column].isna().all()))


def _drop_all_nan_feature_columns(features: pd.DataFrame) -> pd.DataFrame:
    """Descarta as colunas de feature 100% ``NaN`` antes do treino; loga quais saíram.

    Uma coluna inteiramente ``NaN`` não é inofensiva: o ``HistGradientBoostingClassifier``
    levanta ``ValueError: window shape cannot be larger than input array shape`` ao tentar
    o binning dela. Isso ocorre num estado de **backfill parcial legítimo** desenhado pela
    própria SPEC -- os splits (Sprint 02, 0 quota, sem gate) podem ser materializados antes
    do round-a-round (Sprint 05, atrás de gate humano); nesse intervalo, as colunas de
    dinâmica por round ficam 100% ``NaN``.

    Colunas **parcialmente** ``NaN`` são preservadas intactas (o classificador trata a
    ausência nativamente). Descartar só a coluna toda-nula é degradação legítima de features
    ainda-não-backfillados -- não um silenciador genérico de ``NaN``.

    Esta guarda olha o dataset **inteiro** e por isso não cobre o caso em que a coluna tem
    dado apenas depois do corte temporal: para esse, ver ``restrict_to_trainable_columns``.
    """
    all_nan = all_nan_columns(features)
    if not all_nan:
        return features
    logger.warning(
        "Descartando %d coluna(s) de feature 100%% NaN antes do treino (backfill parcial): %s",
        len(all_nan),
        ", ".join(all_nan),
    )
    return features.drop(columns=all_nan)


def _drop_proscribed_feature_columns(features: pd.DataFrame) -> pd.DataFrame:
    """Descarta as colunas de média de carreira proscritas; loga quais saíram.

    Guarda de vazamento, não degradação: se uma destas colunas chegou até aqui, alguém a
    materializou em ``bout_features`` e o descarte precisa ser **visível** no log -- a
    materialização é que está errada. Nenhum treino do projeto pode vê-las (SPEC 007 CA-11,
    ADR 0002); o silêncio total é o cenário normal, porque hoje nada as produz.
    """
    presentes = sorted(str(column) for column in features.columns if column in _PROSCRIBED_COLUMNS)
    if not presentes:
        return features
    logger.warning(
        "Descartando %d coluna(s) de média de carreira proscrita(s) do conjunto de features: "
        "%s. São snapshot de 2025 e embutem o futuro da carreira do lutador (ADR 0002, "
        "SPEC 007 CA-11); nenhum treino pode vê-las.",
        len(presentes),
        ", ".join(presentes),
    )
    return features.drop(columns=presentes)


def _drop_fabricated_corner_rows(raw: pd.DataFrame) -> pd.DataFrame:
    """Descarta as lutas anteriores a ``FIRST_RELIABLE_CORNER_DATE``; loga quantas saíram.

    O alvo do modelo é o **canto** (``target_winner_corner``), e nas lutas do Kaggle anteriores
    a 2010-03-21 o canto é o vencedor renomeado (ver o comentário da constante). Treinar com rótulo
    que sabemos ser falso é o mesmo defeito que barrou o ``cardSection`` default e o ``corner``
    da Cito -- a fonte ser a nossa não abre exceção.

    O filtro vive aqui, na camada de dataset, e não na materialização de features: as
    **features** daquelas lutas estão corretas (idade, alcance, cartel prévio não dependem de
    quem é o vermelho) e seguem alimentando as janelas móveis do histórico dos atletas. O que
    não presta é o alvo, e o alvo só existe aqui.

    O descarte nunca é silencioso: quem roda o treino vê no log quantas lutas saíram e por quê,
    a mesma disciplina de ``_drop_all_nan_feature_columns``.
    """
    fabricated = raw[COL_EVENT_DATE] < FIRST_RELIABLE_CORNER_DATE
    n_fabricated = int(fabricated.sum())
    if n_fabricated == 0:
        return raw
    logger.warning(
        "Descartando %d luta(s) anteriores a %s do dataset preditivo: nesses eventos o canto "
        "do Kaggle é fabricado (o vermelho venceu 100%% das lutas em todos os anos de 1994 a "
        "2009 e nas 40 lutas de 2010-01-01 a 2010-03-20), logo o alvo é o vencedor renomeado, "
        "não o canto. Restam %d luta(s). Ver ADR 0006 e a Emenda 1.",
        n_fabricated,
        FIRST_RELIABLE_CORNER_DATE.isoformat(),
        len(raw) - n_fabricated,
    )
    return raw[~fabricated]


def build_dataset_without_window_filter(rows: pd.DataFrame) -> Dataset:
    """Constrói o dataset **sem** aplicar a janela de canto confiável.

    Caminho exclusivo do harness de ablação (``analysis.ablation``, bloco
    ``janela-canto-fabricado``), que precisa reproduzir a variante **histórica** do dataset
    -- a janela de 2010-01-01, anterior à Emenda 1 da ADR 0006 -- para medir a correção
    contra ela. A variante histórica é *menos* restritiva que a de produção, então não há
    como obtê-la filtrando a saída de ``build_dataset`` por cima.

    **Nenhum treino, walk-forward ou serving pode chamá-la**: o rótulo pré-janela é
    fabricado (ADR 0006) e o caminho de produção é ``build_dataset``. Esta extração é a
    alternativa exata à parametrização do corte que a ADR 0006 recusou -- um parâmetro com
    valor padrão convidaria a "treinar sem o filtro" na chamada mais próxima; uma função de
    nome denunciante fica visível em qualquer revisão e é guardada por teste
    (``test_caminho_sem_filtro_nao_e_usado_por_nenhum_treino_nem_pelo_serving``).

    Fora a janela, faz o mesmo que ``build_dataset`` -- ver a docstring dela.
    """
    decided = rows[rows[COL_TARGET].notna()].reset_index(drop=True)
    expanded = pd.DataFrame(list(decided[COL_FEATURES]), index=decided.index)
    numeric_columns = _numeric_feature_columns(expanded)
    if numeric_columns:
        features = expanded[numeric_columns].apply(pd.to_numeric).astype("float64")
        features = _drop_proscribed_feature_columns(features)
        features = _drop_all_nan_feature_columns(features)
    else:
        features = pd.DataFrame(index=decided.index)
    target = decided[COL_TARGET].map(_label_for).astype("int64")
    return Dataset(
        features=features,
        target=target,
        event_date=decided[COL_EVENT_DATE],
        bout_id=decided[COL_BOUT_ID],
        feature_names=[str(column) for column in features.columns],
    )


def build_dataset(raw: pd.DataFrame) -> Dataset:
    """Constrói o dataset preditivo a partir da frame crua de ``read_bout_features``.

    Aplica a janela de canto confiável (ADR 0006: nada anterior a
    ``FIRST_RELIABLE_CORNER_DATE`` chega ao treino), descarta linhas de alvo nulo
    (NC/empate), expande o JSONB ``features`` em colunas, seleciona apenas as numéricas (X)
    e mapeia o alvo para binário (y). O ``NaN`` das features é preservado (ausência
    explícita, sem imputação).

    Duas guardas correm sobre X, nesta ordem: as médias de carreira proscritas (vazamento de
    futuro, ADR 0002) e as colunas 100% ``NaN`` (backfill parcial). A ordem importa pouco no
    resultado, mas a proscrição vem antes para que uma coluna proibida e vazia seja reportada
    pelo motivo certo.

    Este é o **único** caminho de produção. A janela não é parâmetro e não se desliga aqui.
    """
    return build_dataset_without_window_filter(_drop_fabricated_corner_rows(raw))


def temporal_split(dataset: Dataset, test_fraction: float = 0.2) -> TemporalSplit:
    """Separa treino/teste por data de evento: as lutas mais recentes viram o teste.

    Ordena por ``(event_date, bout_id)`` de forma estável e reserva a fração final como
    holdout. Garante ``event_test.min() >= event_train.max()`` -- prova de ausência de
    vazamento temporal. ``test_fraction`` deve estar em ``(0, 1)`` e sobrar ao menos uma
    luta para treino e uma para teste.
    """
    if not 0.0 < test_fraction < 1.0:
        raise ValueError(f"test_fraction deve estar em (0, 1); recebido: {test_fraction}")
    order = (
        pd.DataFrame(
            {
                COL_EVENT_DATE: dataset.event_date.to_numpy(),
                COL_BOUT_ID: dataset.bout_id.to_numpy(),
            }
        )
        .sort_values([COL_EVENT_DATE, COL_BOUT_ID], kind="stable")
        .index.to_numpy()
    )
    n_total = len(order)
    n_test = max(1, round(n_total * test_fraction))
    n_train = n_total - n_test
    if n_train <= 0:
        raise ValueError(
            f"Amostras insuficientes ({n_total}) para um split temporal com treino não vazio."
        )
    train_pos = order[:n_train]
    test_pos = order[n_train:]
    event_train = dataset.event_date.iloc[train_pos]
    return TemporalSplit(
        x_train=dataset.features.iloc[train_pos],
        x_test=dataset.features.iloc[test_pos],
        y_train=dataset.target.iloc[train_pos],
        y_test=dataset.target.iloc[test_pos],
        event_train=event_train,
        event_test=dataset.event_date.iloc[test_pos],
        boundary_date=event_train.max(),
    )


def _descricao_da_coluna_descartada(column: str, x_test: pd.DataFrame) -> str:
    """Rótulo da coluna descartada com quantos valores ela tinha do lado do holdout.

    O número é o que torna o aviso acionável: separa "a feature não existe em lugar nenhum"
    de "a feature existe, mas o corte temporal a deixou inteira do lado do teste".
    """
    preenchidas = int(x_test[column].notna().sum()) if column in x_test.columns else 0
    return f"{column} ({preenchidas} valor(es), todos no holdout)"


def restrict_to_trainable_columns(split: TemporalSplit) -> TemporalSplit:
    """Descarta das **duas** fatias as colunas 100% ``NaN`` dentro do treino; loga quais saíram.

    Complemento indispensável de ``_drop_all_nan_feature_columns``, que enxerga só o dataset
    inteiro. Uma feature backfillada apenas para os anos recentes (o piloto de round-a-round
    cobre 2023-2025) tem dado -- e portanto sobrevive à guarda global --, mas o split temporal
    reserva justamente as lutas recentes para o holdout: dentro da fatia de treino ela é
    inteiramente ``NaN`` e o ``HistGradientBoostingClassifier`` levanta ``ValueError`` no
    binning do ``fit``.

    O descarte sai das duas fatias de propósito: um modelo treinado sem a coluna não pode
    recebê-la na predição. Coluna sem sinal no treino é coluna que o modelo não aprendeu --
    mantê-la no holdout só desalinharia o vetor. Quem roda o treino vê no log quais features
    saíram e quantos valores delas ficaram do lado do teste; o descarte nunca é silencioso.

    Colunas **parcialmente** preenchidas no treino são preservadas (o classificador trata
    ``NaN`` nativamente). O loop walk-forward aplica a mesma regra por passo, sobre a fatia
    anterior ao corte daquele evento.
    """
    sem_sinal = all_nan_columns(split.x_train)
    if not sem_sinal:
        return split
    logger.warning(
        "Descartando %d coluna(s) de feature sem nenhum valor na fatia de treino "
        "(corte temporal em %s; o dado existe apenas depois dele, invisível ao modelo): %s. "
        "As mesmas colunas saem do holdout para manter treino e predição alinhados.",
        len(sem_sinal),
        split.boundary_date.isoformat(),
        ", ".join(_descricao_da_coluna_descartada(column, split.x_test) for column in sem_sinal),
    )
    return replace(
        split,
        x_train=split.x_train.drop(columns=sem_sinal),
        x_test=split.x_test.drop(columns=sem_sinal),
    )
