"""Testes dos blocos D3+D4 da SPEC 009 (Slice 06) -- histórico qualificado pelo adversário.

Cobrem, sobre DataFrame sintético (Pandas puro, sem Postgres):

- **D4** (``opponent_win_rate_prior_avg``): média simples do cartel as-of dos adversários
  anteriores, com o estreante fora do denominador (RF-03) e a leitura feita na **linha do
  adversário naquela luta**, nunca no valor de hoje (RF-04);
- **D3** (``similar_style_win_rate_prior``): média das lutas anteriores **ponderada** pelo
  núcleo gaussiano da semelhança de estilo, sobre **todas** elas -- jamais filtrada por
  limiar (RF-05);
- a corretude point-in-time linha a linha das duas (RF-01) e o **teste do lado do
  adversário** com o contrapositivo, que é o que impede o CA-06 de passar por vacuidade.

A fixture monta o histórico dos **adversários** de propósito: o estilo e o cartel as-of de
cada um saem das lutas anteriores dele, que é exatamente o que a RF-04 manda ler.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
import pytest

from analysis.dataset import PROSCRIBED_FEATURE_BASES
from apps.bouts.enums import BoutMethod
from ingestion.features.rolling import (
    COL_BOUT_ID,
    COL_FIGHTER_ID,
    OPPONENT_WIN_RATE_PRIOR_AVG,
    RECENT_FORM_FEATURES,
    SIMILAR_STYLE_WIN_RATE_PRIOR,
    STYLE_KERNEL_SIGMA,
    add_recent_form_features,
)

_LUTADOR_A = 1

# As duas bases as-of desta slice. Lista literal (mesmo motivo dos blocos anteriores): o
# registro da ablação continua falando do bloco certo mesmo se ``RECENT_FORM_FEATURES``
# crescer por outro motivo.
_BASES_D3_D4: tuple[str, ...] = (SIMILAR_STYLE_WIN_RATE_PRIOR, OPPONENT_WIN_RATE_PRIOR_AVG)

# Box-score que produz o vetor de estilo (grappling=1, volume=0): três quedas, cinco minutos
# de controle e 100% do golpe conectado no solo, sem nenhum golpe significativo. Os três
# componentes do eixo de grappling saturam e os dois do eixo de volume ficam em zero.
_ESTILO_GRAPPLER: dict[str, float] = {
    "takedowns_landed": 3,
    "control": 300.0,
    "ground_landed": 10,
}
# O vetor oposto (grappling=0, volume=1): nenhuma queda, nenhum controle, 100% do golpe
# conectado na distância e seis golpes significativos por minuto (90 em 15 minutos).
_ESTILO_VOLUME: dict[str, float] = {
    "sig_landed": 90,
    "distance_landed": 10,
}
# Nenhum golpe conectado em posição alguma: as shares ficam com denominador zero, os eixos
# nascem nulos (RF-03) e a luta é **inelegível** para o D3.
_ESTILO_INDEFINIDO: dict[str, float] = {}

# O peso do adversário mais dissemelhante possível: ``d**2 = 2`` no quadrado unitário, com
# ``2*sigma**2 = 1``. Conferido contra a constante do módulo no teste da RF-05.
_PESO_DO_EXTREMO = 0.1353352832366127


@dataclass(frozen=True)
class _Luta:
    """Uma luta do lutador A, com o histórico que dá ao adversário estilo e cartel as-of.

    ``historico_do_adversario`` são os resultados das lutas **anteriores** do adversário (é
    delas que saem o ``win_rate_prior`` e os eixos de estilo dele naquela data);
    ``posteriores_do_adversario``, os das lutas **depois** desta -- o insumo do teste do lado
    do adversário (CA-06), que exige que elas não mudem nada.
    """

    resultado: str = "win"
    estilo_do_adversario: dict[str, float] = field(default_factory=dict)
    historico_do_adversario: tuple[str, ...] = ()
    posteriores_do_adversario: tuple[str, ...] = ()


def _participacao(
    *,
    fighter_id: int,
    bout_id: int,
    dia: int,
    resultado: str,
    sig_landed: float = 0,
    takedowns_landed: float = 0,
    control: float = 0.0,
    distance_landed: float = 0,
    clinch_landed: float = 0,
    ground_landed: float = 0,
) -> dict[str, object]:
    """Uma linha lutador-luta da frame longa, com as colunas que ``rolling`` consome.

    Três rounds cheios (15 minutos de cativeiro) em toda luta, para que a taxa por minuto
    seja legível à mão. As colunas que os eixos de estilo não usam entram com ``0`` -- é
    fixture, não imputação: o que este arquivo mede são os dois eixos e o cartel as-of.
    """
    return {
        COL_FIGHTER_ID: fighter_id,
        COL_BOUT_ID: bout_id,
        "event_date": date(2024, 1, dia),
        "result": resultado,
        "method": BoutMethod.DECISION.value,
        "round": 3,
        "ending_time_seconds": 300,
        "sig_strikes_landed": sig_landed,
        "sig_strikes_attempted": 0,
        "takedowns_landed": takedowns_landed,
        "takedowns_attempted": 0,
        "control_time_seconds": control,
        "knockdowns": 0,
        "submission_attempts": 0,
        "total_strikes_landed": 0,
        "head_landed": 0,
        "body_landed": 0,
        "leg_landed": 0,
        "distance_landed": distance_landed,
        "clinch_landed": clinch_landed,
        "ground_landed": ground_landed,
        "head_attempted": 0,
        "body_attempted": 0,
        "leg_attempted": 0,
        "distance_attempted": 0,
        "clinch_attempted": 0,
        "ground_attempted": 0,
        "scheduled_rounds": 3,
    }


def _oposto(resultado: str) -> str:
    """Resultado do outro canto: vitória vira derrota e vice-versa; NC/empate são simétricos."""
    return {"win": "loss", "loss": "win"}.get(resultado, resultado)


def _ordenada(linhas: list[dict[str, object]]) -> pd.DataFrame:
    """Frame na ordem canônica ``(fighter_id, event_date, bout_id)`` -- contrato da Slice 01."""
    return (
        pd.DataFrame(linhas)
        .sort_values(by=[COL_FIGHTER_ID, "event_date", COL_BOUT_ID], kind="stable")
        .reset_index(drop=True)
    )


def _frame(lutas: list[_Luta]) -> pd.DataFrame:
    """Frame longa do lutador A, uma luta por entrada, com o histórico de cada adversário.

    O adversário da i-ésima luta de A recebe ``historico_do_adversario`` lutas antes dela
    (contra enchimentos, sempre com o mesmo box-score de ``estilo_do_adversario``) e
    ``posteriores_do_adversario`` lutas depois -- as anteriores são o que define o estilo e o
    cartel as-of dele; as posteriores existem só para provar que nada as lê (CA-06).
    """
    linhas: list[dict[str, object]] = []
    bout_id = 1000
    for indice, luta in enumerate(lutas):
        adversario = 100 + indice
        dia_da_luta = 10 * (indice + 1)
        anteriores = luta.historico_do_adversario
        for ordem, resultado in enumerate(anteriores):
            bout_id += 1
            dia = dia_da_luta - len(anteriores) + ordem
            for lutador, seu_resultado in (
                (adversario, resultado),
                (500 + indice * 10 + ordem, _oposto(resultado)),
            ):
                linhas.append(
                    _participacao(
                        fighter_id=lutador,
                        bout_id=bout_id,
                        dia=dia,
                        resultado=seu_resultado,
                        **(luta.estilo_do_adversario if lutador == adversario else {}),
                    )
                )
        bout_id += 1
        for lutador, seu_resultado in (
            (_LUTADOR_A, luta.resultado),
            (adversario, _oposto(luta.resultado)),
        ):
            linhas.append(
                _participacao(
                    fighter_id=lutador, bout_id=bout_id, dia=dia_da_luta, resultado=seu_resultado
                )
            )
        for ordem, resultado in enumerate(luta.posteriores_do_adversario):
            bout_id += 1
            for lutador, seu_resultado in (
                (adversario, resultado),
                (700 + indice * 10 + ordem, _oposto(resultado)),
            ):
                linhas.append(
                    _participacao(
                        fighter_id=lutador,
                        bout_id=bout_id,
                        dia=dia_da_luta + 1 + ordem,
                        resultado=seu_resultado,
                        **(luta.estilo_do_adversario if lutador == adversario else {}),
                    )
                )
    return _ordenada(linhas)


def _lutas_de_a(frame: pd.DataFrame) -> pd.DataFrame:
    """As linhas do lutador A, em ordem cronológica, reindexadas de 0."""
    linhas = frame.loc[frame[COL_FIGHTER_ID] == _LUTADOR_A]
    return linhas.sort_values("event_date", kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------------------------------
# D4 -- força de calendário (``opponent_win_rate_prior_avg``)
# ---------------------------------------------------------------------------------------


def test_forca_de_calendario_ignora_adversario_estreante_no_denominador() -> None:
    """CA-05 (RF-03/RF-04): média do cartel as-of, com o estreante fora do denominador.

    O adversário da 1a luta chega com uma vitória e uma derrota (``win_rate_prior`` = 0,5);
    o da 2a é estreante (``win_rate_prior`` ausente). Na 3a luta de A a feature vale
    exatamente ``0,5`` -- o estreante **não** entra no denominador, e tratá-lo como zero
    afirmaria um cartel que ninguém tem.
    """
    frame = _frame(
        [
            _Luta(historico_do_adversario=("win", "loss")),
            _Luta(historico_do_adversario=()),
            _Luta(),
        ]
    )

    resultado = _lutas_de_a(add_recent_form_features(frame))

    assert resultado[OPPONENT_WIN_RATE_PRIOR_AVG].iloc[2] == pytest.approx(0.5)


def test_forca_de_calendario_e_nula_na_estreia() -> None:
    """CA-05: sem nenhuma luta anterior não há adversário anterior -- ausência, não zero."""
    resultado = _lutas_de_a(add_recent_form_features(_frame([_Luta(), _Luta()])))

    assert pd.isna(resultado[OPPONENT_WIN_RATE_PRIOR_AVG].iloc[0])


def test_forca_de_calendario_com_todos_os_adversarios_estreantes_e_nula() -> None:
    """CA-08 (RF-03): denominador vazio vira ``NaN``, jamais ``0.0``.

    ``0.0`` diria "só enfrentou gente que nunca venceu", uma afirmação forte sobre um
    calendário do qual nada se sabe. A distinção entre ausência e zero é o que a RF-03 existe
    para preservar.
    """
    resultado = _lutas_de_a(add_recent_form_features(_frame([_Luta(), _Luta(), _Luta()])))

    assert pd.isna(resultado[OPPONENT_WIN_RATE_PRIOR_AVG].iloc[2])


def test_forca_de_calendario_e_identica_com_e_sem_as_lutas_futuras() -> None:
    """CA-05 (RF-01): point-in-time linha a linha -- as lutas N+1..M não mudam a luta N."""
    completa = _frame(
        [
            _Luta(historico_do_adversario=("win", "loss")),
            _Luta(historico_do_adversario=("win",)),
            _Luta(historico_do_adversario=("loss",)),
        ]
    )
    ultimo_bout_da_segunda = int(_lutas_de_a(completa)[COL_BOUT_ID].iloc[1])
    truncada = completa.loc[completa[COL_BOUT_ID] <= ultimo_bout_da_segunda].reset_index(drop=True)

    de_completa = _lutas_de_a(add_recent_form_features(completa))
    de_truncada = _lutas_de_a(add_recent_form_features(truncada))

    assert de_truncada[OPPONENT_WIN_RATE_PRIOR_AVG].iloc[1] == pytest.approx(
        de_completa[OPPONENT_WIN_RATE_PRIOR_AVG].iloc[1]
    )


def test_forca_de_calendario_entra_no_conjunto_de_features_de_forma_recente() -> None:
    """A feature é emitida como coluna as-of por lutador -- não é intermediária local."""
    assert OPPONENT_WIN_RATE_PRIOR_AVG in RECENT_FORM_FEATURES


def test_forca_de_calendario_nao_le_o_futuro_do_adversario() -> None:
    """CA-06 (RF-04): o cartel do adversário é o **daquela data**, nunca o de hoje.

    As duas frames são idênticas exceto pelo histórico **posterior** do adversário da 1a luta
    de A: numa ele para de lutar, na outra vence mais três vezes **antes** da 2a luta de A. O
    cartel dele *na data em que enfrentou A* não mudou, então a força de calendário de A não
    pode mudar. Uma leitura ingênua ("o ``win_rate_prior`` mais recente do adversário")
    passaria a valer outro número aqui -- e é exatamente esse defeito que a RF-04 proíbe.
    """
    sem_futuro = _frame(
        [_Luta(historico_do_adversario=("win", "loss")), _Luta()],
    )
    com_futuro = _frame(
        [
            _Luta(
                historico_do_adversario=("win", "loss"),
                posteriores_do_adversario=("win", "win", "win"),
            ),
            _Luta(),
        ]
    )

    base = _lutas_de_a(add_recent_form_features(sem_futuro))
    enriquecida = _lutas_de_a(add_recent_form_features(com_futuro))

    assert base[OPPONENT_WIN_RATE_PRIOR_AVG].iloc[1] == pytest.approx(0.5)
    assert enriquecida[OPPONENT_WIN_RATE_PRIOR_AVG].iloc[1] == pytest.approx(
        base[OPPONENT_WIN_RATE_PRIOR_AVG].iloc[1]
    )


def test_forca_de_calendario_muda_com_o_historico_anterior_do_adversario() -> None:
    """CA-06, contrapositivo: o histórico **anterior** do adversário muda, sim, a feature.

    Sem esta metade o teste do lado do adversário passaria por **vacuidade** -- uma feature
    constante (ou sempre nula) satisfaria "não muda com o futuro do adversário" sem ler nada.
    Aqui só o passado do adversário muda (duas vitórias em vez de uma vitória e uma derrota) e
    a força de calendário de A tem de acompanhar.
    """
    meio_a_meio = _frame([_Luta(historico_do_adversario=("win", "loss")), _Luta()])
    invicto = _frame([_Luta(historico_do_adversario=("win", "win")), _Luta()])

    de_meio_a_meio = _lutas_de_a(add_recent_form_features(meio_a_meio))
    de_invicto = _lutas_de_a(add_recent_form_features(invicto))

    assert de_meio_a_meio[OPPONENT_WIN_RATE_PRIOR_AVG].iloc[1] == pytest.approx(0.5)
    assert de_invicto[OPPONENT_WIN_RATE_PRIOR_AVG].iloc[1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------------------
# D3 -- desempenho contra estilo semelhante (``similar_style_win_rate_prior``)
# ---------------------------------------------------------------------------------------


def test_estilo_semelhante_pondera_pelo_nucleo_gaussiano_com_valor_exato() -> None:
    """CA-09: média das anteriores ponderada por ``exp(-d**2 / (2*sigma**2))``.

    Cenário canônico: A venceu um grappler puro ``(1, 0)`` e perdeu para um volumoso puro
    ``(0, 1)``; o adversário da luta corrente é outro volumoso puro ``(0, 1)``. Com
    ``2*sigma**2 = 1``, a vitória sobre o grappler pesa ``exp(-2) = 0,1353`` e a derrota para
    o volumoso pesa ``1,0``, então a feature vale ``0,1353 / 1,1353 = 0,119203``.

    A média **simples** daria ``0,5`` e a versão filtrada por limiar daria ``0,0`` -- os dois
    números que este teste existe para descartar.
    """
    frame = _frame(
        [
            _Luta(
                resultado="win",
                estilo_do_adversario=_ESTILO_GRAPPLER,
                historico_do_adversario=("win",),
            ),
            _Luta(
                resultado="loss",
                estilo_do_adversario=_ESTILO_VOLUME,
                historico_do_adversario=("win",),
            ),
            _Luta(estilo_do_adversario=_ESTILO_VOLUME, historico_do_adversario=("win",)),
        ]
    )

    resultado = _lutas_de_a(add_recent_form_features(frame))

    esperado = _PESO_DO_EXTREMO / (_PESO_DO_EXTREMO + 1.0)
    assert resultado[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2] == pytest.approx(esperado)
    assert resultado[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2] == pytest.approx(0.119203, abs=1e-6)


def test_peso_do_adversario_mais_dissemelhante_possivel_e_maior_que_um_decimo() -> None:
    """CA-09 (RF-05): o núcleo **pondera**, nunca filtra -- o extremo pesa 13,5%, não zero.

    Os dois eixos vivem em ``[0, 1]``, então a distância de estilo tem máximo ``sqrt(2)`` e
    ``d**2`` máximo ``2``. A largura do núcleo é aferida **contra a constante do módulo**,
    jamais contra um literal: é ela que decide se o núcleo pondera ou filtra disfarçado, e um
    número redigitado no teste esconderia uma mudança na produção.
    """
    peso_do_extremo = math.exp(-2.0 / (2.0 * STYLE_KERNEL_SIGMA**2))

    assert peso_do_extremo > 0.1
    assert peso_do_extremo == pytest.approx(_PESO_DO_EXTREMO)


def test_estilo_semelhante_continua_definido_com_adversario_oposto_a_todos() -> None:
    """CA-09 (RF-05): nenhuma luta anterior é descartada por limiar de semelhança.

    As duas vitórias anteriores de A foram sobre grapplers puros e o adversário corrente é o
    volumoso puro -- o confronto mais dissemelhante possível com **todo** o histórico. Uma
    implementação que filtrasse por limiar devolveria ``NaN`` (nenhuma luta "parecida"); a
    ponderada devolve ``1,0``, porque as duas contribuem com o mesmo peso pequeno.
    """
    frame = _frame(
        [
            _Luta(
                resultado="win",
                estilo_do_adversario=_ESTILO_GRAPPLER,
                historico_do_adversario=("win",),
            ),
            _Luta(
                resultado="win",
                estilo_do_adversario=_ESTILO_GRAPPLER,
                historico_do_adversario=("win",),
            ),
            _Luta(estilo_do_adversario=_ESTILO_VOLUME, historico_do_adversario=("win",)),
        ]
    )

    valor = _lutas_de_a(add_recent_form_features(frame))[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2]

    assert not pd.isna(valor)
    assert valor == pytest.approx(1.0)


def test_estilo_semelhante_e_nulo_sem_nenhuma_luta_anterior_elegivel() -> None:
    """CA-08 (RF-03): adversário anterior de eixo nulo não entra em nenhum dos dois termos.

    Os dois adversários anteriores de A são estreantes, então o vetor de estilo deles é
    desconhecido e as duas lutas ficam fora do numerador **e** do denominador. Sem nenhuma
    elegível o denominador é zero e a feature é ausência -- nunca ``0.0``, que o modelo leria
    como "nunca venceu ninguém parecido".
    """
    frame = _frame(
        [
            _Luta(resultado="win"),
            _Luta(resultado="win"),
            _Luta(estilo_do_adversario=_ESTILO_VOLUME, historico_do_adversario=("win",)),
        ]
    )

    resultado = _lutas_de_a(add_recent_form_features(frame))

    assert pd.isna(resultado[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2])
    assert resultado[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2] != 0.0


def test_estilo_semelhante_e_nulo_quando_o_adversario_corrente_nao_tem_estilo() -> None:
    """CA-08 (RF-03): sem o vetor do adversário **corrente** não há distância a medir.

    A tem uma vitória anterior sobre um grappler de estilo conhecido, mas o adversário desta
    luta é estreante: não existe semelhança a calcular, e inventar peso uniforme faria a
    feature virar ``win_rate_prior`` disfarçada, em silêncio.
    """
    frame = _frame(
        [
            _Luta(
                resultado="win",
                estilo_do_adversario=_ESTILO_GRAPPLER,
                historico_do_adversario=("win",),
            ),
            _Luta(estilo_do_adversario=_ESTILO_INDEFINIDO),
        ]
    )

    resultado = _lutas_de_a(add_recent_form_features(frame))

    assert pd.isna(resultado[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[1])


def test_estilo_semelhante_deixa_no_contest_e_empate_fora_dos_dois_termos() -> None:
    """CA-08 (RF-03): NC e empate saem do numerador **e** do denominador.

    Mesma convenção de ``win_rate_prior``. A venceu um volumoso e depois teve um no contest
    contra outro volumoso; o adversário corrente é volumoso. Contando o NC como derrota a
    feature valeria ``0,5``; a convenção correta devolve ``1,0``.
    """
    frame = _frame(
        [
            _Luta(
                resultado="win",
                estilo_do_adversario=_ESTILO_VOLUME,
                historico_do_adversario=("win",),
            ),
            _Luta(
                resultado="no_contest",
                estilo_do_adversario=_ESTILO_VOLUME,
                historico_do_adversario=("win",),
            ),
            _Luta(estilo_do_adversario=_ESTILO_VOLUME, historico_do_adversario=("win",)),
        ]
    )

    resultado = _lutas_de_a(add_recent_form_features(frame))

    assert resultado[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2] == pytest.approx(1.0)


def test_estilo_semelhante_e_nulo_so_com_no_contest_e_empate_antes() -> None:
    """CA-08: histórico anterior inteiro de NC/empate deixa o denominador vazio -> nulo."""
    frame = _frame(
        [
            _Luta(
                resultado="no_contest",
                estilo_do_adversario=_ESTILO_VOLUME,
                historico_do_adversario=("win",),
            ),
            _Luta(
                resultado="draw",
                estilo_do_adversario=_ESTILO_VOLUME,
                historico_do_adversario=("win",),
            ),
            _Luta(estilo_do_adversario=_ESTILO_VOLUME, historico_do_adversario=("win",)),
        ]
    )

    resultado = _lutas_de_a(add_recent_form_features(frame))

    assert pd.isna(resultado[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2])


def test_estilo_semelhante_e_nulo_na_estreia() -> None:
    """CA-08: sem luta anterior nenhuma não há o que ponderar."""
    frame = _frame(
        [
            _Luta(estilo_do_adversario=_ESTILO_VOLUME, historico_do_adversario=("win",)),
            _Luta(estilo_do_adversario=_ESTILO_VOLUME, historico_do_adversario=("win",)),
        ]
    )

    resultado = _lutas_de_a(add_recent_form_features(frame))

    assert pd.isna(resultado[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[0])


def _cenario_canonico(posteriores: tuple[str, ...] = ()) -> pd.DataFrame:
    """Cenário canônico do D3, com histórico posterior opcional do 1o adversário (CA-06)."""
    return _frame(
        [
            _Luta(
                resultado="win",
                estilo_do_adversario=_ESTILO_GRAPPLER,
                historico_do_adversario=("win",),
                posteriores_do_adversario=posteriores,
            ),
            _Luta(
                resultado="loss",
                estilo_do_adversario=_ESTILO_VOLUME,
                historico_do_adversario=("win",),
            ),
            _Luta(estilo_do_adversario=_ESTILO_VOLUME, historico_do_adversario=("win",)),
        ]
    )


def test_estilo_semelhante_e_identico_com_e_sem_as_lutas_futuras() -> None:
    """CA-05 (RF-01): point-in-time linha a linha -- as lutas N+1..M não mudam a luta N.

    A prova importa mais aqui do que nas features anteriores: o D3 é a **primeira** feature do
    projeto que não usa janela do Pandas (o peso depende da linha corrente, e nenhuma
    ``rolling``/``expanding`` expressa isso). A exclusão do futuro vem do prefixo estrito, não
    de um ``shift(1)`` herdado.
    """
    completa = _cenario_canonico()
    ultimo_bout_da_segunda = int(_lutas_de_a(completa)[COL_BOUT_ID].iloc[1])
    truncada = completa.loc[completa[COL_BOUT_ID] <= ultimo_bout_da_segunda].reset_index(drop=True)

    de_completa = _lutas_de_a(add_recent_form_features(completa))
    de_truncada = _lutas_de_a(add_recent_form_features(truncada))

    assert de_truncada[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[1] == pytest.approx(
        de_completa[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[1]
    )


def test_estilo_semelhante_nao_le_o_futuro_do_adversario() -> None:
    """CA-06 (RF-04): o estilo do adversário passado é o **daquela data**, não o de hoje.

    O adversário grappler da 1a luta de A vira um volumoso puro depois dela (três lutas de
    puro volume antes da luta corrente de A). Lido "hoje", ele seria idêntico ao adversário
    corrente e o peso da vitória de A saltaria de ``0,1353`` para ``1,0``, levando a feature
    de ``0,119`` para ``0,5``. Lido as-of, nada muda.
    """
    base = _lutas_de_a(add_recent_form_features(_cenario_canonico()))
    com_futuro = _lutas_de_a(
        add_recent_form_features(_cenario_canonico(posteriores=("win", "win", "win")))
    )

    assert com_futuro[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2] == pytest.approx(
        base[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2]
    )
    assert com_futuro[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2] == pytest.approx(0.119203, abs=1e-6)


def test_estilo_semelhante_muda_com_o_estilo_anterior_do_adversario() -> None:
    """CA-06, contrapositivo: o passado do adversário muda, sim, o peso da luta.

    Sem esta metade o teste anterior passaria por **vacuidade**. Aqui o adversário da 1a luta
    de A é volumoso desde sempre (em vez de grappler): o peso daquela vitória vira ``1,0`` e a
    feature sobe de ``0,119`` para ``0,5``.
    """
    volumoso_desde_sempre = _frame(
        [
            _Luta(
                resultado="win",
                estilo_do_adversario=_ESTILO_VOLUME,
                historico_do_adversario=("win",),
            ),
            _Luta(
                resultado="loss",
                estilo_do_adversario=_ESTILO_VOLUME,
                historico_do_adversario=("win",),
            ),
            _Luta(estilo_do_adversario=_ESTILO_VOLUME, historico_do_adversario=("win",)),
        ]
    )

    resultado = _lutas_de_a(add_recent_form_features(volumoso_desde_sempre))

    assert resultado[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2] == pytest.approx(0.5)


def test_prefixo_do_estilo_semelhante_independe_da_ordem_da_frame_recebida() -> None:
    """RF-01: a ordenação canônica é pré-condição **reforçada**, não presumida do caller.

    O D3 é calculado por **prefixo posicional** dentro do grupo do lutador -- "tudo antes
    desta linha". Isso só é o passado real se a frame estiver na ordem
    ``(fighter_id, event_date, bout_id)``. Com uma frame embaralhada e sem reordenação
    defensiva, a feature leria lutas futuras **em silêncio**, que é o pior modo de falha
    possível. Aqui a entrada chega embaralhada de propósito e o resultado tem de ser idêntico.
    """
    ordenada = _cenario_canonico()
    embaralhada = ordenada.sample(frac=1.0, random_state=0).reset_index(drop=True)

    de_ordenada = _lutas_de_a(add_recent_form_features(ordenada))
    de_embaralhada = _lutas_de_a(add_recent_form_features(embaralhada))

    for base in _BASES_D3_D4:
        pd.testing.assert_series_equal(de_ordenada[base], de_embaralhada[base], check_names=False)


def test_bases_do_bloco_d3_d4_entram_no_conjunto_de_features_de_forma_recente() -> None:
    """As duas são emitidas como coluna as-of por lutador -- não são intermediárias locais."""
    for base in _BASES_D3_D4:
        assert base in RECENT_FORM_FEATURES, base


def test_nomes_do_bloco_d3_d4_nao_colidem_com_as_bases_proscritas() -> None:
    """CA-12 (RF-10): nenhum nome do D3/D4 é média de carreira proscrita do snapshot 2025."""
    for base in _BASES_D3_D4:
        assert base not in PROSCRIBED_FEATURE_BASES, base
        for sufixo in ("_a", "_b", "_diff"):
            assert f"{base}{sufixo}" not in PROSCRIBED_FEATURE_BASES, f"{base}{sufixo}"
