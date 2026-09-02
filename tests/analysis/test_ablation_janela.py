"""Testes do bloco ``janela-canto-fabricado`` (Slice 00B da SPEC 009 -- M8).

Este bloco não é um conjunto de colunas: é a comparação de duas **variantes de dataset** --
a histórica (janela de 2010-01-01, ADR 0006 antes da Emenda 1) e a corrigida (2010-03-21) --
para medir o efeito de remover do treino as 40 lutas de alvo fabricado que sobreviveram à
ADR original.

O risco técnico central da slice é o pareamento. Remover lutas do começo da série encolhe o
dataset e, como o holdout é **fração de linhas** e não âncora de data, a fronteira do split
se desloca: algumas lutas migram do teste para o treino e os dois braços deixam de
compartilhar as mesmas linhas. Um bootstrap "pareado" sobre linhas diferentes seria medição
inválida com aparência de rigor. Daí o pareamento explícito pela **interseção de
``bout_id``**, com asserção de alvo idêntico -- é o que os testes abaixo prendem.

Todos os testes são funções puras sobre DataFrame sintético, exceto os do CLI, que rodam
contra o Postgres de teste transacional (o CLI lê ``bout_features``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analysis import ablation
from analysis.ablation import (
    PREVIOUS_TRAIN_WINDOW_START,
    SCHEMA_VERSION,
    WINDOW_BLOCK_NAME,
    measure_window_correction,
    paired_holdout_index,
    save_window_measurement,
)
from analysis.dataset import FIRST_RELIABLE_CORNER_DATE
from apps.bouts.enums import Corner
from apps.bouts.models import Bout
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.events.models import Event
from apps.features.models import BoutFeatures
from apps.fighters.tests.factories import FighterFactory

# Bootstrap enxuto nos testes: a corretude do estimador não depende de B (a Sprint 00 já o
# prendeu ao ``log_loss`` do sklearn); 10.000 reamostragens por teste custariam segundos sem
# provar nada a mais.
B_DE_TESTE = 500

# Lutas de alvo fabricado da frame sintética: o defeito real em miniatura -- todas dentro de
# [2010-01-01, 2010-03-21) e todas com o vermelho vencendo.
N_FABRICADAS = 8
PRIMEIRA_FABRICADA = date(2010, 1, 5)

# Lutas confiáveis, a partir do próprio dia do corte corrigido.
N_CONFIAVEIS = 240
PRIMEIRA_CONFIAVEL = FIRST_RELIABLE_CORNER_DATE


def _linha_crua(
    *,
    bout_id: int,
    event_date: date,
    target: str | None,
    features: dict[str, object],
) -> dict[str, object]:
    """Monta uma linha crua no formato devolvido por ``read_bout_features``."""
    return {
        "bout_id": bout_id,
        "event_date": event_date,
        "target_winner_corner": target,
        "features": features,
    }


def _features(indice: int, vermelho_venceu: bool) -> dict[str, object]:
    """Uma feature com sinal real e uma de ruído, ambas variando linha a linha.

    A heterogeneidade é deliberada: com confiança uniforme a perda por linha seria constante,
    a diferença entre os braços não teria variância e o IC degeneraria num ponto -- correto,
    mas incapaz de exercitar a reamostragem.
    """
    return {
        "reach_cm_diff": (6.0 if vermelho_venceu else -6.0) + float(indice % 5) - 2.0,
        "wins_prior_diff": float(indice % 7) - 3.0,
    }


def _frame_sintetica(*, com_fabricadas: bool = True) -> pd.DataFrame:
    """Série de 2009 a 2020: pré-janela, o intervalo fabricado e as lutas confiáveis.

    As de 2009 saem nos **dois** braços (nem a janela histórica as aceitava) e existem para
    provar que o braço "antes" reproduz a variante histórica, não a ausência de filtro.
    """
    linhas: list[dict[str, object]] = []
    bout_id = 1
    for deslocamento in range(3):
        linhas.append(
            _linha_crua(
                bout_id=bout_id,
                event_date=date(2009, 9, 1) + timedelta(days=30 * deslocamento),
                target="red",
                features=_features(bout_id, vermelho_venceu=True),
            )
        )
        bout_id += 1
    if com_fabricadas:
        for deslocamento in range(N_FABRICADAS):
            linhas.append(
                _linha_crua(
                    bout_id=bout_id,
                    event_date=PRIMEIRA_FABRICADA + timedelta(days=7 * deslocamento),
                    target="red",
                    features=_features(bout_id, vermelho_venceu=True),
                )
            )
            bout_id += 1
    for deslocamento in range(N_CONFIAVEIS):
        vermelho_venceu = deslocamento % 3 != 0
        linhas.append(
            _linha_crua(
                bout_id=bout_id,
                event_date=PRIMEIRA_CONFIAVEL + timedelta(days=7 * deslocamento),
                target="red" if vermelho_venceu else "blue",
                features=_features(bout_id, vermelho_venceu),
            )
        )
        bout_id += 1
    return pd.DataFrame(linhas)


def _medicao(frame: pd.DataFrame | None = None) -> ablation.WindowCorrectionMeasurement:
    """Medição sobre a frame sintética, com o B enxuto dos testes."""
    return measure_window_correction(
        _frame_sintetica() if frame is None else frame,
        n_resamples=B_DE_TESTE,
    )


def test_janela_medida_vai_do_corte_historico_ao_corrigido() -> None:
    """O bloco compara 2010-01-01 (histórica) contra 2010-03-21 (corrigida)."""
    medicao = _medicao()

    assert medicao.window_before == PREVIOUS_TRAIN_WINDOW_START == date(2010, 1, 1)
    assert medicao.window_after == FIRST_RELIABLE_CORNER_DATE == date(2010, 3, 21)


def test_lutas_do_intervalo_removido_entram_no_registro_com_a_taxa_do_vermelho() -> None:
    """CA-04: as lutas de ``[2010-01-01, 2010-03-21)`` são nomeadas, contadas e qualificadas.

    A taxa de vitória do vermelho nelas é o que sustenta o diagnóstico de alvo fabricado --
    no banco de produção ela é 40/40 (100%); aqui, na miniatura, 8/8.
    """
    embaralhada = _frame_sintetica().sample(frac=1.0, random_state=7).reset_index(drop=True)
    medicao = _medicao(embaralhada)

    # Ordenado, e não na ordem em que as linhas chegaram: ``read_bout_features`` não tem
    # ``ORDER BY``, então uma listagem na ordem da frame varia entre execuções e o registro
    # em disco deixaria de ser reproduzível (RF-11).
    assert medicao.removed_bout_ids == list(range(4, 4 + N_FABRICADAS))
    assert medicao.n_removed == N_FABRICADAS
    assert medicao.red_win_rate_removed == 1.0
    assert medicao.n_dataset_before - medicao.n_dataset_after == N_FABRICADAS


def test_lutas_anteriores_a_janela_historica_saem_dos_dois_bracos() -> None:
    """As lutas de 2009 não são "removidas" por esta correção -- a ADR 0006 já as tirara.

    O braço "antes" reproduz a variante **histórica** (janela de 2010-01-01), não a ausência
    de filtro. Confundir as duas coisas inflaria o efeito medido com 1.249 lutas que nenhum
    dos dois braços jamais treinou.
    """
    medicao = _medicao()

    assert medicao.n_dataset_before == N_FABRICADAS + N_CONFIAVEIS
    assert medicao.n_dataset_after == N_CONFIAVEIS


def test_holdout_pareado_e_a_intersecao_por_bout_id_dos_dois_holdouts() -> None:
    """CA-04: o pareamento usa só as lutas presentes nos **dois** holdouts.

    Remover lutas do começo da série empurra lutas do teste para o treino na variante
    corrigida (o holdout é fração de linhas, não âncora de data). Essas migradas ficam de
    fora do pareamento: no braço corrigido elas foram **treinadas**, e medir sobre elas
    compararia um modelo que as viu com um que não viu.
    """
    medicao = _medicao()

    assert medicao.n_test_before > medicao.n_test_after
    assert medicao.n_paired_holdout == medicao.n_test_after
    assert medicao.n_train_after > medicao.n_train_before - N_FABRICADAS


def test_indice_pareado_e_a_intersecao_e_preserva_a_ordem_do_braco_corrigido() -> None:
    """A função de pareamento é pura: interseção por ``bout_id``, ordem estável."""
    antes = pd.Series([1, 0, 1, 0], index=[10, 11, 12, 13])
    depois = pd.Series([0, 1, 0], index=[11, 12, 14])

    assert list(paired_holdout_index(antes, depois)) == [11, 12]


def test_alvo_divergente_entre_os_bracos_para_a_mesma_luta_falha_alto() -> None:
    """CA-04: alvo diferente para a mesma luta invalida a medição -- e ela para.

    Se os dois braços discordam do rótulo de uma mesma ``bout_id``, o pareamento não está
    comparando a mesma luta e o IC resultante seria rigor aparente sobre lixo.
    """
    antes = pd.Series([1, 0], index=[10, 11])
    depois = pd.Series([1, 1], index=[10, 11])

    with pytest.raises(ValueError, match="divergente"):
        paired_holdout_index(antes, depois)


def test_holdouts_sem_intersecao_falham_alto() -> None:
    """Sem luta em comum não há bootstrap pareado possível -- erro, não IC vazio."""
    antes = pd.Series([1, 0], index=[10, 11])
    depois = pd.Series([1, 0], index=[20, 21])

    with pytest.raises(ValueError, match="interseção"):
        paired_holdout_index(antes, depois)


def test_log_loss_dos_dois_bracos_e_medido_no_holdout_pareado() -> None:
    """O delta reportado é exatamente a diferença dos dois log-losses reportados.

    Reportar o log-loss de cada braço sobre o **seu** holdout (1.485 contra 1.477 linhas no
    banco real) daria dois números cuja diferença não é o delta -- e quem lesse o registro
    somaria maçã com laranja. Os dois são medidos no holdout pareado.
    """
    medicao = _medicao()

    assert medicao.log_loss_after - medicao.log_loss_before == pytest.approx(
        medicao.delta, abs=1e-12
    )


def test_medicao_e_deterministica_por_semente() -> None:
    """RF-11: duas execuções com a mesma semente produzem exatamente o mesmo IC."""
    primeira = _medicao()
    segunda = _medicao()

    assert (primeira.delta, primeira.ci_low, primeira.ci_high) == (
        segunda.delta,
        segunda.ci_low,
        segunda.ci_high,
    )


def test_frame_sem_luta_no_intervalo_produz_delta_zero_e_ic_degenerado() -> None:
    """CA-05: sem nada a remover os dois braços coincidem -- delta 0,0, sem exceção.

    O caminho não tem atalho ("se não há removidas, devolve zero"): os dois datasets são
    construídos e os dois modelos treinados de verdade. Um atalho tornaria este teste vazio,
    provando apenas que o atalho existe.
    """
    medicao = _medicao(_frame_sintetica(com_fabricadas=False))

    assert medicao.removed_bout_ids == []
    assert medicao.n_removed == 0
    assert medicao.red_win_rate_removed is None
    assert medicao.delta == 0.0
    assert (medicao.ci_low, medicao.ci_high) == (0.0, 0.0)
    assert medicao.adopted is False
    assert medicao.n_paired_holdout == medicao.n_test_after


def test_veredito_e_ci_high_menor_que_zero_e_nao_decide_a_adocao_da_janela() -> None:
    """CA-07: o veredito é o da RF-07 e nada mais.

    IC que cruza zero produz ``adopted: false`` sem exceção -- e **não** reverte a correção
    da janela. A adoção da janela é decisão de princípio (não se treina com rótulo que
    sabemos ser falso), fechada na SPEC 009 antes de qualquer número.
    """
    medicao = _medicao()

    assert medicao.adopted == (medicao.ci_high < 0.0)


def test_numero_de_features_e_registrado_nos_dois_bracos() -> None:
    """A fronteira do split muda, e com ela o conjunto de colunas treináveis pode mudar.

    Foi o que aconteceu na própria ADR 0006 (uma coluna deixou de ser toda-``NaN`` no
    treino). Registrar os dois números impede que a mudança passe despercebida e seja lida
    como efeito da janela.
    """
    medicao = _medicao()

    assert medicao.n_features_before > 0
    assert medicao.n_features_after > 0


def test_registro_json_grava_o_payload_completo(tmp_path: Path) -> None:
    """O registro tem a mesma forma dos demais blocos, mais o que é próprio desta medição."""
    medicao = _medicao()

    caminho = save_window_measurement(medicao, tmp_path)

    assert caminho == tmp_path / f"{WINDOW_BLOCK_NAME}.json"
    payload = json.loads(caminho.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["block"] == WINDOW_BLOCK_NAME
    assert payload["window_before"] == "2010-01-01"
    assert payload["window_after"] == "2010-03-21"
    assert payload["n_removed"] == N_FABRICADAS
    assert payload["removed_bout_ids"] == list(range(4, 4 + N_FABRICADAS))
    assert payload["red_win_rate_removed"] == 1.0
    assert payload["n_paired_holdout"] == medicao.n_test_after
    assert payload["adopted"] is medicao.adopted
    assert payload["delta"] == medicao.delta


# --- CLI, contra o Postgres de teste ------------------------------------------------------

N_LUTAS_CONFIAVEIS_SEMEADAS = 100


def _conta(session: Session, model: type[BoutFeatures] | type[Bout] | type[Event]) -> int:
    """Contagem de linhas de uma tabela, para a asserção de harness read-only."""
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _semeia(session: Session) -> None:
    """Semeia lutas fabricadas no intervalo removido e lutas confiáveis depois dele."""
    fighters = [FighterFactory.build(name=f"Lutador janela {indice}") for indice in range(12)]
    for fighter in fighters:
        session.add(fighter)
    session.flush()
    fighter_ids = [int(fighter.id) for fighter in fighters]

    datas = [PRIMEIRA_FABRICADA + timedelta(days=7 * indice) for indice in range(N_FABRICADAS)]
    datas += [
        PRIMEIRA_CONFIAVEL + timedelta(days=7 * indice)
        for indice in range(N_LUTAS_CONFIAVEIS_SEMEADAS)
    ]
    for indice, data_do_evento in enumerate(datas):
        fabricada = data_do_evento < FIRST_RELIABLE_CORNER_DATE
        vermelho_venceu = True if fabricada else indice % 3 != 0
        event = EventFactory.build(name=f"UFC Janela {indice}", date=data_do_evento)
        session.add(event)
        session.flush()
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
                features=_features(indice, vermelho_venceu),
                target_winner_corner=Corner.RED if vermelho_venceu else Corner.BLUE,
                source="feature-engineering",
                generated_at=datetime.now(UTC),
            )
        )
    session.flush()


@pytest.fixture
def base_semeada(db_session: Session) -> Session:
    """Base sintética persistida, com lutas dentro e fora do intervalo removido."""
    _semeia(db_session)
    return db_session


@pytest.fixture
def cli_isolado(
    base_semeada: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Path]:
    """CLI apontado para a sessão transacional do teste e para um diretório temporário."""

    @contextmanager
    def _sessao() -> Iterator[Session]:
        yield base_semeada

    monkeypatch.setattr(ablation, "SessionLocal", _sessao)
    monkeypatch.setattr(ablation, "ABLATION_DIR", tmp_path)
    monkeypatch.setattr(ablation, "BOOTSTRAP_RESAMPLES", B_DE_TESTE)
    yield tmp_path


def _posicao(mensagens: list[str], trecho: str) -> int:
    """Índice da primeira mensagem que contém o trecho; -1 se nenhuma contiver."""
    for posicao, mensagem in enumerate(mensagens):
        if trecho in mensagem:
            return posicao
    return -1


def test_cli_do_bloco_da_janela_grava_o_registro_e_reporta_na_ordem_exigida(
    cli_isolado: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-06: janela -> removidas -> tamanhos -> pareado -> log-loss -> delta -> IC -> veredito.

    A asserção é por **posição** das mensagens, não por igualdade de texto: o que a slice
    promete é a ordem de leitura, não a redação.
    """
    with caplog.at_level(logging.INFO, logger="analysis.ablation"):
        ablation.main(["--block", WINDOW_BLOCK_NAME])

    assert (cli_isolado / f"{WINDOW_BLOCK_NAME}.json").exists()
    mensagens = [registro.getMessage() for registro in caplog.records]
    posicoes = [
        _posicao(mensagens, "Janela do treino"),
        _posicao(mensagens, "Lutas removidas"),
        _posicao(mensagens, "Dataset"),
        _posicao(mensagens, "Holdout pareado"),
        _posicao(mensagens, "log-loss"),
        _posicao(mensagens, "Delta"),
        _posicao(mensagens, "IC 95%"),
        _posicao(mensagens, "Veredito"),
    ]
    assert all(posicao >= 0 for posicao in posicoes), (posicoes, mensagens)
    assert posicoes == sorted(posicoes), (posicoes, mensagens)


def test_cli_do_bloco_da_janela_nao_sinaliza_falha_com_ic_cruzando_zero(
    cli_isolado: Path,
) -> None:
    """CA-07: veredito negativo é registro, não erro -- nada levanta, nada sai diferente de 0."""
    ablation.main(["--block", WINDOW_BLOCK_NAME])

    payload = json.loads((cli_isolado / f"{WINDOW_BLOCK_NAME}.json").read_text(encoding="utf-8"))
    assert payload["adopted"] in (True, False)
    assert payload["n_removed"] == N_FABRICADAS
    assert payload["red_win_rate_removed"] == 1.0


def test_cli_com_all_inclui_o_bloco_da_janela(cli_isolado: Path) -> None:
    """``--all`` percorre os blocos de coluna **e** o bloco especial da janela."""
    ablation.main(["--all"])

    gravados = {caminho.stem for caminho in cli_isolado.glob("*.json")}
    assert WINDOW_BLOCK_NAME in gravados


def test_medicao_do_bloco_nao_escreve_nada_no_banco(base_semeada: Session) -> None:
    """A medição é read-only sobre ``bout_features``: as contagens do granular não mudam."""
    antes = (
        _conta(base_semeada, BoutFeatures),
        _conta(base_semeada, Bout),
        _conta(base_semeada, Event),
    )

    ablation.run_window_correction(base_semeada, n_resamples=B_DE_TESTE)

    assert antes == (
        _conta(base_semeada, BoutFeatures),
        _conta(base_semeada, Bout),
        _conta(base_semeada, Event),
    )
