"""Testes do loop walk-forward (Slice 09 da SPEC 007 -- M6).

O loop percorre os eventos da janela em ordem cronológica e, para cada um, treina
**exclusivamente** com lutas de eventos anteriores, prevê o card, grava as predições e
acumula a curva. Os testes cobrem, nesta ordem, o que a slice existe para garantir:

- o corte temporal por passo (``training_index_before``) é estrito -- nenhuma luta do
  evento previsto, nem posterior a ele, entra no treino (RF-12, anti-leakage);
- a janela de eventos é lida em ordem cronológica estável;
- o loop produz um passo por evento previsível, com ``train_max_date < event_date`` em
  **todos** eles;
- a curva acumulada (accuracy, log-loss, calibração, taxa real) é coerente passo a passo;
- a gravação em ``bout_predictions`` é idempotente por ``(bout_id, model_version)``;
- eventos sem base utilizável são pulados com aviso, sem derrubar o loop;
- o resumo final compara o pool contra o baseline ingênuo **sem exigir ganho**;
- a série de execuções é persistida sem sobrescrever a anterior.

A fixture principal semeia um histórico anterior à janela e 24 eventos dentro dela, contra
o Postgres de teste transacional -- a mesma disciplina dos demais testes de ``analysis``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.dataset import build_dataset
from analysis.metrics import metrics_delta
from analysis.walk_forward import (
    SCHEMA_VERSION,
    WalkForwardRun,
    list_window_events,
    run_walk_forward,
    save_run,
    training_index_before,
)
from apps.bouts.enums import Corner
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.features.models import BoutFeatures
from apps.fighters.tests.factories import FighterFactory
from apps.predictions.models import BoutPrediction
from apps.predictions.selectors import get_prediction_outcomes
from apps.predictions.services import SOURCE_WALK_FORWARD


def _raw_row(
    *,
    bout_id: int,
    event_date: date,
    target: str,
    features: dict[str, object],
) -> dict[str, object]:
    """Monta uma linha crua no formato devolvido por ``read_bout_features``."""
    return {
        "bout_id": bout_id,
        "event_date": event_date,
        "target_winner_corner": target,
        "features": features,
    }


def test_training_index_before_exclui_o_proprio_evento_e_todo_o_futuro() -> None:
    """O corte é **estrito**: só entram lutas de eventos anteriores ao corte.

    A entrada vem fora de ordem de propósito: o corte é por data, não por posição na
    frame. Uma luta na **mesma** data do evento previsto fica de fora -- não há ordenação
    intra-dia confiável e incluí-la seria vazamento (RF-12).
    """
    cutoff = date(2025, 9, 13)
    datas = [
        date(2025, 9, 27),  # posterior
        date(2025, 1, 5),  # anterior
        cutoff,  # o próprio evento previsto
        date(2024, 6, 1),  # anterior
        date(2026, 3, 7),  # posterior
    ]
    raw = pd.DataFrame(
        [
            _raw_row(
                bout_id=100 + posicao,
                event_date=event_date,
                target="red" if posicao % 2 == 0 else "blue",
                features={"reach_cm_diff": float(posicao)},
            )
            for posicao, event_date in enumerate(datas)
        ]
    )
    dataset = build_dataset(raw)

    idx = training_index_before(dataset, cutoff)

    assert dataset.event_date.loc[idx].max() < cutoff
    assert sorted(dataset.bout_id.loc[idx]) == [101, 103]
    # Nenhuma luta do evento do corte nem posterior a ele entrou no treino.
    assert not {102, 100, 104} & set(dataset.bout_id.loc[idx])


def test_list_window_events_respeita_a_janela_e_ordena_cronologicamente(
    db_session: Session,
) -> None:
    """Só os eventos da janela saem, em ordem crescente de ``(date, id)``.

    As datas são semeadas embaralhadas e uma delas cai fora dos dois lados da janela: a
    ordem da curva é a do calendário, nunca a de inserção. O par de eventos no mesmo dia
    prova o desempate estável por ``id``.
    """
    window_start = date(2025, 9, 6)
    window_end = date(2025, 12, 31)
    datas = [
        date(2026, 3, 7),  # depois da janela
        date(2025, 10, 4),
        date(2025, 6, 1),  # antes da janela
        date(2025, 9, 6),  # extremo inicial (inclusivo)
        date(2025, 12, 31),  # extremo final (inclusivo)
        date(2025, 10, 4),  # mesmo dia: desempate por id
    ]
    for indice, event_date in enumerate(datas):
        db_session.add(EventFactory.build(name=f"Evento {indice}", date=event_date))
    db_session.flush()

    eventos = list_window_events(db_session, window_start, window_end)

    assert [evento.date for evento in eventos] == [
        date(2025, 9, 6),
        date(2025, 10, 4),
        date(2025, 10, 4),
        date(2025, 12, 31),
    ]
    mesmo_dia = [evento.id for evento in eventos if evento.date == date(2025, 10, 4)]
    assert mesmo_dia == sorted(mesmo_dia)


# --- Base sintética compartilhada pelos testes do loop ---------------------------------

WINDOW_START = date(2025, 9, 6)
N_EVENTOS_DA_JANELA = 24
N_LUTAS_POR_EVENTO = 2
# Feature que só passa a ser preenchida no meio da janela: reproduz o backfill parcial
# (round-a-round de 2019-2025) que faz ``n_features`` variar de passo para passo.
FEATURE_TARDIA = "round1_sig_strike_share_r3_diff"
PRIMEIRO_EVENTO_COM_FEATURE_TARDIA = 8


@dataclass(frozen=True)
class BaseSemeada:
    """Estado semeado: os ids dos eventos da janela e o par de cantos de cada luta."""

    window_start: date
    window_end: date
    event_ids: list[int]
    cantos_por_luta: dict[int, tuple[int, int]]  # bout_id -> (red_fighter_id, blue_fighter_id)


def _seed_luta(
    session: Session,
    *,
    event_id: int,
    red_id: int,
    blue_id: int,
    vermelho_venceu: bool,
    features: dict[str, object],
) -> int:
    """Semeia a luta completa: ``bouts`` + os dois cantos + a linha de ``bout_features``.

    O vencedor real vai para ``bouts.winner_id`` (é dele que a leitura da Slice 07 deriva o
    acerto) e o canto vencedor, para o alvo do dataset -- os dois lados precisam concordar.
    """
    bout = BoutFactory.build(event_id=event_id, winner_id=red_id if vermelho_venceu else blue_id)
    session.add(bout)
    session.flush()
    session.add(BoutFighterFactory.build(bout_id=bout.id, fighter_id=red_id, corner=Corner.RED))
    session.add(BoutFighterFactory.build(bout_id=bout.id, fighter_id=blue_id, corner=Corner.BLUE))
    session.add(
        BoutFeatures(
            bout_id=bout.id,
            features=features,
            target_winner_corner=Corner.RED if vermelho_venceu else Corner.BLUE,
            source="feature-engineering",
            generated_at=datetime.now(UTC),
        )
    )
    session.flush()
    return int(bout.id)


def _features(indice: int, *, com_feature_tardia: bool) -> dict[str, object]:
    """Features sintéticas com sinal: ``reach_cm_diff`` correlaciona com o alvo.

    A correlação é imperfeita de propósito (uma luta em cada cinco contraria o sinal), para
    que o modelo tenha o que aprender sem virar preditor perfeito -- métrica perfeita não
    mediria nada.
    """
    vermelho_venceu = indice % 5 != 0
    return {
        "reach_cm_diff": 6.0 if vermelho_venceu else -6.0,
        "wins_prior_diff": float(indice % 7) - 3.0,
        FEATURE_TARDIA: 0.4 + (indice % 3) / 10 if com_feature_tardia else None,
    }


def _semeia_base(session: Session) -> BaseSemeada:
    """Semeia um histórico anterior à janela e 24 eventos dentro dela, 2 lutas cada.

    O histórico existe para que o primeiro passo já tenha treino com as duas classes; a
    janela existe para produzir a curva. A partir de ``PRIMEIRO_EVENTO_COM_FEATURE_TARDIA``
    a feature tardia passa a ter valor, reproduzindo o backfill parcial.
    """
    fighters = [FighterFactory.build(name=f"Lutador {indice}") for indice in range(12)]
    for fighter in fighters:
        session.add(fighter)
    session.flush()
    fighter_ids = [int(fighter.id) for fighter in fighters]
    cantos_por_luta: dict[int, tuple[int, int]] = {}

    # Histórico anterior à janela: 40 lutas de 2024-01 em diante, sem a feature tardia.
    for indice in range(40):
        event = EventFactory.build(
            name=f"Histórico {indice}", date=date(2024, 1, 1) + timedelta(days=7 * indice)
        )
        session.add(event)
        session.flush()
        red_id = fighter_ids[indice % len(fighter_ids)]
        blue_id = fighter_ids[(indice + 1) % len(fighter_ids)]
        bout_id = _seed_luta(
            session,
            event_id=int(event.id),
            red_id=red_id,
            blue_id=blue_id,
            vermelho_venceu=indice % 5 != 0,
            features=_features(indice, com_feature_tardia=False),
        )
        cantos_por_luta[bout_id] = (red_id, blue_id)

    # Janela: um evento por semana, duas lutas por card.
    event_ids: list[int] = []
    for passo in range(N_EVENTOS_DA_JANELA):
        event_date = WINDOW_START + timedelta(days=7 * (passo + 1))
        event = EventFactory.build(name=f"UFC Janela {passo}", date=event_date)
        session.add(event)
        session.flush()
        event_ids.append(int(event.id))
        for luta in range(N_LUTAS_POR_EVENTO):
            indice = 40 + passo * N_LUTAS_POR_EVENTO + luta
            red_id = fighter_ids[indice % len(fighter_ids)]
            blue_id = fighter_ids[(indice + 3) % len(fighter_ids)]
            bout_id = _seed_luta(
                session,
                event_id=int(event.id),
                red_id=red_id,
                blue_id=blue_id,
                vermelho_venceu=indice % 5 != 0,
                features=_features(
                    indice,
                    com_feature_tardia=passo >= PRIMEIRO_EVENTO_COM_FEATURE_TARDIA,
                ),
            )
            cantos_por_luta[bout_id] = (red_id, blue_id)

    return BaseSemeada(
        window_start=WINDOW_START,
        window_end=WINDOW_START + timedelta(days=7 * N_EVENTOS_DA_JANELA),
        event_ids=event_ids,
        cantos_por_luta=cantos_por_luta,
    )


@pytest.fixture
def base(db_session: Session) -> BaseSemeada:
    """Base sintética: histórico anterior à janela + 24 eventos dentro dela."""
    return _semeia_base(db_session)


@pytest.fixture
def run(db_session: Session, base: BaseSemeada) -> WalkForwardRun:
    """Uma execução completa do loop sobre a base sintética (reusada por vários testes)."""
    return run_walk_forward(db_session, base.window_start, base.window_end)


def test_walk_forward_produz_um_passo_por_evento_em_ordem_cronologica(
    run: WalkForwardRun, base: BaseSemeada
) -> None:
    """CA-10: um passo por evento da janela, em ordem cronológica estrita.

    A forma exigida pela SPEC (curva com pelo menos 20 eventos) é provada aqui sobre base
    semeada; a curva sobre a janela real vai no relatório de implementação.
    """
    assert len(run.steps) == N_EVENTOS_DA_JANELA
    assert len(run.steps) >= 20
    assert [passo.event_id for passo in run.steps] == base.event_ids
    datas = [passo.event_date for passo in run.steps]
    assert datas == sorted(datas)
    assert all(anterior < seguinte for anterior, seguinte in pairwise(datas))


def test_walk_forward_nunca_treina_com_o_evento_previsto_nem_com_o_futuro(
    run: WalkForwardRun,
) -> None:
    """RF-12: em **todo** passo o treino termina antes do evento previsto.

    Espelha ``test_temporal_split_nao_vaza_futuro_para_o_treino``: a prova é o par
    ``(train_max_date, event_date)`` de cada passo, não uma inspeção pontual.
    """
    assert run.steps
    for passo in run.steps:
        assert passo.train_max_date < passo.event_date
    # O treino cresce evento a evento: cada passo vê tudo o que o anterior viu, e mais.
    tamanhos = [passo.n_train for passo in run.steps]
    assert tamanhos == sorted(tamanhos)
    assert tamanhos[-1] > tamanhos[0]


def test_walk_forward_acumula_a_curva_passo_a_passo(run: WalkForwardRun) -> None:
    """CA-04: cada passo carrega a curva **acumulada** até ali, coerente com os anteriores.

    ``n_cumulative`` é a soma dos ``n_predicted`` até o passo -- é o que garante que a curva
    fala do pool inteiro de predições, e não de um card isolado. ``n_features >= 1`` em todo
    passo: um passo sem nenhuma feature utilizável não teria por que existir (seria pulado).
    """
    acumulado = 0
    for passo in run.steps:
        acumulado += passo.n_predicted
        assert passo.n_cumulative == acumulado
        assert 0.0 <= passo.cumulative_accuracy <= 1.0
        assert passo.cumulative_log_loss > 0.0
        assert passo.n_features >= 1
        assert passo.n_predicted >= 1

    n_cumulativos = [passo.n_cumulative for passo in run.steps]
    assert n_cumulativos == sorted(n_cumulativos)
    assert len(set(n_cumulativos)) == len(n_cumulativos)  # estritamente crescente


def test_walk_forward_reporta_calibracao_por_passo(run: WalkForwardRun) -> None:
    """CA-04: a curva expõe a probabilidade média apostada contra a taxa real do vermelho.

    É esse par que mostra **calibração** -- onde o dado recente melhora o modelo antes de
    melhorar o acerto. As duas quantidades são probabilidades e ficam em [0, 1]; a taxa
    acumulada real é, por definição, a fração de vitórias do vermelho no pool (e portanto
    também a accuracy do baseline ingênuo sobre o mesmo pool).
    """
    for passo in run.steps:
        assert 0.0 <= passo.step_mean_prob_red <= 1.0
        assert 0.0 <= passo.step_red_rate <= 1.0
        assert 0.0 <= passo.cumulative_mean_prob_red <= 1.0
        assert 0.0 <= passo.cumulative_red_rate <= 1.0

    ultimo = run.steps[-1]
    total = sum(passo.n_predicted for passo in run.steps)
    vitorias_vermelhas = sum(passo.step_red_rate * passo.n_predicted for passo in run.steps)
    assert ultimo.cumulative_red_rate == pytest.approx(vitorias_vermelhas / total)


def test_walk_forward_varia_o_numero_de_features_conforme_o_backfill_avanca(
    run: WalkForwardRun,
) -> None:
    """A feature preenchida só a partir do meio da janela entra quando passa a ter sinal.

    Antes disso ela é 100% ``NaN`` **dentro da fatia de treino** daquele passo e precisa sair
    -- se ficasse, o ``HistGradientBoostingClassifier`` levantaria no binning do ``fit``. É
    por isso que ``n_features`` é reportado por passo, e não uma vez para a execução toda.
    """
    larguras = [passo.n_features for passo in run.steps]

    assert min(larguras) < max(larguras)
    assert larguras == sorted(larguras)  # o backfill só acrescenta; nunca tira feature


def _predicoes(session: Session) -> list[BoutPrediction]:
    """Todas as linhas de ``bout_predictions``, em ordem determinística."""
    return list(session.scalars(select(BoutPrediction).order_by(BoutPrediction.id)).all())


def test_walk_forward_grava_uma_predicao_por_luta_prevista(
    db_session: Session, run: WalkForwardRun
) -> None:
    """CA-03: uma linha por luta prevista, com ``source="walk_forward"`` e a versão do passo.

    A versão e a largura gravadas são as **daquele passo**, não as da execução: é o que faz
    ``bout_predictions`` guardar a série temporal de modelos que a curva descreve.
    """
    linhas = _predicoes(db_session)

    assert len(linhas) == sum(passo.n_predicted for passo in run.steps)
    assert {linha.source for linha in linhas} == {SOURCE_WALK_FORWARD}
    versoes = {passo.model_version: passo for passo in run.steps}
    assert {linha.model_version for linha in linhas} == set(versoes)
    for linha in linhas:
        assert linha.n_features == versoes[linha.model_version].n_features


def test_walk_forward_grava_o_lutador_do_canto_de_maior_probabilidade(
    db_session: Session, base: BaseSemeada, run: WalkForwardRun
) -> None:
    """CA-03: ``predicted_winner_id`` é o lutador do canto que o modelo apostou.

    Sem neutralização de canto: numa luta persistida o canto é dado real, e a probabilidade
    gravada é a atribuída ao vencedor previsto -- por construção, nunca menor que 0,5.
    """
    linhas = _predicoes(db_session)

    assert len(linhas) == sum(passo.n_predicted for passo in run.steps)
    for linha in linhas:
        red_id, blue_id = base.cantos_por_luta[linha.bout_id]
        assert linha.predicted_winner_id in {red_id, blue_id}
        assert linha.prob_predicted_winner >= 0.5
        assert linha.prob_predicted_winner <= 1.0


def test_walk_forward_reexecutado_nao_duplica_linhas(
    db_session: Session, base: BaseSemeada, run: WalkForwardRun
) -> None:
    """CA-03: reexecutar a mesma janela sobre o mesmo estado não cria linha nova.

    A idempotência vem do ``model_version`` **determinístico**: mesmo corte, mesmo tamanho e
    mesma largura de treino produzem a mesma chave natural ``(bout_id, model_version)``, e o
    upsert da Slice 07 atualiza em vez de inserir.
    """
    antes = len(_predicoes(db_session))
    assert antes == sum(passo.n_predicted for passo in run.steps)

    segunda = run_walk_forward(db_session, base.window_start, base.window_end)

    assert [passo.model_version for passo in segunda.steps] == [
        passo.model_version for passo in run.steps
    ]
    assert len(_predicoes(db_session)) == antes


def test_walk_forward_grava_predicoes_confrontaveis_com_o_resultado_real(
    db_session: Session, run: WalkForwardRun
) -> None:
    """CA-03: a leitura da Slice 07 deriva o acerto das linhas que o loop gravou.

    Confirma que a curva e a tabela falam do mesmo desfecho: a fração de acertos derivada de
    ``bouts.winner_id`` bate com a accuracy acumulada do último passo.
    """
    assert run.steps
    acertos: list[bool] = []
    for linha in _predicoes(db_session):
        outcomes = get_prediction_outcomes(db_session, linha.bout_id)
        do_loop = [
            outcome for outcome in outcomes if outcome.prediction.source == SOURCE_WALK_FORWARD
        ]
        assert len(do_loop) == 1
        assert do_loop[0].hit is not None
        acertos.append(do_loop[0].hit)

    assert len(acertos) == run.steps[-1].n_cumulative
    assert sum(acertos) / len(acertos) == pytest.approx(run.steps[-1].cumulative_accuracy)


def _seed_evento_sem_base(
    session: Session, *, event_date: date, com_features: bool, red_id: int, blue_id: int
) -> int:
    """Semeia um evento da janela sem base utilizável e devolve o ``event_id``.

    ``com_features=False`` reproduz a luta que ainda não foi materializada em
    ``bout_features``; ``True`` reproduz a luta de alvo nulo (NC/empate), que ``build_dataset``
    descarta. Nos dois casos o evento não tem uma única linha no dataset.
    """
    event = EventFactory.build(name=f"Sem base {event_date.isoformat()}", date=event_date)
    session.add(event)
    session.flush()
    bout = BoutFactory.build(event_id=event.id, winner_id=None)
    session.add(bout)
    session.flush()
    session.add(BoutFighterFactory.build(bout_id=bout.id, fighter_id=red_id, corner=Corner.RED))
    session.add(BoutFighterFactory.build(bout_id=bout.id, fighter_id=blue_id, corner=Corner.BLUE))
    if com_features:
        session.add(
            BoutFeatures(
                bout_id=bout.id,
                features={"reach_cm_diff": 2.0, "wins_prior_diff": 1.0},
                target_winner_corner=None,  # NC/empate: sem alvo, fora do dataset
                source="feature-engineering",
                generated_at=datetime.now(UTC),
            )
        )
    session.flush()
    return int(event.id)


def test_walk_forward_pula_evento_sem_base_utilizavel_e_segue(
    db_session: Session, base: BaseSemeada, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-01: evento sem linha utilizável é pulado com aviso; o loop não para.

    Dois cenários de ausência, ambos reais: a luta ainda não materializada em
    ``bout_features`` e a luta de alvo nulo (NC/empate), que ``build_dataset`` descarta.
    Nenhum dos dois vira passo -- não se inventa métrica para um card que não existe --
    e os eventos seguintes seguem produzindo passos normalmente.
    """
    red_id, blue_id = next(iter(base.cantos_por_luta.values()))
    sem_features = _seed_evento_sem_base(
        db_session,
        event_date=base.window_start + timedelta(days=1),
        com_features=False,
        red_id=red_id,
        blue_id=blue_id,
    )
    alvo_nulo = _seed_evento_sem_base(
        db_session,
        event_date=base.window_start + timedelta(days=2),
        com_features=True,
        red_id=red_id,
        blue_id=blue_id,
    )

    with caplog.at_level(logging.WARNING, logger="analysis.walk_forward"):
        run = run_walk_forward(db_session, base.window_start, base.window_end)

    previstos = {passo.event_id for passo in run.steps}
    assert sem_features not in previstos
    assert alvo_nulo not in previstos
    assert previstos == set(base.event_ids)
    avisos = caplog.text
    assert str(sem_features) in avisos
    assert str(alvo_nulo) in avisos


def test_walk_forward_pula_passo_sem_treino_utilizavel_e_segue(
    db_session: Session, base: BaseSemeada, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-01: o passo cujo corte deixa o treino vazio ou de classe única é pulado.

    A janela começa antes do primeiro evento da base: o passo inicial não tem nenhuma luta
    anterior para treinar e os seguintes têm treino de uma só classe, onde o classificador
    não tem o que separar. Nenhum vira passo, o aviso diz por quê, e o loop segue até os
    eventos da janela do gap.
    """
    with caplog.at_level(logging.WARNING, logger="analysis.walk_forward"):
        run = run_walk_forward(db_session, date(2024, 1, 1), base.window_end)

    datas = [passo.event_date for passo in run.steps]
    assert datas
    assert datas[0] > date(2024, 1, 1)  # o primeiro evento da base não virou passo
    assert set(base.event_ids) <= {passo.event_id for passo in run.steps}
    assert "sem treino utilizável" in caplog.text


def test_walk_forward_resume_o_pool_contra_o_baseline_do_mesmo_pool(
    run: WalkForwardRun,
) -> None:
    """CA-05: o resumo final mede o pool inteiro e o compara ao baseline ingênuo.

    O baseline é o do **mesmo pool** (sempre o canto vermelho, com a prevalência do último
    treino): é o piso que o modelo precisa bater, e seu ROC-AUC degenera para 0,5 porque um
    preditor constante não ordena nada. O teste **não exige ganho** -- ausência de ganho é
    resultado válido nesta SPEC; o que se exige é que o delta reporte o sinal real.
    """
    assert run.final.n_samples == sum(passo.n_predicted for passo in run.steps)
    assert run.baseline.n_samples == run.final.n_samples
    assert run.baseline.roc_auc == 0.5
    assert run.final.accuracy == pytest.approx(run.steps[-1].cumulative_accuracy)
    assert run.final.log_loss == pytest.approx(run.steps[-1].cumulative_log_loss)
    # A accuracy do baseline é a taxa real de vitória do vermelho no pool -- por definição.
    assert run.baseline.accuracy == pytest.approx(run.steps[-1].cumulative_red_rate)

    delta = metrics_delta(run.final, run.baseline)
    assert delta.accuracy == pytest.approx(run.final.accuracy - run.baseline.accuracy)
    assert delta.improves_accuracy is (run.final.accuracy > run.baseline.accuracy)


def test_walk_forward_falha_alto_quando_a_janela_nao_tem_nenhum_passo(
    db_session: Session, base: BaseSemeada
) -> None:
    """Janela sem um único evento previsível levanta, em vez de devolver curva vazia.

    Nada de métrica fabricada: uma execução sobre janela vazia não tem accuracy 0,0 nem
    baseline -- ela não tem resultado nenhum, e dizer isso alto é o comportamento honesto.
    """
    depois_da_base = base.window_end + timedelta(days=30)

    with pytest.raises(ValueError, match="nenhum evento previsível"):
        run_walk_forward(db_session, depois_da_base, depois_da_base + timedelta(days=60))


def test_save_run_persiste_a_curva_sem_sobrescrever_a_execucao_anterior(
    run: WalkForwardRun, tmp_path: Path
) -> None:
    """CA-04: cada execução vira um arquivo próprio; a série anterior permanece.

    Sobrescrever um nome fixo destruiria a comparabilidade que a RF-11 existe para dar --
    a curva de hoje só significa alguma coisa ao lado da de ontem.
    """
    assert run.window_end is not None  # a fixture fecha a janela dos dois lados
    primeiro = save_run(run, directory=tmp_path)
    segundo = save_run(
        replace(run, started_at=run.started_at + timedelta(minutes=5)), directory=tmp_path
    )

    assert primeiro != segundo
    assert sorted(caminho.name for caminho in tmp_path.iterdir()) == sorted(
        [primeiro.name, segundo.name]
    )

    conteudo = json.loads(primeiro.read_text(encoding="utf-8"))
    assert conteudo["schema_version"] == SCHEMA_VERSION
    assert conteudo["window_start"] == run.window_start.isoformat()
    assert conteudo["window_end"] == run.window_end.isoformat()
    assert conteudo["random_state"] == run.random_state
    assert len(conteudo["steps"]) == len(run.steps)
    primeiro_passo = conteudo["steps"][0]
    assert primeiro_passo["event_id"] == run.steps[0].event_id
    assert primeiro_passo["event_date"] == run.steps[0].event_date.isoformat()
    assert primeiro_passo["train_max_date"] == run.steps[0].train_max_date.isoformat()
    assert primeiro_passo["model_version"] == run.steps[0].model_version
    for campo in (
        "n_train",
        "n_features",
        "n_predicted",
        "cumulative_accuracy",
        "cumulative_log_loss",
        "cumulative_mean_prob_red",
        "cumulative_red_rate",
        "n_cumulative",
    ):
        assert primeiro_passo[campo] == getattr(run.steps[0], campo)
    assert conteudo["final"]["n_samples"] == run.final.n_samples
    assert conteudo["baseline"]["accuracy"] == run.baseline.accuracy
