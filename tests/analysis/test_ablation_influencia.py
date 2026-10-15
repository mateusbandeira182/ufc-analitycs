"""Testes da ablação **local** por bloco no serving (SPEC 011, Slice 05 -- RF-17/RF-18).

Grandeza diferente da do resto de ``analysis.ablation``: lá se mede, com bootstrap pareado
sobre um holdout, se um bloco de features **melhora o modelo**; aqui se mede quanto um bloco
move a probabilidade **deste confronto**, neutralizando as colunas dele com ``NaN`` e
re-predizendo. Uma é estatística sobre o dataset, a outra é explicabilidade de um par.

Os três contratos sob prova:

1. **11 entradas, sempre** -- uma por chave de ``BLOCK_REGISTRY``, com ``janela-canto-fabricado``
   de fora: ele não é conjunto de colunas, é comparação de duas variantes de dataset.
2. **Bloco sem cobertura devolve ``None``, jamais ``0.0``** -- "não avaliado" e "não influenciou"
   são afirmações diferentes, e a segunda diz que a divisão não importa quando ela nem foi medida.
3. **O sinal de D-11** -- ``delta_prob_a = prob_a_base - prob_a_ablado``, positivo favorece A.

Estratégia: **dublê determinístico de modelo**, não modelo treinado. Um classificador real
daria direção, não valor; o que a CA-25 trava é a aritmética do sinal, e para isso o valor
precisa ser conhecido de antemão. Nenhum teste aqui toca o banco.
"""

from __future__ import annotations

from typing import ClassVar, cast

import pandas as pd
import pytest
from sklearn.ensemble import HistGradientBoostingClassifier

from analysis.ablation import BLOCK_REGISTRY, WINDOW_BLOCK_NAME, block_influences
from analysis.model import LoadedModel
from analysis.predict import ServedMatchup, neutral_prob_a_wins, prob_a_wins_of
from ingestion.features.rolling import ROUND1_SIG_STRIKE_SHARE_R3

# O bloco que o dublê enxerga: ``dinamica-por-round`` tem UMA base as-of, logo exatamente o
# trio ``_a``/``_b``/``_diff``. Bloco pequeno de propósito -- a aritmética do sinal fica
# verificável à mão.
_BLOCO_MEDIDO = "dinamica-por-round"
_COLUNAS = [f"{ROUND1_SIG_STRIKE_SHARE_R3}{sufixo}" for sufixo in ("_a", "_b", "_diff")]
_COLUNA_DIFF = _COLUNAS[-1]

# As probabilidades que o dublê devolve. Escolhidas para que a base neutra saia 0,9 e a
# ablacionada 0,5, dando um delta de exatamente +0,4.
_PROB_A_FAVORITO = 0.9
_PROB_A_AZARAO = 0.1
_PROB_SEM_SINAL = 0.5
_DELTA_ESPERADO = 0.4


class _StubModel:
    """Modelo determinístico: a probabilidade é função conhecida de UMA coluna.

    Existe para fixar o **sinal** de D-11 com valor conhecido. Um classificador treinado daria
    direção, não valor -- e o que a CA-25 trava é a aritmética do sinal, não o aprendizado.

    A regra é deliberadamente trivial: ``_diff > 0`` favorece o canto vermelho (0,9),
    ``_diff < 0`` favorece o azul (0,1) e ``NaN`` -- a coluna neutralizada -- não informa nada
    (0,5). Assim, ablar o bloco apaga **todo** o sinal disponível, e o deslocamento medido é o
    valor inteiro, não uma fração a estimar.
    """

    # Ordem invertida de propósito: ``prob_a_wins_of`` deve ler a classe positiva pelo índice
    # de ``classes_``, nunca pela coluna 1 presumida. Com ``[1, 0]``, a coluna do vermelho é a
    # 0 -- um leitor que assumisse a 1 devolveria o complemento e o sinal inverteria.
    classes_: ClassVar[list[int]] = [1, 0]

    def predict_proba(self, features: pd.DataFrame) -> list[list[float]]:
        """``[[p_red, p_blue], ...]`` por linha, na ordem de ``classes_`` (red primeiro)."""
        probabilidades: list[list[float]] = []
        for diferencial in features[_COLUNA_DIFF]:
            if pd.isna(diferencial):
                p_red = _PROB_SEM_SINAL
            elif diferencial > 0:
                p_red = _PROB_A_FAVORITO
            else:
                p_red = _PROB_A_AZARAO
            probabilidades.append([p_red, 1.0 - p_red])
        return probabilidades


def _served() -> ServedMatchup:
    """Confronto servido pelo dublê: ida com o diferencial a favor de A, volta espelhada.

    As duas linhas carregam o trio inteiro preenchido -- é o que faz o bloco medido sair com
    cobertura ``full``, e o que torna significativa a asserção de que os outros dez saem
    ``none``.
    """
    loaded = LoadedModel(
        # ``LoadedModel.model`` é tipado como ``HistGradientBoostingClassifier``; o dublê entra
        # por ``cast`` (nunca ``Any``) porque só o protocolo ``classes_``/``predict_proba`` é
        # exercitado, e treinar um estimador real aqui destruiria o valor conhecido.
        model=cast(HistGradientBoostingClassifier, _StubModel()),
        feature_names=list(_COLUNAS),
        trained_at="2026-10-14T00:00:00Z",
    )
    ida = pd.DataFrame([{_COLUNAS[0]: 1.0, _COLUNAS[1]: 0.0, _COLUNA_DIFF: 1.0}])
    volta = pd.DataFrame([{_COLUNAS[0]: 0.0, _COLUNAS[1]: 1.0, _COLUNA_DIFF: -1.0}])
    return ServedMatchup(loaded=loaded, forward=ida, reverse=volta)


def _base_neutra(served: ServedMatchup) -> float:
    """Probabilidade neutra de canto do palpite base -- o mesmo cálculo que o service faz."""
    (ida,) = prob_a_wins_of(served.loaded, served.forward)
    (volta,) = prob_a_wins_of(served.loaded, served.reverse)
    return neutral_prob_a_wins(ida, volta)


def test_base_neutra_do_duble_e_o_valor_conhecido() -> None:
    """Pré-condição dos demais: o dublê entrega base neutra de exatamente 0,9.

    Sem este teste, um erro na montagem do dublê (ordem de ``classes_``, espelho da volta)
    deslocaria o valor conhecido e as asserções de sinal passariam a medir outra coisa.
    """
    assert _base_neutra(_served()) == pytest.approx(_PROB_A_FAVORITO)


def test_block_influences_devolve_uma_entrada_por_bloco_do_registro() -> None:
    """CA-12: 11 entradas, uma por chave de ``BLOCK_REGISTRY``, na ordem do registro."""
    served = _served()

    influencias = block_influences(served, _base_neutra(served))

    assert len(influencias) == 11
    assert [i.name for i in influencias] == list(BLOCK_REGISTRY)


def test_block_influences_nao_inclui_o_bloco_da_janela() -> None:
    """DP-05d: ``janela-canto-fabricado`` fica fora -- não é conjunto de colunas.

    Ele compara duas **variantes de dataset** com holdouts de tamanhos diferentes, e por isso
    nem passa por ``measure_block``. A resposta tem 11 entradas, não 12.
    """
    served = _served()

    nomes = {i.name for i in block_influences(served, _base_neutra(served))}

    assert WINDOW_BLOCK_NAME not in nomes
    assert WINDOW_BLOCK_NAME not in BLOCK_REGISTRY


def test_delta_prob_a_e_positivo_quando_o_bloco_favorece_o_lado_a() -> None:
    """CA-25 / D-11: ``prob_a_base - prob_a_ablado``, positivo empurra para A.

    Valor conhecido: base neutra 0,9 (o diferencial favorece A nas duas ordens), ablacionada
    0,5 (o dublê não tem mais nada para ler). Delta = +0,4.

    O sinal é **deliberadamente oposto**, em semântica, ao de ``analysis.metrics.MetricsDelta``
    -- lá negativo é bom, porque a grandeza é perda. Harmonizar os dois inverteria a tela sem
    quebrar nenhum outro teste; é este número que trava o sentido.
    """
    served = _served()

    por_nome = {i.name: i for i in block_influences(served, _base_neutra(served))}

    medido = por_nome[_BLOCO_MEDIDO]
    assert medido.delta_prob_a is not None
    assert medido.delta_prob_a == pytest.approx(_DELTA_ESPERADO)
    assert medido.delta_prob_a > 0


def test_bloco_medido_declara_cobertura_cheia_pela_contagem_real() -> None:
    """RF-18: as três colunas do bloco estão no modelo e preenchidas -> ``full``."""
    served = _served()

    por_nome = {i.name: i for i in block_influences(served, _base_neutra(served))}

    medido = por_nome[_BLOCO_MEDIDO]
    assert medido.n_columns == len(_COLUNAS)
    assert medido.n_columns_evaluated == len(_COLUNAS)
    assert medido.coverage == "full"
    assert medido.description == BLOCK_REGISTRY[_BLOCO_MEDIDO].description


def test_bloco_sem_coluna_no_modelo_devolve_delta_nulo_nunca_zero() -> None:
    """CA-13: ``coverage == "none"`` -> ``delta_prob_a is None``, asserido com ``is``.

    O ``is None`` não é preciosismo: ``0.0 == None`` é falso, mas um teste escrito como
    ``== 0`` passaria para os dois valores se alguém trocasse o nulo por zero "para a tela não
    quebrar". É a confusão que a RF-18 existe para impedir -- zero afirma que a divisão não
    importa, e ninguém mediu isso.
    """
    served = _served()

    influencias = block_influences(served, _base_neutra(served))

    sem_cobertura = [i for i in influencias if i.coverage == "none"]
    # Guarda de não-vacuidade: o dublê só conhece as três colunas de um bloco, logo os outros
    # dez têm de sair sem cobertura. Sem esta asserção, o laço abaixo passaria vazio.
    assert len(sem_cobertura) == 10
    for influencia in sem_cobertura:
        assert influencia.delta_prob_a is None
        assert influencia.n_columns == 0
        assert influencia.n_columns_evaluated == 0
    # A recíproca, que fecha o contrato nos dois sentidos (DP-05c): delta nulo se e somente se
    # a cobertura é ``none``.
    for influencia in influencias:
        assert (influencia.delta_prob_a is None) == (influencia.coverage == "none")


def test_coverage_parcial_quando_so_parte_das_colunas_esta_preenchida() -> None:
    """DP-05b/DP-05c: coluna nula nas DUAS ordens não conta como avaliada.

    Uma coluna vale como avaliada quando é não-nula em **alguma** das duas linhas: as duas são
    o mesmo par espelhado, e ablar uma coluna preenchida de um lado só **move** a
    probabilidade. "Avaliada" é exatamente "poderia mover a agulha".
    """
    served = _served()
    # Apaga o ``_b`` nas duas ordens: sobram duas das três colunas com valor.
    sem_b = ServedMatchup(
        loaded=served.loaded,
        forward=served.forward.assign(**{_COLUNAS[1]: float("nan")}),
        reverse=served.reverse.assign(**{_COLUNAS[1]: float("nan")}),
    )

    por_nome = {i.name: i for i in block_influences(sem_b, _base_neutra(sem_b))}

    medido = por_nome[_BLOCO_MEDIDO]
    assert medido.n_columns == len(_COLUNAS)
    assert medido.n_columns_evaluated == len(_COLUNAS) - 1
    assert medido.coverage == "partial"
    # Parcial ainda é medido: o diferencial continua de pé, então há o que ablar.
    assert medido.delta_prob_a == pytest.approx(_DELTA_ESPERADO)


def test_coluna_preenchida_em_uma_ordem_so_conta_como_avaliada() -> None:
    """DP-05b: não-nula em **alguma** das duas linhas basta -- ``notna().any()``.

    É o caso real do confronto assimétrico: A tem o valor e B não, então ``x_a`` é não-nulo na
    ida e nulo na volta. Contar só o que está preenchido nas duas subestimaria a cobertura e
    rotularia como "não avaliado" um bloco que move a agulha.
    """
    served = _served()
    assimetrico = ServedMatchup(
        loaded=served.loaded,
        forward=served.forward,
        reverse=served.reverse.assign(**dict.fromkeys(_COLUNAS, float("nan"))),
    )

    por_nome = {i.name: i for i in block_influences(assimetrico, _base_neutra(assimetrico))}

    assert por_nome[_BLOCO_MEDIDO].n_columns_evaluated == len(_COLUNAS)
    assert por_nome[_BLOCO_MEDIDO].coverage == "full"


def test_ablacao_neutraliza_o_diferencial_junto_com_os_cantos() -> None:
    """O ``_diff`` **tem** de ser neutralizado: ele é calculado ANTES da ablação.

    O dublê lê **só** o ``_diff``. Se a ablação apagasse apenas ``_a``/``_b``, o diferencial
    sobreviveria, o modelo continuaria informado e o delta sairia ``0.0`` -- um bloco com
    cobertura cheia rotulado "não influenciou". A tela sairia plausível e inteiramente falsa.
    """
    served = _served()

    por_nome = {i.name: i for i in block_influences(served, _base_neutra(served))}

    # Delta não-nulo é a prova de que o ``_diff`` foi apagado: era a única coluna que o dublê
    # consulta, e com ela de pé a ablação não teria movido nada.
    assert por_nome[_BLOCO_MEDIDO].delta_prob_a != pytest.approx(0.0)


def test_ablacao_nao_muta_as_linhas_servidas() -> None:
    """A ablação trabalha sobre **cópia**: as linhas servidas sobrevivem intactas.

    Elas são o insumo compartilhado dos 11 blocos. Mutá-las faria o segundo bloco medido
    herdar a neutralização do primeiro, e a influência de cada um dependeria da ordem de
    iteração -- erro que só apareceria no bloco de baixo da tela.
    """
    served = _served()
    antes = served.forward.copy()

    block_influences(served, _base_neutra(served))

    pd.testing.assert_frame_equal(served.forward, antes)
