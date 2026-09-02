"""Testes do harness de medição por bloco (Slice 00 da SPEC 009 -- M8).

O harness é a régua que a RF-06 exige e que o repositório não tinha: mede um bloco de
features por ablação com tudo mais fixo (mesmo dataset, mesmo split temporal, mesmo
``random_state``), quantifica a incerteza por bootstrap pareado (B=10.000, IC 95%) e
registra o veredito pré-comprometido da RF-07 (``ci_high < 0``).

O risco central da slice é um harness que valida a si mesmo -- um bootstrap errado produz
um IC plausível e ninguém percebe. A amarra em teste é
``test_bootstrap_delta_pontual_coincide_com_log_loss_do_sklearn``: ela prende o delta
pontual à métrica canônica de ``sklearn.metrics``. A segunda amarra (execução real contra o
número tabelado no PRD) vive no relatório de implementação, não aqui.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sklearn.metrics import log_loss
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analysis import ablation
from analysis.ablation import (
    BLOCK_REGISTRY,
    SCHEMA_VERSION,
    WINDOW_BLOCK_NAME,
    BlockMeasurement,
    BootstrapDelta,
    ColumnCoverage,
    FeatureBlock,
    block_columns,
    measure_block,
    paired_bootstrap_log_loss_delta,
    save_measurement,
)
from analysis.dataset import TemporalSplit
from apps.bouts.enums import Corner
from apps.bouts.models import Bout
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.events.models import Event
from apps.features.models import BoutFeatures
from apps.fighters.tests.factories import FighterFactory
from ingestion.features.rolling import (
    CAREER_MINUTES_BEFORE,
    CONTROL_TIME_AVG_TREND,
    KO_LOSSES_PRIOR,
    OPPONENT_WIN_RATE_PRIOR_AVG,
    ROUND1_SIG_STRIKE_SHARE_R3,
    SHARE_BODY_R3,
    SHARE_CLINCH_R3,
    SHARE_DISTANCE_R3,
    SHARE_GROUND_R3,
    SHARE_HEAD_R3,
    SHARE_LEG_R3,
    SIG_STRIKES_ABSORBED_PM_TREND,
    SIG_STRIKES_LANDED_PM_TREND,
    SIMILAR_STYLE_WIN_RATE_PRIOR,
    SUBMISSION_LOSSES_PRIOR,
    TAKEDOWN_DEFENSE_TREND,
    TAKEDOWNS_LANDED_AVG_TREND,
)

# Bootstrap enxuto nos testes: a corretude do estimador não depende de B, e 10.000
# reamostragens por teste puro custariam segundos sem provar nada a mais. O B de produção
# (``BOOTSTRAP_RESAMPLES``) é o que a RF-06 fixa e o que a execução real usa.
B_DE_TESTE = 500


def _holdout_sintetico(n: int = 200) -> tuple[list[int], list[float], list[float]]:
    """Holdout balanceado: rótulos alternados, um modelo informado e um desinformado.

    ``prob_bom`` acerta o lado do rótulo na maioria das linhas, com **confiança variável**,
    e erra uma linha em sete; ``prob_ruim`` é constante em 0,5 (não sabe nada). A
    heterogeneidade é deliberada: com confiança uniforme a perda por linha seria a mesma em
    toda linha, a diferença entre os modelos seria constante e o IC degeneraria em um ponto
    -- correto, porém incapaz de exercitar a reamostragem. Aqui o primeiro modelo tem
    log-loss estritamente menor e há variância real entre as linhas, que é o cenário em que
    o IC do delta precisa ficar inteiro abaixo de zero.
    """
    y_true = [indice % 2 for indice in range(n)]
    confiancas = (0.6, 0.7, 0.8, 0.9, 0.75, 0.65, 0.85)
    prob_bom: list[float] = []
    for indice, rotulo in enumerate(y_true):
        erra = indice % 7 == 3
        p_classe_verdadeira = 0.35 if erra else confiancas[indice % len(confiancas)]
        prob_bom.append(p_classe_verdadeira if rotulo == 1 else 1.0 - p_classe_verdadeira)
    prob_ruim = [0.5] * n
    return y_true, prob_bom, prob_ruim


def test_registro_do_bloco_dimensoes_ausentes_tem_as_doze_bases_as_of() -> None:
    """CA-08 da SPEC 009: o bloco A entra no registro com exatamente as 12 bases as-of.

    A comparação é de conjunto contra a lista literal: uma base esquecida no registro faria
    o bloco ser medido incompleto e o veredito valeria para outra coisa que não o bloco A.
    Nenhuma feature bout-level -- o bloco A é todo por lutador (12 bases -> 36 colunas).
    """
    bloco = BLOCK_REGISTRY["dimensoes-ausentes"]

    assert set(bloco.asof_bases) == {
        "sig_strike_accuracy_r3",
        "head_accuracy_r3",
        "body_accuracy_r3",
        "leg_accuracy_r3",
        "distance_accuracy_r3",
        "clinch_accuracy_r3",
        "ground_accuracy_r3",
        "takedown_accuracy_r3",
        "knockdowns_pm_r3",
        "knockdowns_avg_r3",
        "submission_attempts_avg_r3",
        "total_to_sig_strike_ratio_r3",
    }
    assert len(bloco.asof_bases) == 12
    assert bloco.bout_level_features == ()
    assert len(block_columns(bloco)) == 36


def test_registro_dos_dois_sub_blocos_de_divisao_e_independente() -> None:
    """CA-03/CA-04 da SPEC 009: divisão entra como **dois** blocos, não um.

    A categoria física e a taxa base histórica respondem perguntas diferentes e podem pagar
    uma sem a outra -- medi-las juntas produziria um veredito só, que não diz qual das duas
    carregou (ou afundou) o resultado. As colunas vêm das listas nomeadas de
    ``ingestion.features.division``, nunca redigitadas: uma string desatualizada faria a
    coluna sumir do bloco em silêncio e o veredito valeria para um bloco menor do que o
    medido diz ser.
    """
    categoria = BLOCK_REGISTRY["divisao-categoria"]
    taxa_base = BLOCK_REGISTRY["divisao-taxa-base"]

    assert categoria is not taxa_base
    assert set(categoria.bout_level_features).isdisjoint(taxa_base.bout_level_features)
    # Nenhum dos dois tem base as-of: divisão é propriedade da luta, não do lutador.
    assert categoria.asof_bases == ()
    assert taxa_base.asof_bases == ()


def test_blocos_de_divisao_resolvem_para_colunas_unicas_sem_sufixo_de_canto() -> None:
    """CA-14: as colunas do bloco são bout-level -- sem ``_a``/``_b``, sem ``_diff``.

    Se o registro pedisse o trio de canto, ``measure_block`` não encontraria as colunas no
    split, avisaria "não podem ser medidas" e mediria um bloco **vazio** -- delta zero por
    ausência de coluna, lido como ausência de sinal.
    """
    assert block_columns(BLOCK_REGISTRY["divisao-categoria"]) == [
        "weight_class_lbs",
        "is_womens_division",
    ]
    assert block_columns(BLOCK_REGISTRY["divisao-taxa-base"]) == ["division_finish_rate_prior"]


def test_registro_de_blocos_contem_os_dois_blocos_semeados() -> None:
    """O registro nasce com os dois blocos já existentes no modelo (M5)."""
    assert "striking-profile" in BLOCK_REGISTRY
    assert "dinamica-por-round" in BLOCK_REGISTRY


def test_registro_do_perfil_de_striking_resolve_dezoito_colunas() -> None:
    """Seis bases as-of viram o trio ``_a``/``_b``/``_diff`` -- 18 colunas, ordem estável."""
    colunas = block_columns(BLOCK_REGISTRY["striking-profile"])

    assert colunas == [
        "share_head_r3_a",
        "share_head_r3_b",
        "share_head_r3_diff",
        "share_body_r3_a",
        "share_body_r3_b",
        "share_body_r3_diff",
        "share_leg_r3_a",
        "share_leg_r3_b",
        "share_leg_r3_diff",
        "share_distance_r3_a",
        "share_distance_r3_b",
        "share_distance_r3_diff",
        "share_clinch_r3_a",
        "share_clinch_r3_b",
        "share_clinch_r3_diff",
        "share_ground_r3_a",
        "share_ground_r3_b",
        "share_ground_r3_diff",
    ]


def test_registro_da_dinamica_por_round_resolve_tres_colunas() -> None:
    """Uma base as-of vira exatamente o trio de canto -- 3 colunas."""
    colunas = block_columns(BLOCK_REGISTRY["dinamica-por-round"])

    assert colunas == [
        "round1_sig_strike_share_r3_a",
        "round1_sig_strike_share_r3_b",
        "round1_sig_strike_share_r3_diff",
    ]


def test_block_columns_emite_feature_bout_level_como_coluna_unica() -> None:
    """Feature bout-level entra sem sufixo de canto e sem ``_diff`` degenerado.

    É o caminho que a Slice 02 da SPEC 009 vai consumir: uma coluna de contexto de luta
    (divisão, título, rounds agendados) vale igual para os dois cantos, então virar par
    ``_a``/``_b`` produziria diferencial sempre zero -- ruído puro.
    """
    bloco = FeatureBlock(
        name="ad-hoc",
        asof_bases=("alpha",),
        bout_level_features=("beta",),
    )

    assert block_columns(bloco) == ["alpha_a", "alpha_b", "alpha_diff", "beta"]


def test_bootstrap_com_vetores_identicos_da_delta_zero_e_ic_degenerado() -> None:
    """Sem diferença entre os modelos, o delta e o IC inteiro colapsam em zero.

    É o caso da CA-02 levado ao bootstrap: se as duas probabilidades são as mesmas, a
    diferença por linha é zero em toda linha, e nenhuma reamostragem consegue produzir
    outro valor. Um IC não-degenerado aqui denunciaria pareamento quebrado -- reamostrar
    linhas diferentes para cada modelo.
    """
    y_true, prob, _ = _holdout_sintetico()

    resultado = paired_bootstrap_log_loss_delta(y_true, prob, prob, n_resamples=B_DE_TESTE)

    assert resultado.delta == 0.0
    assert resultado.ci_low == 0.0
    assert resultado.ci_high == 0.0


def test_bootstrap_com_modelo_estritamente_melhor_da_ic_inteiro_abaixo_de_zero() -> None:
    """Modelo informado contra desinformado: todo o intervalo fica do lado da melhora.

    ``ci_high < 0`` é exatamente o veredito pré-comprometido da RF-07.
    """
    y_true, prob_bom, prob_ruim = _holdout_sintetico()

    resultado = paired_bootstrap_log_loss_delta(y_true, prob_bom, prob_ruim, n_resamples=B_DE_TESTE)

    assert resultado.delta < 0.0
    assert resultado.ci_high < 0.0
    assert resultado.ci_low < resultado.ci_high


def test_bootstrap_e_deterministico_por_semente() -> None:
    """Mesma semente reproduz o IC; semente diferente move o IC (RF-11).

    A segunda metade é o que impede um falso determinismo: um IC que não muda com a
    semente estaria ignorando o RNG, e a reprodutibilidade seria acidental.
    """
    y_true, prob_bom, prob_ruim = _holdout_sintetico()

    primeira = paired_bootstrap_log_loss_delta(
        y_true, prob_bom, prob_ruim, n_resamples=B_DE_TESTE, seed=7
    )
    segunda = paired_bootstrap_log_loss_delta(
        y_true, prob_bom, prob_ruim, n_resamples=B_DE_TESTE, seed=7
    )
    outra_semente = paired_bootstrap_log_loss_delta(
        y_true, prob_bom, prob_ruim, n_resamples=B_DE_TESTE, seed=99
    )

    assert (primeira.delta, primeira.ci_low, primeira.ci_high) == (
        segunda.delta,
        segunda.ci_low,
        segunda.ci_high,
    )
    assert (outra_semente.ci_low, outra_semente.ci_high) != (primeira.ci_low, primeira.ci_high)


def test_bootstrap_delta_pontual_coincide_com_log_loss_do_sklearn() -> None:
    """O delta pontual é o delta de ``sklearn.metrics.log_loss`` -- a amarra da slice.

    Este é o teste que impede o harness de validar a si mesmo: sem ele, uma perda por linha
    calculada errado produziria um IC plausível e ninguém perceberia. Amarrando o delta
    pontual à métrica canônica, um erro de fórmula falha aqui, alto.
    """
    y_true, prob_bom, prob_ruim = _holdout_sintetico()

    resultado = paired_bootstrap_log_loss_delta(y_true, prob_bom, prob_ruim, n_resamples=B_DE_TESTE)

    esperado = float(log_loss(y_true, prob_bom, labels=[0, 1])) - float(
        log_loss(y_true, prob_ruim, labels=[0, 1])
    )
    assert resultado.delta == pytest.approx(esperado, rel=1e-9)
    assert math.isfinite(resultado.delta)


def _synthetic_split(
    *,
    n: int = 300,
    test_fraction: float = 0.2,
    nulos_em_alpha_a: int = 0,
) -> TemporalSplit:
    """``TemporalSplit`` sintético com um "bloco" (``alpha_*``) e uma base (``beta_diff``).

    O alvo correlaciona com as duas colunas, imperfeitamente, para que os dois modelos
    tenham o que aprender sem virarem preditores perfeitos -- métrica perfeita não mediria
    nada. ``nulos_em_alpha_a`` planta ausência em ``alpha_a``, **distribuída uniformemente**
    pela janela, para exercitar a medição de cobertura (RF-09). A distribuição é
    deliberada: concentrar os nulos no início deixaria a coluna 100% nula dentro da fatia
    de treino, e o ``HistGradientBoostingClassifier`` levantaria no binning -- caso real,
    porém tratado por ``restrict_to_trainable_columns`` na pipeline, não pelo harness. Uma
    feature de baixa cobertura de verdade é esparsa ao longo da janela, não ausente do
    treino.

    Datas: uma luta por dia a partir de 2015-01-01, ordenadas -- o holdout é a cauda, como
    no split temporal real.
    """
    inicio = date(2015, 1, 1)
    datas = [inicio + timedelta(days=indice) for indice in range(n)]
    alvo = [1 if indice % 3 != 0 else 0 for indice in range(n)]
    alpha_a = [0.2 + (indice % 5) / 10.0 for indice in range(n)]
    alpha_b = [0.9 - (indice % 4) / 10.0 for indice in range(n)]
    beta_diff = [
        float(rotulo) * 2.0 - 1.0 + (indice % 7) / 7.0 for indice, rotulo in enumerate(alvo)
    ]

    features = pd.DataFrame(
        {
            "alpha_a": alpha_a,
            "alpha_b": alpha_b,
            "alpha_diff": [a - b for a, b in zip(alpha_a, alpha_b, strict=True)],
            "beta_diff": beta_diff,
        }
    )
    n_nulos = min(nulos_em_alpha_a, n)
    n_preenchidas = n - n_nulos
    preenchidas = {round(passo * n / n_preenchidas) for passo in range(n_preenchidas)}
    for posicao in range(n):
        if posicao not in preenchidas:
            features.loc[posicao, "alpha_a"] = float("nan")

    target = pd.Series(alvo, dtype="int64")
    event_date = pd.Series(datas)
    n_test = max(1, round(n * test_fraction))
    n_train = n - n_test
    event_train = event_date.iloc[:n_train]
    return TemporalSplit(
        x_train=features.iloc[:n_train],
        x_test=features.iloc[n_train:],
        y_train=target.iloc[:n_train],
        y_test=target.iloc[n_train:],
        event_train=event_train,
        event_test=event_date.iloc[n_train:],
        boundary_date=event_train.max(),
    )


_BLOCO_ALPHA = FeatureBlock(name="alpha", asof_bases=("alpha",))
_BLOCO_INEXISTENTE = FeatureBlock(name="fantasma", asof_bases=("nao_existe",))


def test_measure_block_de_bloco_inexistente_da_delta_exatamente_zero() -> None:
    """Bloco cujas colunas não estão no split: os dois modelos são o mesmo modelo (CA-02).

    É a prova mais forte de que dataset, split e ``random_state`` estão de fato fixos: se
    remover nada mudasse qualquer coisa, o delta não seria exatamente zero, e todo delta
    medido pelo harness carregaria esse ruído embutido.
    """
    split = _synthetic_split()

    medicao = measure_block(split, _BLOCO_INEXISTENTE, n_resamples=B_DE_TESTE)

    assert medicao.delta == 0.0
    assert medicao.bootstrap.ci_low == 0.0
    assert medicao.bootstrap.ci_high == 0.0
    assert medicao.block_columns == []
    assert medicao.missing_columns == ["nao_existe_a", "nao_existe_b", "nao_existe_diff"]
    assert medicao.adopted is False


def test_measure_block_usa_o_mesmo_split_nos_dois_modelos() -> None:
    """A medição reporta o split de entrada intacto e o bloco resolvido dentro dele (CA-02).

    O modelo "sem" recebe exatamente as colunas do "com" menos as do bloco -- aqui, 4 menos
    3. A janela reportada é a do split inteiro (treino + holdout), não a de uma das fatias.
    """
    split = _synthetic_split()

    medicao = measure_block(split, _BLOCO_ALPHA, n_resamples=B_DE_TESTE)

    assert medicao.n_train == len(split.y_train)
    assert medicao.n_test == len(split.y_test)
    assert medicao.boundary_date == split.boundary_date
    assert medicao.window_start == split.event_train.min()
    assert medicao.window_end == split.event_test.max()
    assert medicao.block_columns == ["alpha_a", "alpha_b", "alpha_diff"]
    assert medicao.missing_columns == []
    assert medicao.n_features_with == len(split.x_train.columns)
    assert medicao.n_features_with - len(medicao.block_columns) == 1


def test_measure_block_e_deterministico() -> None:
    """Mesmo split, mesmo ``random_state`` e mesma semente: medição idêntica (RF-11)."""
    split = _synthetic_split()

    primeira = measure_block(split, _BLOCO_ALPHA, n_resamples=B_DE_TESTE)
    segunda = measure_block(split, _BLOCO_ALPHA, n_resamples=B_DE_TESTE)

    assert primeira.log_loss_with == segunda.log_loss_with
    assert primeira.log_loss_without == segunda.log_loss_without
    assert primeira.delta == segunda.delta
    assert primeira.bootstrap.ci_low == segunda.bootstrap.ci_low
    assert primeira.bootstrap.ci_high == segunda.bootstrap.ci_high


def test_cobertura_por_coluna_mede_a_janela_inteira() -> None:
    """A cobertura conta os não-nulos sobre treino + holdout, uma entrada por coluna (CA-04).

    A janela inteira e não só o treino: a pergunta da RF-09 é "esta feature existe no
    dado?", que independe de onde caiu a fronteira do split.
    """
    split = _synthetic_split(n=300, nulos_em_alpha_a=60)

    medicao = measure_block(split, _BLOCO_ALPHA, n_resamples=B_DE_TESTE)

    por_coluna = {item.column: item for item in medicao.coverage}
    assert [item.column for item in medicao.coverage] == ["alpha_a", "alpha_b", "alpha_diff"]
    assert por_coluna["alpha_a"].n_rows == 300
    assert por_coluna["alpha_a"].n_non_null == 240
    assert por_coluna["alpha_a"].fraction == pytest.approx(0.8)
    assert por_coluna["alpha_b"].n_non_null == 300
    assert por_coluna["alpha_b"].fraction == pytest.approx(1.0)


def test_cobertura_baixa_aparece_destacada_no_log(caplog: pytest.LogCaptureFixture) -> None:
    """Coluna a 20% de preenchimento vira ``WARNING`` nomeando a coluna e o limiar (CA-04)."""
    split = _synthetic_split(n=300, nulos_em_alpha_a=240)

    with caplog.at_level(logging.WARNING, logger="analysis.ablation"):
        medicao = measure_block(split, _BLOCO_ALPHA, n_resamples=B_DE_TESTE)

    por_coluna = {item.column: item for item in medicao.coverage}
    assert por_coluna["alpha_a"].fraction == pytest.approx(0.2)
    assert por_coluna["alpha_a"].is_low is True
    assert por_coluna["alpha_b"].is_low is False
    avisos = [registro.getMessage() for registro in caplog.records]
    assert any("alpha_a" in aviso and "30" in aviso for aviso in avisos)
    assert not any("alpha_b" in aviso for aviso in avisos)


def _medicao_com_ic(ci_low: float, ci_high: float, delta: float = -0.001) -> BlockMeasurement:
    """Medição sintética com um IC escolhido, para exercitar o veredito sem treinar nada."""
    return BlockMeasurement(
        block="bloco-de-teste",
        measured_at=datetime(2026, 9, 2, 13, 40, 11, tzinfo=UTC),
        window_start=date(2010, 1, 2),
        window_end=date(2025, 9, 6),
        boundary_date=date(2022, 6, 4),
        n_train=5940,
        n_test=1485,
        random_state=0,
        n_features_with=40,
        block_columns=["alpha_a", "alpha_b", "alpha_diff"],
        missing_columns=[],
        coverage=[ColumnCoverage(column="alpha_a", n_non_null=6234, n_rows=7425, std=0.1234)],
        log_loss_with=0.7543,
        log_loss_without=0.7637,
        bootstrap=BootstrapDelta(
            delta=delta, ci_low=ci_low, ci_high=ci_high, n_resamples=10_000, seed=0
        ),
    )


def test_registro_json_grava_o_payload_completo_com_cobertura_antes_do_delta(
    tmp_path: Path,
) -> None:
    """O registro é gravado como ``<bloco>.json`` com todo o contexto da medição (CA-03).

    A ordem das chaves não é estética: a cobertura vem **antes** das chaves de delta porque
    é assim que a RF-09 quer que o resultado seja lido -- coluna vazia explica delta nulo
    antes de ele ser interpretado como ausência de sinal.
    """
    medicao = _medicao_com_ic(ci_low=-0.0281, ci_high=0.0092)

    caminho = save_measurement(medicao, directory=tmp_path)

    assert caminho == tmp_path / "bloco-de-teste.json"
    payload = json.loads(caminho.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["block"] == "bloco-de-teste"
    assert payload["window_start"] == "2010-01-02"
    assert payload["window_end"] == "2025-09-06"
    assert payload["boundary_date"] == "2022-06-04"
    assert payload["n_train"] == 5940
    assert payload["n_test"] == 1485
    assert payload["random_state"] == 0
    assert payload["n_features_with"] == 40
    assert payload["block_columns"] == ["alpha_a", "alpha_b", "alpha_diff"]
    assert payload["missing_columns"] == []
    assert payload["coverage"] == [
        {
            "column": "alpha_a",
            "n_non_null": 6234,
            "n_rows": 7425,
            "fraction": pytest.approx(0.8396, abs=1e-4),
            "std": pytest.approx(0.1234),
        }
    ]
    assert payload["log_loss_with"] == pytest.approx(0.7543)
    assert payload["log_loss_without"] == pytest.approx(0.7637)
    assert payload["delta"] == pytest.approx(-0.001)
    assert payload["ci_low"] == pytest.approx(-0.0281)
    assert payload["ci_high"] == pytest.approx(0.0092)
    assert payload["n_resamples"] == 10_000
    assert payload["seed"] == 0
    assert payload["adopted"] is False

    chaves = list(payload)
    assert chaves.index("coverage") < chaves.index("delta")
    assert chaves.index("coverage") < chaves.index("ci_low")


def test_registro_json_de_bloco_que_nao_paga_grava_adopted_false_sem_levantar(
    tmp_path: Path,
) -> None:
    """IC cruzando zero é desfecho de **sucesso** do slice, não falha (RF-07).

    Nada levanta, nada sinaliza erro: o registro do que não pagou é entregável da SPEC.
    """
    cruza_zero = _medicao_com_ic(ci_low=-0.0281, ci_high=0.0092)
    paga = _medicao_com_ic(ci_low=-0.0400, ci_high=-0.0100, delta=-0.025)

    assert cruza_zero.adopted is False
    assert paga.adopted is True

    caminho = save_measurement(cruza_zero, directory=tmp_path)

    assert json.loads(caminho.read_text(encoding="utf-8"))["adopted"] is False


def _posicao_da_mensagem(mensagens: list[str], trecho: str) -> int:
    """Índice da primeira mensagem que contém o trecho; -1 quando não há nenhuma."""
    for indice, mensagem in enumerate(mensagens):
        if trecho in mensagem:
            return indice
    return -1


@pytest.fixture
def cli_isolado(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Aponta o CLI para a sessão transacional do teste e para um diretório temporário.

    O ``SessionLocal`` do módulo é substituído por um contexto que devolve a mesma
    ``Session`` da fixture -- sem isso o CLI abriria conexão própria, fora da transação, e
    escaparia do rollback.
    """

    @contextmanager
    def _sessao() -> Iterator[Session]:
        yield db_session

    monkeypatch.setattr(ablation, "SessionLocal", _sessao)
    monkeypatch.setattr(ablation, "ABLATION_DIR", tmp_path)
    monkeypatch.setattr(ablation, "BOOTSTRAP_RESAMPLES", B_DE_TESTE)
    yield tmp_path


def test_cli_de_um_bloco_grava_o_registro_e_reporta_na_ordem_exigida(
    cli_isolado: Path,
    base_de_ablacao: BaseDeAblacao,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ordem do relatório é o CA-06: janela, cobertura, colunas, log-loss, delta, IC, veredito.

    A cobertura vem antes do delta de propósito (RF-09). A asserção é por **posição** das
    mensagens, não por igualdade de texto -- o que a slice promete é a ordem de leitura, não
    a redação.
    """
    with caplog.at_level(logging.INFO, logger="analysis.ablation"):
        ablation.main(["--block", "striking-profile"])

    assert (cli_isolado / "striking-profile.json").exists()
    mensagens = [registro.getMessage() for registro in caplog.records]
    posicoes = [
        _posicao_da_mensagem(mensagens, "Janela medida"),
        _posicao_da_mensagem(mensagens, "Cobertura de"),
        _posicao_da_mensagem(mensagens, "Colunas do bloco"),
        _posicao_da_mensagem(mensagens, "log-loss"),
        _posicao_da_mensagem(mensagens, "Delta"),
        _posicao_da_mensagem(mensagens, "IC 95%"),
        _posicao_da_mensagem(mensagens, "Veredito"),
    ]
    assert all(posicao >= 0 for posicao in posicoes), (posicoes, mensagens)
    assert posicoes == sorted(posicoes), (posicoes, mensagens)


def test_cli_com_all_produz_um_registro_por_bloco(
    cli_isolado: Path, base_de_ablacao: BaseDeAblacao
) -> None:
    """``--all`` mede todos os blocos registrados, um arquivo por bloco (CA-06).

    Desde a Slice 00B o conjunto inclui o bloco especial ``janela-canto-fabricado``, que não
    vive em ``BLOCK_REGISTRY`` porque compara duas **variantes de dataset** e não dois
    conjuntos de colunas -- mas é medição da mesma SPEC e ``--all`` não pode omiti-lo.
    """
    ablation.main(["--all"])

    gravados = {caminho.stem for caminho in cli_isolado.glob("*.json")}
    assert gravados == set(BLOCK_REGISTRY) | {WINDOW_BLOCK_NAME}


def test_cli_com_bloco_desconhecido_falha_pelo_argparse_listando_os_validos() -> None:
    """Nome inválido é erro de uso, tratado pelo argparse -- não exceção do harness."""
    with pytest.raises(SystemExit):
        ablation.main(["--block", "nao-existe"])


def test_cli_de_bloco_que_nao_paga_nao_sinaliza_falha(
    cli_isolado: Path, base_de_ablacao: BaseDeAblacao
) -> None:
    """Veredito negativo não levanta e não vira código de saída (RF-07, CA-03).

    ``main`` não levanta em veredito nenhum; o desfecho "não adotado" é registro, não erro,
    e o registro é gravado do mesmo jeito.
    """
    ablation.main(["--block", "dinamica-por-round"])

    payload = json.loads((cli_isolado / "dinamica-por-round.json").read_text(encoding="utf-8"))
    assert payload["adopted"] in (True, False)


_BASES_DO_PERFIL = (
    SHARE_HEAD_R3,
    SHARE_BODY_R3,
    SHARE_LEG_R3,
    SHARE_DISTANCE_R3,
    SHARE_CLINCH_R3,
    SHARE_GROUND_R3,
)
N_LUTAS_SEMEADAS = 120
PRIMEIRA_DATA_SEMEADA = date(2015, 1, 3)


@dataclass(frozen=True)
class BaseDeAblacao:
    """Contagens do granular antes da medição, para provar que o harness é read-only."""

    n_bout_features: int
    n_bouts: int
    n_events: int
    primeira_data: date
    ultima_data: date


def _features_semeadas(indice: int, vermelho_venceu: bool) -> dict[str, object]:
    """Payload de features de uma luta: as bases do M5 mais uma base fora dos blocos.

    ``reach_cm_diff`` carrega o sinal principal (fica fora dos dois blocos medidos); as
    bases do perfil de striking e da dinâmica por round carregam sinal fraco e ruidoso --
    é o retrato honesto do que o PRD mediu, não um bloco fabricado para "pagar".
    """
    payload: dict[str, object] = {
        "reach_cm_diff": 6.0 if vermelho_venceu else -6.0,
        "wins_prior_diff": float(indice % 7) - 3.0,
    }
    for posicao, base in enumerate(_BASES_DO_PERFIL):
        valor_a = 0.1 + ((indice + posicao) % 6) / 10.0
        valor_b = 0.1 + ((indice + posicao + 3) % 6) / 10.0
        payload[f"{base}_a"] = valor_a
        payload[f"{base}_b"] = valor_b
        payload[f"{base}_diff"] = valor_a - valor_b
    round_a = 0.2 + (indice % 5) / 10.0
    round_b = 0.2 + ((indice + 2) % 5) / 10.0
    payload[f"{ROUND1_SIG_STRIKE_SHARE_R3}_a"] = round_a
    payload[f"{ROUND1_SIG_STRIKE_SHARE_R3}_b"] = round_b
    payload[f"{ROUND1_SIG_STRIKE_SHARE_R3}_diff"] = round_a - round_b
    return payload


def _semeia_base_de_ablacao(session: Session) -> BaseDeAblacao:
    """Semeia lutas com as features dos dois blocos, todas posteriores à janela confiável.

    As datas começam em 2015 de propósito: lutas anteriores a
    ``FIRST_RELIABLE_CORNER_DATE`` seriam descartadas por ``build_dataset`` (ADR 0006) e a
    janela medida não bateria com a semeada.
    """
    fighters = [FighterFactory.build(name=f"Lutador ablação {indice}") for indice in range(12)]
    for fighter in fighters:
        session.add(fighter)
    session.flush()
    fighter_ids = [int(fighter.id) for fighter in fighters]

    for indice in range(N_LUTAS_SEMEADAS):
        event = EventFactory.build(
            name=f"UFC Ablação {indice}",
            date=PRIMEIRA_DATA_SEMEADA + timedelta(days=7 * indice),
        )
        session.add(event)
        session.flush()
        vermelho_venceu = indice % 5 != 0
        red_id = fighter_ids[indice % len(fighter_ids)]
        blue_id = fighter_ids[(indice + 1) % len(fighter_ids)]
        bout = BoutFactory.build(
            event_id=int(event.id), winner_id=red_id if vermelho_venceu else blue_id
        )
        session.add(bout)
        session.flush()
        session.add(BoutFighterFactory.build(bout_id=bout.id, fighter_id=red_id, corner=Corner.RED))
        session.add(
            BoutFighterFactory.build(bout_id=bout.id, fighter_id=blue_id, corner=Corner.BLUE)
        )
        session.add(
            BoutFeatures(
                bout_id=bout.id,
                features=_features_semeadas(indice, vermelho_venceu),
                target_winner_corner=Corner.RED if vermelho_venceu else Corner.BLUE,
                source="feature-engineering",
                generated_at=datetime.now(UTC),
            )
        )
    session.flush()
    return BaseDeAblacao(
        n_bout_features=_conta(session, BoutFeatures),
        n_bouts=_conta(session, Bout),
        n_events=_conta(session, Event),
        primeira_data=PRIMEIRA_DATA_SEMEADA,
        ultima_data=PRIMEIRA_DATA_SEMEADA + timedelta(days=7 * (N_LUTAS_SEMEADAS - 1)),
    )


def _conta(session: Session, model: type[BoutFeatures] | type[Bout] | type[Event]) -> int:
    """Contagem de linhas de uma tabela, para a asserção de harness read-only (CA-07)."""
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


@pytest.fixture
def base_de_ablacao(db_session: Session) -> BaseDeAblacao:
    """Base sintética persistida no Postgres de teste, com as features dos dois blocos."""
    return _semeia_base_de_ablacao(db_session)


def test_integracao_run_ablation_devolve_uma_medicao_por_bloco_com_a_janela_real(
    db_session: Session, base_de_ablacao: BaseDeAblacao
) -> None:
    """Sobre o Postgres, a medição sai com a janela derivada das datas realmente semeadas."""
    medicoes = ablation.run_ablation(
        db_session, [BLOCK_REGISTRY["striking-profile"]], n_resamples=B_DE_TESTE
    )

    assert len(medicoes) == 1
    medicao = medicoes[0]
    assert medicao.block == "striking-profile"
    assert medicao.window_start == base_de_ablacao.primeira_data
    assert medicao.window_end == base_de_ablacao.ultima_data
    assert medicao.n_train + medicao.n_test == N_LUTAS_SEMEADAS
    assert medicao.missing_columns == []
    assert len(medicao.block_columns) == 18


def test_integracao_run_ablation_nao_escreve_nada_no_banco(
    db_session: Session, base_de_ablacao: BaseDeAblacao
) -> None:
    """O harness é read-only: as contagens do granular não mudam (CA-07).

    Nenhuma slice desta SPEC grava; medir é ler. Um ``commit`` escondido aqui contaminaria
    o granular com efeito colateral de análise.
    """
    ablation.run_ablation(db_session, list(BLOCK_REGISTRY.values()), n_resamples=B_DE_TESTE)

    assert _conta(db_session, BoutFeatures) == base_de_ablacao.n_bout_features
    assert _conta(db_session, Bout) == base_de_ablacao.n_bouts
    assert _conta(db_session, Event) == base_de_ablacao.n_events


def test_integracao_dois_blocos_numa_chamada_compartilham_o_mesmo_split(
    db_session: Session, base_de_ablacao: BaseDeAblacao
) -> None:
    """Dataset e split são construídos uma vez para todos os blocos (RF-06).

    Se cada bloco reconstruísse o seu, os deltas deixariam de ser comparáveis entre si --
    a violação seria silenciosa, que é exatamente o modo de falha que a régua existe para
    impedir.
    """
    medicoes = ablation.run_ablation(
        db_session, list(BLOCK_REGISTRY.values()), n_resamples=B_DE_TESTE
    )

    assert len(medicoes) == len(BLOCK_REGISTRY)
    assert len({medicao.n_train for medicao in medicoes}) == 1
    assert len({medicao.n_test for medicao in medicoes}) == 1
    assert len({medicao.boundary_date for medicao in medicoes}) == 1
    assert len({medicao.n_features_with for medicao in medicoes}) == 1


def test_registro_do_bloco_formato_tem_as_cinco_colunas_do_bloco_b2() -> None:
    """CA-03/CA-04 da SPEC 009: o bloco ``formato`` resolve exatamente as 5 colunas do B2.

    Duas features bout-level (coluna única, sem sufixo de canto) mais uma base as-of por
    lutador, que o harness expande no trio ``_a``/``_b``/``_diff``. Os nomes vêm importados
    de ``matchup``/``rolling``, nunca redigitados: uma string desatualizada faria a coluna
    sumir do bloco em silêncio e o veredito passaria a valer para um bloco menor do que o
    medido diz ser.
    """
    bloco = BLOCK_REGISTRY["formato"]

    assert bloco.asof_bases == ("five_round_bouts_before",)
    assert bloco.bout_level_features == ("is_title_bout", "scheduled_rounds")
    assert block_columns(bloco) == [
        "five_round_bouts_before_a",
        "five_round_bouts_before_b",
        "five_round_bouts_before_diff",
        "is_title_bout",
        "scheduled_rounds",
    ]
    assert len(block_columns(bloco)) == 5


def test_registro_do_bloco_historico_qualificado_tem_as_oito_bases_do_bloco_c() -> None:
    """CA-11 da SPEC 009: ``historico-qualificado`` resolve exatamente as 24 colunas do C.

    Bloco **único** C1+C2+C3, como a SPEC manda: as três subpartes são a mesma tese
    (qualificar o histórico além de "quanto vence") e produzem um veredito só. A comparação é
    de conjunto contra a lista literal -- uma base esquecida faria o bloco ser medido
    incompleto e o veredito valeria para outra coisa que não o bloco C.

    Nenhuma feature bout-level: o bloco C é todo por lutador, 8 bases as-of que o harness
    expande no trio ``_a``/``_b``/``_diff``.
    """
    bloco = BLOCK_REGISTRY["historico-qualificado"]

    assert set(bloco.asof_bases) == {
        KO_LOSSES_PRIOR,
        SUBMISSION_LOSSES_PRIOR,
        CAREER_MINUTES_BEFORE,
        SIG_STRIKES_LANDED_PM_TREND,
        SIG_STRIKES_ABSORBED_PM_TREND,
        TAKEDOWNS_LANDED_AVG_TREND,
        TAKEDOWN_DEFENSE_TREND,
        CONTROL_TIME_AVG_TREND,
    }
    assert len(bloco.asof_bases) == 8
    assert bloco.bout_level_features == ()
    assert len(block_columns(bloco)) == 24
    assert set(block_columns(bloco)) == {
        f"{base}{sufixo}" for base in bloco.asof_bases for sufixo in ("_a", "_b", "_diff")
    }


def test_gemeas_de_carreira_do_bloco_c_nao_entram_no_registro_da_ablacao() -> None:
    """CA-11: o bloco C mede **tendência**, não "carreira + tendência".

    As gêmeas de carreira são intermediárias locais de ``rolling`` e não existem como coluna;
    esta guarda impede que uma slice futura as emita e as some ao bloco, o que dobraria as
    colunas medidas e mudaria em silêncio o que o veredito significa.
    """
    bloco = BLOCK_REGISTRY["historico-qualificado"]

    assert not [base for base in bloco.asof_bases if base.endswith("_carreira")]
    assert not [coluna for coluna in block_columns(bloco) if "_carreira" in coluna]


# Blocos D1 e D2 da SPEC 009 (Slice 05). Dois registros independentes de propósito: o
# confronto de bases e o vetor de estilo respondem perguntas diferentes e podem pagar um sem
# o outro -- medi-los juntos daria um veredito só, que não diria qual dos dois carregou o
# resultado, e a RF-06 exige bloco medido isoladamente.
def test_registro_dos_blocos_de_confronto_e_de_estilo_e_independente() -> None:
    """CA-11: ``confronto-de-bases`` e ``eixos-de-estilo`` são **dois** blocos, não um."""
    confronto = BLOCK_REGISTRY["confronto-de-bases"]
    estilo = BLOCK_REGISTRY["eixos-de-estilo"]

    assert confronto is not estilo
    assert set(confronto.asof_bases).isdisjoint(estilo.asof_bases)
    assert set(confronto.bout_level_features).isdisjoint(estilo.bout_level_features)


def test_bloco_confronto_de_bases_resolve_cinco_colunas() -> None:
    """CA-11: uma base as-of (trio de canto) mais duas bout-level (coluna única) -- 5 colunas.

    Os dois caminhos convivem no mesmo bloco: a contagem de canhotos enfrentados é do
    lutador, o confronto de bases é da luta. Se o registro pedisse o trio de canto para as
    bout-level, ``measure_block`` não as acharia no split e mediria um bloco menor -- delta
    quase zero por ausência de coluna, lido como ausência de sinal.
    """
    bloco = BLOCK_REGISTRY["confronto-de-bases"]

    assert block_columns(bloco) == [
        "southpaw_opponents_faced_prior_a",
        "southpaw_opponents_faced_prior_b",
        "southpaw_opponents_faced_prior_diff",
        "is_open_stance_matchup",
        "involves_switch_stance",
    ]


def test_bloco_eixos_de_estilo_resolve_sete_colunas() -> None:
    """CA-11: duas bases as-of (dois trios) mais a distância bout-level -- 7 colunas."""
    bloco = BLOCK_REGISTRY["eixos-de-estilo"]

    assert block_columns(bloco) == [
        "grappling_axis_r3_a",
        "grappling_axis_r3_b",
        "grappling_axis_r3_diff",
        "volume_axis_r3_a",
        "volume_axis_r3_b",
        "volume_axis_r3_diff",
        "style_distance",
    ]


def test_cobertura_por_coluna_carrega_o_desvio_padrao_observado() -> None:
    """CA-11 (RF-09): a cobertura reporta a dispersão da coluna na janela medida.

    Uniforme para **todo** bloco, sem caso especial: para ``style_distance`` este é o número
    que afere a sanidade de ``STYLE_KERNEL_SIGMA`` do bloco D3, mas um gancho de aferição com
    um único uso seria menos simples e menos honesto que um campo a mais no relatório que já
    existe. O valor é **reportado, nunca usado para reajustar σ nem coisa alguma** -- tuning é
    não-objetivo declarado do PRD.
    """
    split = _synthetic_split(n=300, nulos_em_alpha_a=60)
    janela = pd.concat([split.x_train, split.x_test])

    medicao = measure_block(split, _BLOCO_ALPHA, n_resamples=B_DE_TESTE)

    por_coluna = {item.column: item for item in medicao.coverage}
    for coluna, item in por_coluna.items():
        assert item.std is not None, coluna
        assert item.std == pytest.approx(float(janela[coluna].std()))


def test_desvio_padrao_de_coluna_sem_dispersao_medivel_e_nulo() -> None:
    """RF-03 no relatório: menos de dois valores conhecidos não tem desvio-padrão.

    ``None``, e não ``0.0``: zero afirmaria "a coluna é constante", que é uma leitura
    diferente de "não dá para dizer". A distinção importa porque este campo é lido como
    aferição de sanidade, e uma aferição que inventa número não afere nada.
    """
    frame = pd.DataFrame({"unica": [0.7], "vazia": [float("nan")]})

    cobertura = {item.column: item for item in ablation.column_coverage(frame, ["unica", "vazia"])}

    assert cobertura["unica"].std is None
    assert cobertura["vazia"].std is None


def test_desvio_padrao_aparece_no_log_da_cobertura_antes_do_delta(
    cli_isolado: Path,
    base_de_ablacao: BaseDeAblacao,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CA-11: o desvio-padrão sai junto da cobertura, e a cobertura vem antes do delta.

    A posição é o contrato de leitura: quem abre o log encontra dispersão e preenchimento da
    coluna antes de encontrar o número que eles explicam.
    """
    with caplog.at_level(logging.INFO, logger="analysis.ablation"):
        ablation.main(["--block", "striking-profile"])

    mensagens = [registro.getMessage() for registro in caplog.records]
    posicao_desvio = _posicao_da_mensagem(mensagens, "desvio-padrão")
    assert posicao_desvio >= 0, mensagens
    assert posicao_desvio == _posicao_da_mensagem(mensagens, "Cobertura de")
    assert posicao_desvio < _posicao_da_mensagem(mensagens, "Delta")


# Blocos D3 e D4 da SPEC 009 (Slice 06). Dois registros independentes de propósito: "como ele
# foi contra gente parecida com este adversário" e "contra quem ele venceu" são perguntas
# diferentes e podem pagar uma sem a outra -- medi-las juntas daria um veredito só, que não
# diria qual das duas carregou o resultado, e a RF-06 exige bloco medido isoladamente.
def test_registro_dos_blocos_de_estilo_semelhante_e_de_calendario_e_independente() -> None:
    """CA-03/CA-04: ``estilo-semelhante`` e ``forca-de-calendario`` são **dois** blocos."""
    estilo = BLOCK_REGISTRY["estilo-semelhante"]
    calendario = BLOCK_REGISTRY["forca-de-calendario"]

    assert estilo is not calendario
    assert set(estilo.asof_bases).isdisjoint(calendario.asof_bases)
    assert not estilo.bout_level_features
    assert not calendario.bout_level_features


def test_bloco_estilo_semelhante_resolve_tres_colunas() -> None:
    """CA-03: uma base as-of por lutador vira o trio ``_a``/``_b``/``_diff`` -- 3 colunas.

    O D3 é **do lutador**, não da luta: mede o histórico dele contra estilos parecidos com o
    do adversário desta luta, e cada canto tem o seu. Registrá-lo como bout-level faria
    ``measure_block`` procurar uma coluna que não existe e medir um bloco vazio -- delta zero
    por ausência de coluna, lido como ausência de sinal.
    """
    assert block_columns(BLOCK_REGISTRY["estilo-semelhante"]) == [
        "similar_style_win_rate_prior_a",
        "similar_style_win_rate_prior_b",
        "similar_style_win_rate_prior_diff",
    ]


def test_bloco_forca_de_calendario_resolve_tres_colunas() -> None:
    """CA-04: idem para o D4 -- o cartel médio dos adversários anteriores é de cada canto."""
    assert block_columns(BLOCK_REGISTRY["forca-de-calendario"]) == [
        "opponent_win_rate_prior_avg_a",
        "opponent_win_rate_prior_avg_b",
        "opponent_win_rate_prior_avg_diff",
    ]


def test_registro_dos_blocos_d3_d4_usa_as_constantes_de_origem() -> None:
    """As bases vêm importadas de ``rolling``, jamais redigitadas como string literal.

    Uma string duplicada e desatualizada faria a feature sumir do bloco em silêncio, e o
    veredito passaria a valer para um bloco menor do que o medido diz ser -- a mesma classe de
    defeito que escondeu 21 features do M5 por meses.
    """
    assert BLOCK_REGISTRY["estilo-semelhante"].asof_bases == (SIMILAR_STYLE_WIN_RATE_PRIOR,)
    assert BLOCK_REGISTRY["forca-de-calendario"].asof_bases == (OPPONENT_WIN_RATE_PRIOR_AVG,)
