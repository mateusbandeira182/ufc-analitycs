"""Loop walk-forward: percorre a janela evento a evento, re-treinando a cada passo.

Última slice da SPEC 007 (M6 -- RF-11, RF-12). Responde à pergunta que motiva a SPEC
inteira: **a base enriquecida melhora o modelo?** O método é o único honesto para uma
série temporal -- para cada evento da janela, treinar exclusivamente com o que já havia
acontecido, prever o card, comparar com o resultado real e só então avançar. Nenhum passo
enxerga o evento que prevê.

A curva reporta, por passo, accuracy e log-loss **acumulados**, o número de features que o
treino daquele passo pôde usar, a probabilidade média apostada e a taxa real de vitória do
canto vermelho -- é esse último par que mostra **calibração**, onde o ganho do dado recente
aparece antes de aparecer em acerto.

Duas decisões de forma sustentam o módulo:

- **A predição do passo lê a linha já persistida em ``bout_features``**, não a luta
  sintética de ``analysis.predict``. Aquela é datada em "agora" e enxergaria lutas
  posteriores ao evento previsto -- vazamento frontal num walk-forward retrospectivo. A
  linha de ``bout_features`` é point-in-time por construção (todo agregado do M4/M5 usa
  ``shift(1)`` antes de qualquer rolling/expanding).
- **Sem neutralização de canto.** A média das duas ordens (``apps.predictions.services``) é
  decisão de apresentação para confrontos hipotéticos, onde "quem é A" é arbitrário. Numa
  luta persistida o canto é dado real e o modelo legitimamente o usa -- é assim que o
  holdout de ``analysis.model`` mede, e é o que mantém a curva comparável ao baseline e à
  ``PRE_M5_REFERENCE``.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.dataset import Dataset, all_nan_columns, build_dataset, read_bout_features
from analysis.metrics import (
    PRE_M5_REFERENCE,
    Metrics,
    MetricsDelta,
    accuracy_log_loss,
    baseline_metrics,
    compute_metrics,
    metrics_delta,
)
from analysis.model import ARTIFACTS_DIR, RANDOM_STATE, train_model
from apps.bouts.enums import Corner
from apps.events.models import Event
from apps.events.selectors import list_event_bouts
from apps.predictions.services import SOURCE_WALK_FORWARD, record_prediction
from mma_analytics.db import SessionLocal

logger = logging.getLogger(__name__)

# Último evento do seed Kaggle e início da janela do gap fechada pela Cito (SPEC 007).
GAP_START = date(2025, 9, 6)

# Série de execuções da curva; um arquivo por execução, nunca sobrescrito.
RUNS_DIR = ARTIFACTS_DIR / "walk_forward"
SCHEMA_VERSION = 1

# Rótulo da classe positiva do modelo: canto vermelho (convenção fixa A = red, ADR 0001).
_RED_LABEL = 1


@dataclass(frozen=True)
class WalkForwardStep:
    """Um passo do loop: um evento previsto por um modelo que só viu o passado.

    ``train_max_date`` é a prova por passo do anti-leakage: por construção é sempre
    estritamente anterior a ``event_date`` (RF-12).

    As métricas são **acumuladas** sobre todas as lutas previstas até este passo, inclusive
    -- é a curva que a SPEC pede, não a métrica isolada de um card de dez lutas, ruidosa
    demais para ser lida. ``cumulative_red_rate`` é, ao mesmo tempo, a taxa real de vitória
    do vermelho no pool e a accuracy do baseline ingênuo que sempre aposta no vermelho:
    são a mesma quantidade, e é por isso que ela serve de detector de vazamento (um salto
    dela denuncia rótulo contaminado).
    """

    event_id: int
    event_date: date
    event_name: str
    model_version: str
    n_train: int
    n_features: int
    n_predicted: int
    train_max_date: date
    step_mean_prob_red: float
    step_red_rate: float
    cumulative_accuracy: float
    cumulative_log_loss: float
    cumulative_mean_prob_red: float
    cumulative_red_rate: float
    n_cumulative: int


@dataclass(frozen=True)
class WalkForwardRun:
    """Execução completa: a curva, o resumo honesto e os parâmetros que a reproduzem.

    ``final`` mede o pool inteiro de predições (todas as lutas de todos os passos) e
    ``baseline``, o preditor ingênuo que sempre aposta no canto vermelho sobre **o mesmo
    pool** -- o piso a superar. Ausência de ganho é resultado válido: o par existe para ser
    reportado como for, não para ser filtrado até parecer favorável.
    """

    started_at: datetime
    window_start: date
    window_end: date | None
    random_state: int
    steps: list[WalkForwardStep]
    final: Metrics
    baseline: Metrics


def training_index_before(dataset: Dataset, cutoff: date) -> pd.Index:
    """Índice das linhas de eventos **estritamente** anteriores a ``cutoff``.

    Comparação estrita de propósito: um evento na mesma data do previsto fica de fora --
    não há ordenação intra-dia confiável e incluí-lo seria vazamento (RF-12).

    ``build_dataset`` faz ``reset_index(drop=True)``, então ``features``, ``target``,
    ``event_date`` e ``bout_id`` compartilham o mesmo ``RangeIndex``: o índice devolvido
    endereça as quatro séries via ``.loc``. Esse alinhamento é o invariante que o loop usa.
    """
    return dataset.event_date.index[dataset.event_date < cutoff]


def list_window_events(
    session: Session,
    window_start: date = GAP_START,
    window_end: date | None = None,
) -> list[Event]:
    """Eventos da janela em ordem cronológica estável (``date``, depois ``id``).

    ``window_start`` é **inclusivo** e ``window_end``, quando dado, também -- a janela é
    fechada dos dois lados para que uma execução passada seja reproduzível ao pé da letra.
    O desempate por ``id`` mantém a ordem estável entre execuções quando dois eventos caem
    no mesmo dia.

    Fica no pacote ``analysis`` (e não em ``apps.events.selectors``) pelo mesmo precedente
    de ``analysis.dataset.read_bout_features``: leitura que serve exclusivamente à camada
    de análise não precisa engordar a API pública de um app.
    """
    stmt = select(Event).where(Event.date >= window_start)
    if window_end is not None:
        stmt = stmt.where(Event.date <= window_end)
    return list(session.scalars(stmt.order_by(Event.date.asc(), Event.id.asc())).all())


def walk_forward_model_version(cutoff: date, n_train: int, n_features: int) -> str:
    """Chave natural do modelo do passo -- determinística, cabe em ``String(64)``.

    É a metade variável do upsert de ``bout_predictions`` (``bout_id`` + ``model_version``):
    um timestamp de relógio faria cada reexecução gravar linha nova e quebraria a
    idempotência exigida pela SPEC. O trio ``(corte temporal, tamanho do treino, largura do
    treino)`` é exatamente o que caracteriza o modelo daquele passo, e nenhum deles depende
    do momento em que o loop rodou. Ex.: ``"wf-2025-09-13-n8170-f39"``.
    """
    return f"wf-{cutoff.isoformat()}-n{n_train}-f{n_features}"


@dataclass(frozen=True)
class _StepPrediction:
    """Resultado bruto de um passo: o que o modelo apostou em cada luta do card.

    Interno ao módulo: a curva e a gravação em ``bout_predictions`` derivam daqui, sempre
    das mesmas probabilidades -- é o que impede a tabela e o relatório de discordarem sobre
    o mesmo passo.
    """

    bout_ids: list[int]
    y_true: list[int]
    prob_red: list[float]
    n_train: int
    train_max_date: date
    feature_columns: list[str]
    y_train: pd.Series


def _trainable_columns(x_train: pd.DataFrame) -> list[str]:
    """Colunas com ao menos um valor **dentro da fatia de treino** daquele passo.

    O descarte global de ``build_dataset`` não protege o passo: uma feature preenchida só
    depois do corte (o round-a-round backfillado para os anos recentes) fica 100% ``NaN``
    na fatia e o ``HistGradientBoostingClassifier`` levanta no binning do ``fit``. É daqui
    que sai o ``n_features`` **variável por passo** que a curva reporta.
    """
    sem_sinal = set(all_nan_columns(x_train))
    return [str(column) for column in x_train.columns if str(column) not in sem_sinal]


@dataclass
class _Pool:
    """Pool acumulado de predições ao longo dos passos -- a base da curva.

    Mutável de propósito: é um acumulador, e recomputar as métricas sobre ele a cada passo
    é o que produz a curva acumulada (em vez de médias de médias, que ponderariam errado
    cards de tamanhos diferentes).
    """

    y_true: list[int]
    prob_red: list[float]

    def extend(self, y_true: Sequence[int], prob_red: Sequence[float]) -> None:
        """Acrescenta as lutas de um passo ao pool."""
        self.y_true.extend(y_true)
        self.prob_red.extend(prob_red)

    @property
    def y_pred(self) -> list[int]:
        """Rótulo previsto: vermelho quando a probabilidade dele atinge 0,5."""
        return [_RED_LABEL if prob >= 0.5 else 0 for prob in self.prob_red]


def _mean(values: Sequence[float]) -> float:
    """Média simples; assume sequência não vazia (um passo sem luta prevista não existe)."""
    return sum(values) / len(values)


def _predict_step(
    dataset: Dataset, event_date: date, card_index: pd.Index, random_state: int
) -> _StepPrediction | str:
    """Treina com o passado estrito e prevê o card; devolve o **motivo** quando inviável.

    Inviável = sem linhas de treino anteriores ao corte, com uma só classe no treino (o
    classificador não tem o que separar) ou sem uma única feature com sinal antes do corte.
    Nesses casos não se inventa passo: ausência é ausência, e o motivo volta em texto para
    quem chama registrar no log.
    """
    train_index = training_index_before(dataset, event_date)
    if len(train_index) == 0:
        return "sem treino utilizável: nenhuma luta anterior ao corte"
    y_train = dataset.target.loc[train_index]
    if y_train.nunique() < 2:
        return (
            f"sem treino utilizável: as {len(train_index)} luta(s) anteriores ao corte têm "
            f"uma única classe de alvo"
        )
    x_train = dataset.features.loc[train_index]
    columns = _trainable_columns(x_train)
    if not columns:
        return "sem treino utilizável: nenhuma feature com valor antes do corte"

    model = train_model(x_train[columns], y_train, random_state)
    # Mesmo conjunto de colunas do treino: um modelo treinado sem a coluna não pode recebê-la.
    prob_red = model.predict_proba(dataset.features.loc[card_index, columns])[
        :, list(model.classes_).index(_RED_LABEL)
    ]
    return _StepPrediction(
        bout_ids=[int(bout_id) for bout_id in dataset.bout_id.loc[card_index]],
        y_true=[int(label) for label in dataset.target.loc[card_index]],
        prob_red=[float(valor) for valor in prob_red],
        n_train=len(train_index),
        train_max_date=dataset.event_date.loc[train_index].max(),
        feature_columns=columns,
        y_train=y_train,
    )


def run_walk_forward(
    session: Session,
    window_start: date = GAP_START,
    window_end: date | None = None,
    random_state: int = RANDOM_STATE,
) -> WalkForwardRun:
    """Percorre a janela evento a evento, re-treinando a cada passo. **Não commita.**

    Lê o dataset uma única vez -- as linhas de ``bout_features`` são point-in-time por
    construção, então o que muda de passo para passo é o **corte**, não as features. Cada
    evento vira um passo, treinado exclusivamente com lutas anteriores a ele (RF-12).

    A transação é do chamador: sem commit aqui, o loop roda sob a fixture transacional dos
    testes e o ``main`` decide quando persistir.
    """
    started_at = datetime.now(UTC)
    dataset = build_dataset(read_bout_features(session))
    steps: list[WalkForwardStep] = []
    pool = _Pool(y_true=[], prob_red=[])
    # Prevalência do último treino: é a aposta do baseline ingênuo sobre o pool inteiro.
    ultimo_treino: pd.Series = pd.Series(dtype="int64")
    for event in list_window_events(session, window_start, window_end):
        corners = _card_corners(session, event)
        card_index = dataset.bout_id.index[dataset.bout_id.isin(list(corners))]
        if len(card_index) == 0:
            _log_skip(event, "nenhuma luta do card tem linha utilizável em bout_features")
            continue
        prediction = _predict_step(dataset, event.date, card_index, random_state)
        if isinstance(prediction, str):
            _log_skip(event, prediction)
            continue
        pool.extend(prediction.y_true, prediction.prob_red)
        step = _to_step(event, prediction, pool)
        _record_step(session, prediction, corners, step.model_version, step.n_features)
        steps.append(step)
        ultimo_treino = prediction.y_train
    if not steps:
        raise ValueError(
            f"A janela [{window_start.isoformat()}, "
            f"{'aberta' if window_end is None else window_end.isoformat()}] não produziu "
            f"nenhum evento previsível; sem passo não há curva a reportar."
        )
    return WalkForwardRun(
        started_at=started_at,
        window_start=window_start,
        window_end=window_end,
        random_state=random_state,
        steps=steps,
        final=compute_metrics(pool.y_true, pool.y_pred, pool.prob_red),
        baseline=baseline_metrics(ultimo_treino, pool.y_true),
    )


def _log_skip(event: Event, motivo: str) -> None:
    """Registra o evento pulado e o motivo -- passo ausente nunca é silencioso.

    Um evento pulado não vira ponto na curva: inventar métrica para um card que não existe
    seria fabricar resultado. O log é o que torna a lacuna auditável ao ler a curva.
    """
    logger.warning(
        "Evento %d (%s, %s) pulado no walk-forward: %s.",
        event.id,
        event.name,
        event.date.isoformat(),
        motivo,
    )


def _card_corners(session: Session, event: Event) -> dict[int, tuple[int, int]]:
    """Cantos de cada luta do card: ``bout_id -> (vermelho, azul)``.

    Reusa ``list_event_bouts``, que já traz ``bout_fighters`` eager-loaded (sem N+1). Uma
    luta sem exatamente um lutador em cada canto fica **fora do passo inteiro** -- nem curva
    nem gravação. Sem os dois cantos não há vencedor a nomear em ``bout_predictions``, e
    contá-la só na curva faria a tabela e o relatório discordarem sobre o mesmo passo.
    """
    corners: dict[int, tuple[int, int]] = {}
    for bout in list_event_bouts(session, int(event.id)):
        red = [bf.fighter_id for bf in bout.bout_fighters if bf.corner == Corner.RED]
        blue = [bf.fighter_id for bf in bout.bout_fighters if bf.corner == Corner.BLUE]
        if len(red) == 1 and len(blue) == 1:
            corners[int(bout.id)] = (int(red[0]), int(blue[0]))
        else:
            logger.warning(
                "Luta %d do evento %d (%s) sem exatamente os dois cantos cadastrados; "
                "fora do passo do walk-forward.",
                bout.id,
                event.id,
                event.name,
            )
    return corners


def _record_step(
    session: Session,
    prediction: _StepPrediction,
    corners: dict[int, tuple[int, int]],
    model_version: str,
    n_features: int,
) -> None:
    """Grava as predições do passo em ``bout_predictions`` (idempotente, sem commit).

    A probabilidade gravada é sempre a do **vencedor previsto** (a maior das duas), como o
    registro da Slice 07 espera. A transação é do chamador -- o ``main`` commita uma vez, ao
    fim da execução.
    """
    for bout_id, prob_red in zip(prediction.bout_ids, prediction.prob_red, strict=True):
        red_id, blue_id = corners[bout_id]
        if prob_red >= 0.5:
            winner_id, prob_winner = red_id, prob_red
        else:
            winner_id, prob_winner = blue_id, 1.0 - prob_red
        record_prediction(
            session,
            bout_id=bout_id,
            predicted_winner_id=winner_id,
            prob_predicted_winner=prob_winner,
            model_version=model_version,
            n_features=n_features,
            source=SOURCE_WALK_FORWARD,
        )


def _to_step(event: Event, prediction: _StepPrediction, pool: _Pool) -> WalkForwardStep:
    """Monta o passo da curva: o que aconteceu neste card e o acumulado até aqui.

    O pool já vem com as lutas deste passo dentro -- a curva de um passo inclui o próprio
    card, que o modelo daquele passo previu sem o ter visto no treino.
    """
    n_features = len(prediction.feature_columns)
    cumulative_accuracy, cumulative_log_loss = accuracy_log_loss(
        pool.y_true, pool.y_pred, pool.prob_red
    )
    return WalkForwardStep(
        event_id=int(event.id),
        event_date=event.date,
        event_name=event.name,
        model_version=walk_forward_model_version(event.date, prediction.n_train, n_features),
        n_train=prediction.n_train,
        n_features=n_features,
        n_predicted=len(prediction.bout_ids),
        train_max_date=prediction.train_max_date,
        step_mean_prob_red=_mean(prediction.prob_red),
        step_red_rate=_mean([float(label) for label in prediction.y_true]),
        cumulative_accuracy=cumulative_accuracy,
        cumulative_log_loss=cumulative_log_loss,
        cumulative_mean_prob_red=_mean(pool.prob_red),
        cumulative_red_rate=_mean([float(label) for label in pool.y_true]),
        n_cumulative=len(pool.y_true),
    )


def _step_payload(step: WalkForwardStep) -> dict[str, object]:
    """Serializa um passo da curva; datas em ISO-8601, o resto como número cru."""
    return {
        "event_id": step.event_id,
        "event_date": step.event_date.isoformat(),
        "event_name": step.event_name,
        "model_version": step.model_version,
        "n_train": step.n_train,
        "n_features": step.n_features,
        "n_predicted": step.n_predicted,
        "train_max_date": step.train_max_date.isoformat(),
        "step_mean_prob_red": step.step_mean_prob_red,
        "step_red_rate": step.step_red_rate,
        "cumulative_accuracy": step.cumulative_accuracy,
        "cumulative_log_loss": step.cumulative_log_loss,
        "cumulative_mean_prob_red": step.cumulative_mean_prob_red,
        "cumulative_red_rate": step.cumulative_red_rate,
        "n_cumulative": step.n_cumulative,
    }


def save_run(run: WalkForwardRun, directory: Path = RUNS_DIR) -> Path:
    """Persiste a curva de uma execução em JSON e devolve o caminho; nunca sobrescreve.

    O nome deriva do ``started_at`` justamente para que execuções sucessivas se acumulem: a
    curva de uma execução só significa alguma coisa ao lado das anteriores (RF-11), e um
    nome fixo apagaria a série a cada rodada.
    """
    directory.mkdir(parents=True, exist_ok=True)
    carimbo = run.started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%f")
    path = directory / f"walk_forward-{carimbo}.json"
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "started_at": run.started_at.isoformat(),
        "window_start": run.window_start.isoformat(),
        "window_end": None if run.window_end is None else run.window_end.isoformat(),
        "random_state": run.random_state,
        "steps": [_step_payload(step) for step in run.steps],
        "final": asdict(run.final),
        "baseline": asdict(run.baseline),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _log_step(step: WalkForwardStep) -> None:
    """Reporta uma linha da curva: o passo, o treino que o produziu e o acumulado.

    A probabilidade média apostada ao lado da taxa real é o par que mostra **calibração** --
    é onde o dado recente melhora o modelo antes de melhorar o acerto.
    """
    logger.info(
        "%s %-46.46s n_train=%-6d n_features=%-3d n=%-3d | acc=%.4f logloss=%.4f "
        "| p_red=%.4f real=%.4f (n_acum=%d)",
        step.event_date.isoformat(),
        step.event_name,
        step.n_train,
        step.n_features,
        step.n_predicted,
        step.cumulative_accuracy,
        step.cumulative_log_loss,
        step.cumulative_mean_prob_red,
        step.cumulative_red_rate,
        step.n_cumulative,
    )


def _log_metrics(rotulo: str, metrics: Metrics) -> None:
    """Reporta um bloco de métricas (pool, baseline ou referência) via ``logging``."""
    logger.info(
        "%s: accuracy=%.4f log_loss=%.4f roc_auc=%.4f (n=%d)",
        rotulo,
        metrics.accuracy,
        metrics.log_loss,
        metrics.roc_auc,
        metrics.n_samples,
    )


def _log_delta(rotulo: str, delta: MetricsDelta) -> None:
    """Reporta um delta com o veredito de accuracy, sem overclaim.

    ``não supera`` é resultado válido: o valor da slice é a medição, não o ganho. Log-loss
    menor é melhor (delta negativo é melhora), accuracy e ROC-AUC maiores são melhores.
    """
    veredito = "supera" if delta.improves_accuracy else "não supera"
    logger.info(
        "%s: accuracy=%+.4f log_loss=%+.4f roc_auc=%+.4f -> o walk-forward %s em accuracy.",
        rotulo,
        delta.accuracy,
        delta.log_loss,
        delta.roc_auc,
        veredito,
    )


def _log_run(run: WalkForwardRun, path: Path) -> None:
    """Reporta a curva inteira, o resumo e as duas comparações honestas."""
    logger.info(
        "Walk-forward concluído: %d passo(s) na janela [%s, %s], %d luta(s) previstas.",
        len(run.steps),
        run.window_start.isoformat(),
        "aberta" if run.window_end is None else run.window_end.isoformat(),
        run.final.n_samples,
    )
    for step in run.steps:
        _log_step(step)
    _log_metrics("Walk-forward ", run.final)
    _log_metrics("Baseline     ", run.baseline)
    _log_metrics("Pré-M5       ", PRE_M5_REFERENCE)
    _log_delta("Delta vs. baseline", metrics_delta(run.final, run.baseline))
    _log_delta("Delta vs. pré-M5  ", metrics_delta(run.final, PRE_M5_REFERENCE))
    logger.info("Teto real: linha de mercado (~0.58 corner vermelho).")
    logger.info("Curva salva em: %s", path)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Interpreta os argumentos de ``python -m analysis.walk_forward``."""
    parser = argparse.ArgumentParser(
        description=(
            "Percorre os eventos da janela em ordem cronológica, re-treinando a cada passo "
            "com apenas o passado, e reporta a curva acumulada de accuracy e log-loss."
        ),
    )
    parser.add_argument(
        "--window-start",
        type=date.fromisoformat,
        default=GAP_START,
        help="Primeira data da janela (inclusiva), em ISO-8601. Padrão: início do gap.",
    )
    parser.add_argument(
        "--window-end",
        type=date.fromisoformat,
        default=None,
        help="Última data da janela (inclusiva), em ISO-8601. Padrão: sem limite superior.",
    )
    parser.add_argument(
        "--random-state",
        type=int,
        default=RANDOM_STATE,
        help="Semente do classificador, para tornar cada passo reprodutível.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entrypoint: roda o loop, **persiste a curva, commita** e só então reporta.

    A ordem é deliberada: uma falha na apresentação não pode custar uma execução de minutos
    nem as predições já calculadas. ``run_walk_forward`` não commita (é o que o torna
    testável sob a fixture transacional), então o commit é aqui.
    """
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    with SessionLocal() as session:
        run = run_walk_forward(session, args.window_start, args.window_end, args.random_state)
        path = save_run(run)
        session.commit()
    _log_run(run, path)


if __name__ == "__main__":
    main()
