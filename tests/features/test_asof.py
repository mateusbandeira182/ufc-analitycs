"""Testes do cache as-of por lutador (``ingestion.features.asof``) -- Plano 011-01.

Cobrem a Slice 01 da SPEC 011 (M9): materializar o vetor de features "agora" de cada lutador
numa tabela própria, idempotente por ``fighter_id``, para que o serving pare de reconstruir a
pipeline a cada request.

Três grupos de asserção:

1. **Insumo do bloco D3** (``rolling.opponent_asof_style_history`` e
   ``rolling.similar_style_prior_for_opponent``) -- ``similar_style_win_rate_prior`` é feature
   do PAR (D-15): o que se cacheia por lutador é o histórico de estilos enfrentados, e a
   fórmula que o transforma em valor de confronto continua vivendo num lugar só.
2. **Construção das linhas as-of** -- uma linha por lutador **com luta anterior**, payload sem
   a feature do par, ausência atravessando como ``null`` explícito (nunca ``NaN``, que não é
   JSON válido).
3. **Idempotência do upsert** -- reexecutar mantém contagem e conteúdo; só ``materialized_at``
   é refrescado. É o sinal demonstrável desta camada, asserido sobre o estado do banco.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import BoutFighter
from apps.features.models import FeaturePayload, FighterAsOfFeatures, StyleHistory
from ingestion.features.asof import (
    asof_payload_columns,
    build_asof_materialization,
    enrich_asof,
    materialize_asof_features,
)
from ingestion.features.long_frame import LONG_FRAME_COLUMNS, build_long_frame, read_granular
from ingestion.features.materialize import SOURCE
from ingestion.features.rolling import (
    GRAPPLING_AXIS_R3,
    OPPONENT_GRAPPLING_AXIS_R3,
    OPPONENT_VOLUME_AXIS_R3,
    SIMILAR_STYLE_WIN_RATE_PRIOR,
    STYLE_HISTORY_COLUMNS,
    STYLE_HISTORY_WON,
    add_recent_form_features,
    opponent_asof_style_history,
    similar_style_prior_for_opponent,
)
from tests.analysis.test_predict import _make_fighter, _seed_future_bout, _seed_history

# Núcleo gaussiano do D3 com ``2*sigma**2 == 1``: o adversário mais dissemelhante possível
# (``d**2 == 2``, os dois eixos vivem em [0, 1]) pesa ``exp(-2)``. Valor fixado por argumento
# na SPEC 009, jamais ajustado pela métrica -- aqui ele é só a aritmética esperada.
_PESO_MAIS_DISSEMELHANTE = math.exp(-2.0)

# Tolerância de paridade da RF-03: o cache tem de bater com a pipeline ao vivo neste limite.
_TOLERANCIA = 1e-9

# ``as_of`` fixo dos testes de materialização: o histórico sintético termina em 2019-2020, e
# fixar a data mantém o recorte "luta anterior" determinístico, sem depender do relógio.
_AS_OF = date(2026, 1, 1)


def _linhas_do_cache(
    session: Session,
) -> dict[int, tuple[FeaturePayload, StyleHistory, datetime, str]]:
    """Estado persistido do cache as-of, por lutador -- a asserção é sobre o banco."""
    session.expire_all()
    linhas = session.execute(select(FighterAsOfFeatures)).scalars().all()
    return {
        int(linha.fighter_id): (
            linha.features,
            linha.style_history,
            linha.materialized_at,
            linha.source,
        )
        for linha in linhas
    }


def _n_bouts_source(session: Session) -> int:
    """Contagem de lutas gravada na última materialização (denominador da guarda)."""
    session.expire_all()
    return int(session.execute(select(func.max(FighterAsOfFeatures.n_bouts_source))).scalar_one())


def _contagem_de_lutas_por_lutador(session: Session) -> dict[int, int]:
    """Quantas participações cada lutador tem no granular semeado (todas passadas)."""
    linhas = session.execute(
        select(BoutFighter.fighter_id, func.count()).group_by(BoutFighter.fighter_id)
    ).all()
    return {int(fighter_id): int(total) for fighter_id, total in linhas}


def _long_row(
    *,
    bout_id: int,
    fighter_id: int,
    corner: Corner,
    result: str,
    event_date: date,
    sig_strikes_landed: int,
    takedowns_landed: int,
) -> dict[str, object]:
    """Uma participação (lutador-luta) completa da frame longa, com box-score plausível.

    Parte de ``LONG_FRAME_COLUMNS`` para que nenhuma coluna do contrato falte -- é o mesmo
    recurso que ``analysis.predict._hypothetical_rows`` usa. O box-score varia por linha
    porque os **eixos de estilo** (insumo do D3) são derivados dele: com valores constantes
    todos os adversários teriam o mesmo estilo e o núcleo gaussiano ficaria degenerado.
    """
    row: dict[str, object] = dict.fromkeys(LONG_FRAME_COLUMNS)
    row.update(
        {
            "bout_id": bout_id,
            "fighter_id": fighter_id,
            "fighter_name": f"Fighter {fighter_id}",
            "event_id": bout_id,
            "event_name": f"UFC {bout_id}",
            "event_date": event_date,
            "corner": corner,
            "result": result,
            "method": BoutMethod.DECISION.value,
            "round": 3,
            "ending_time_seconds": 300,
            "weight_class": "lightweight",
            "title_bout": False,
            "scheduled_rounds": 3,
            "knockdowns": 0,
            "sig_strikes_landed": sig_strikes_landed,
            "sig_strikes_attempted": sig_strikes_landed * 2,
            "takedowns_landed": takedowns_landed,
            "takedowns_attempted": takedowns_landed + 2,
            "submission_attempts": 0,
            "control_time_seconds": takedowns_landed * 60,
            "total_strikes_landed": sig_strikes_landed + 10,
            "total_strikes_attempted": sig_strikes_landed * 2 + 10,
            "head_landed": sig_strikes_landed - 5,
            "head_attempted": sig_strikes_landed,
            "body_landed": 3,
            "body_attempted": 5,
            "leg_landed": 2,
            "leg_attempted": 3,
            "distance_landed": sig_strikes_landed - 8,
            "distance_attempted": sig_strikes_landed,
            "clinch_landed": 5,
            "clinch_attempted": 7,
            "ground_landed": 3,
            "ground_attempted": 4,
            "reversals": 0,
            "source": "kaggle",
        }
    )
    return row


def _carreira_de_tres_lutas() -> pd.DataFrame:
    """Frame longa de um elenco de quatro lutadores, com o lutador 1 em três lutas.

    Os rivais (2, 3 e 4) se enfrentam **antes** de cruzarem com o lutador 1, porque os eixos
    de estilo do adversário são **as-of**: um rival estreante teria eixos nulos e o D3 do
    lutador 1 sairia ``NaN`` -- o teste passaria medindo ausência, não a fórmula. Cada linha
    tem box-score distinto para que os estilos as-of não coincidam.
    """
    rows: list[dict[str, object]] = []
    combates = [
        # (bout_id, lutador, rival, desfecho do lutador, data, golpes, quedas, do rival)
        (1, 2, 3, "win", date(2019, 1, 1), 45, 0, 22, 3),
        (2, 3, 4, "win", date(2019, 2, 1), 30, 2, 50, 1),
        (3, 2, 4, "loss", date(2019, 3, 1), 38, 1, 28, 4),
        (4, 1, 2, "win", date(2020, 1, 1), 40, 0, 20, 4),
        (5, 1, 3, "loss", date(2020, 6, 1), 25, 3, 60, 0),
        (6, 1, 4, "win", date(2021, 1, 1), 55, 1, 30, 2),
    ]
    for (
        bout_id,
        atleta,
        rival,
        resultado,
        dia,
        golpes,
        quedas,
        golpes_rival,
        quedas_rival,
    ) in combates:
        rows.append(
            _long_row(
                bout_id=bout_id,
                fighter_id=atleta,
                corner=Corner.RED,
                result=resultado,
                event_date=dia,
                sig_strikes_landed=golpes,
                takedowns_landed=quedas,
            )
        )
        rows.append(
            _long_row(
                bout_id=bout_id,
                fighter_id=rival,
                corner=Corner.BLUE,
                result="loss" if resultado == "win" else "win",
                event_date=dia,
                sig_strikes_landed=golpes_rival,
                takedowns_landed=quedas_rival,
            )
        )
    return pd.DataFrame(rows)


def test_opponent_asof_style_history_devolve_eixos_do_adversario_e_desfecho() -> None:
    """Por linha: os dois eixos as-of do ADVERSÁRIO daquela luta e o desfecho do lutador.

    É o insumo declarado do D3 (D-15 da SPEC 011): o que pode ser cacheado por lutador não é
    a feature do par, é o histórico de estilos que ele enfrentou. Reusa ``_opponent_stats``,
    de modo que a regra de pareamento continua num lugar só.
    """
    enriquecida = add_recent_form_features(_carreira_de_tres_lutas())

    historico = opponent_asof_style_history(enriquecida)

    assert list(historico.columns) == [
        OPPONENT_GRAPPLING_AXIS_R3,
        OPPONENT_VOLUME_AXIS_R3,
        STYLE_HISTORY_WON,
    ]
    assert list(historico.index) == list(enriquecida.index)
    # O eixo projetado é o da linha do ADVERSÁRIO na MESMA luta, não o do próprio lutador.
    for posicao in range(len(enriquecida)):
        bout_id = enriquecida["bout_id"].iloc[posicao]
        fighter_id = enriquecida["fighter_id"].iloc[posicao]
        rival = enriquecida[
            (enriquecida["bout_id"] == bout_id) & (enriquecida["fighter_id"] != fighter_id)
        ]
        esperado = rival[GRAPPLING_AXIS_R3].iloc[0]
        obtido = historico[OPPONENT_GRAPPLING_AXIS_R3].iloc[posicao]
        assert (pd.isna(esperado) and pd.isna(obtido)) or esperado == obtido


def test_opponent_asof_style_history_mapeia_o_desfecho_com_nan_em_nc() -> None:
    """Vitória vira ``1.0``, derrota ``0.0`` e no contest/empate ``NaN`` (fora do cálculo).

    Mesma convenção de ``win_rate_prior``: o no contest não é meia vitória, é ausência de
    informação -- fica fora do numerador **e** do denominador da média ponderada.
    """
    frame = _carreira_de_tres_lutas()
    frame.loc[frame["bout_id"] == 5, "result"] = "no_contest"
    enriquecida = add_recent_form_features(frame)

    historico = opponent_asof_style_history(enriquecida)

    desfechos = historico[STYLE_HISTORY_WON]
    vitorias = desfechos[enriquecida["result"].to_numpy() == "win"]
    derrotas = desfechos[enriquecida["result"].to_numpy() == "loss"]
    sem_resultado = desfechos[enriquecida["result"].to_numpy() == "no_contest"]
    assert (vitorias == 1.0).all()
    assert (derrotas == 0.0).all()
    assert sem_resultado.isna().all()


def test_similar_style_prior_for_opponent_pondera_pelo_nucleo_gaussiano() -> None:
    """Com histórico e eixos conhecidos, o valor é a média ponderada esperada à mão.

    Uma vitória contra um adversário de estilo **idêntico** ao de hoje (peso ``1``) e uma
    derrota contra o mais dissemelhante possível (peso ``exp(-2)``): o prior é
    ``1 / (1 + exp(-2))``. Aritmética explícita, não uma chamada espelhando a implementação.
    """
    historico = pd.DataFrame(
        [
            {
                OPPONENT_GRAPPLING_AXIS_R3: 0.0,
                OPPONENT_VOLUME_AXIS_R3: 0.0,
                STYLE_HISTORY_WON: 1.0,
            },
            {
                OPPONENT_GRAPPLING_AXIS_R3: 1.0,
                OPPONENT_VOLUME_AXIS_R3: 1.0,
                STYLE_HISTORY_WON: 0.0,
            },
        ]
    )

    prior = similar_style_prior_for_opponent(historico, 0.0, 0.0)

    assert prior is not None
    assert prior == pytest.approx(1.0 / (1.0 + _PESO_MAIS_DISSEMELHANTE), abs=_TOLERANCIA)


def test_similar_style_prior_for_opponent_sem_historico_elegivel_devolve_none() -> None:
    """Histórico vazio ou só com no contest devolve ``None`` -- jamais ``0.0``.

    Denominador zero não é "perdeu todas": é ausência de base para opinar. Zerar afirmaria
    algo que o dado não permite (RF-03 da SPEC 009).
    """
    vazio = pd.DataFrame(
        columns=[OPPONENT_GRAPPLING_AXIS_R3, OPPONENT_VOLUME_AXIS_R3, STYLE_HISTORY_WON]
    )
    assert similar_style_prior_for_opponent(vazio, 0.5, 0.5) is None

    so_no_contest = pd.DataFrame(
        [
            {
                OPPONENT_GRAPPLING_AXIS_R3: 0.5,
                OPPONENT_VOLUME_AXIS_R3: 0.5,
                STYLE_HISTORY_WON: float("nan"),
            }
        ]
    )
    assert similar_style_prior_for_opponent(so_no_contest, 0.5, 0.5) is None


def test_similar_style_prior_for_opponent_sem_eixo_do_adversario_devolve_none() -> None:
    """Adversário sem eixos as-of (estreante) não tem semelhança computável -> ``None``.

    O ``NaN`` propaga pelo peso e o denominador zera; devolver ``0.5`` ou ``0.0`` inventaria
    um estilo que ninguém mediu.
    """
    historico = pd.DataFrame(
        [
            {
                OPPONENT_GRAPPLING_AXIS_R3: 0.2,
                OPPONENT_VOLUME_AXIS_R3: 0.3,
                STYLE_HISTORY_WON: 1.0,
            }
        ]
    )

    assert similar_style_prior_for_opponent(historico, None, None) is None


def test_similar_style_prior_for_opponent_reproduz_a_pipeline_ao_vivo() -> None:
    """A fórmula do cache e a da pipeline dão o MESMO valor para a mesma configuração.

    Amarra de primeira classe do cache por lutador: ``add_recent_form_features`` calcula o D3
    da terceira luta a partir do prefixo estrito das duas anteriores; alimentar
    ``similar_style_prior_for_opponent`` com esse mesmo prefixo e com os eixos do adversário
    da terceira tem de reproduzir o valor exato. Se divergir, o serving pelo cache passa a
    comer uma feature diferente da que o modelo viu no treino -- em silêncio.
    """
    enriquecida = add_recent_form_features(_carreira_de_tres_lutas())
    do_lutador = enriquecida[enriquecida["fighter_id"] == 1]
    historico_completo = opponent_asof_style_history(enriquecida)
    prefixo = historico_completo.loc[do_lutador.index[:2]]
    eixos_de_hoje = historico_completo.loc[do_lutador.index[2]]

    prior = similar_style_prior_for_opponent(
        prefixo,
        float(eixos_de_hoje[OPPONENT_GRAPPLING_AXIS_R3]),
        float(eixos_de_hoje[OPPONENT_VOLUME_AXIS_R3]),
    )

    esperado = do_lutador[SIMILAR_STYLE_WIN_RATE_PRIOR].iloc[2]
    assert not pd.isna(esperado)
    assert prior is not None
    assert prior == pytest.approx(float(esperado), abs=_TOLERANCIA)


def test_build_asof_materialization_uma_linha_por_lutador_com_historico(
    db_session: Session,
) -> None:
    """CA-01: exatamente uma linha por lutador com ao menos uma luta anterior a ``as_of``."""
    ids = _seed_history(db_session)

    materialization = build_asof_materialization(db_session, _AS_OF)

    assert {row.fighter_id for row in materialization.rows} == set(ids.values())
    assert len(materialization.rows) == len(ids)
    assert materialization.as_of == _AS_OF


def test_build_asof_materialization_ignora_lutador_so_com_luta_futura(
    db_session: Session,
) -> None:
    """Lutador cuja ÚNICA luta cadastrada é futura **não** ganha linha no cache (DP-07).

    A regra migrou de ``analysis.predict._fighters_with_past_bouts`` para cá: estar no granular
    não basta, é preciso ter luta **anterior** a ``as_of``. Sem esse recorte, um estreante
    escalado no card passaria no teste de histórico pela própria luta que se quer prever, e o
    serving devolveria um palpite fabricado sobre um vetor inteiramente nulo.
    """
    ids = _seed_history(db_session)
    estreante = _make_fighter("Fighter Estreante", 195)
    db_session.add(estreante)
    db_session.flush()
    _seed_future_bout(db_session, ids["Fighter Alpha"], int(estreante.id))

    materialization = build_asof_materialization(db_session, _AS_OF)

    assert int(estreante.id) not in {row.fighter_id for row in materialization.rows}


def test_build_asof_materialization_payload_traz_as_bases_as_of(db_session: Session) -> None:
    """O payload cobre exatamente ``asof_payload_columns`` -- nem a mais, nem a menos.

    Percorre o conjunto de bases emitido pela pipeline, nunca uma lista redigitada: é o que
    faz uma feature nova entrar no cache sozinha em vez de sumir em silêncio.
    """
    _seed_history(db_session)
    enriquecida = enrich_asof(db_session, build_long_frame(read_granular(db_session)))
    esperadas = set(asof_payload_columns(enriquecida))

    materialization = build_asof_materialization(db_session, _AS_OF)

    assert esperadas
    for row in materialization.rows:
        assert set(row.features) == esperadas


def test_build_asof_materialization_nao_vaza_a_feature_do_par(db_session: Session) -> None:
    """CA-P3: ``similar_style_win_rate_prior`` **não** entra no payload por lutador (D-15).

    É feature do PAR: depende de quem é o adversário. Se entrasse aqui, alguém a alinharia a
    ``feature_names`` e o serving passaria a comer o valor errado -- calculado sem adversário.
    O que o cache guarda é o insumo (``style_history``), de onde o confronto a deriva.
    """
    _seed_history(db_session)

    materialization = build_asof_materialization(db_session, _AS_OF)

    for row in materialization.rows:
        assert SIMILAR_STYLE_WIN_RATE_PRIOR not in row.features
    # A base as-of que o D3 consome continua no payload -- o que sai é só a feature do par.
    assert all(GRAPPLING_AXIS_R3 in row.features for row in materialization.rows)


def test_build_asof_materialization_style_history_uma_entrada_por_luta_passada(
    db_session: Session,
) -> None:
    """CA-P3: ``style_history`` tem uma entrada por luta passada, em ordem cronológica."""
    ids = _seed_history(db_session)
    lutas_passadas = _contagem_de_lutas_por_lutador(db_session)

    materialization = build_asof_materialization(db_session, _AS_OF)

    por_lutador = {row.fighter_id: row for row in materialization.rows}
    for nome, fighter_id in ids.items():
        historico = por_lutador[fighter_id].style_history
        assert len(historico) == lutas_passadas[fighter_id], nome
        assert all(set(entrada) == set(STYLE_HISTORY_COLUMNS) for entrada in historico), nome


def test_build_asof_materialization_conta_as_lutas_que_a_pipeline_consumiu(
    db_session: Session,
) -> None:
    """``n_bouts_source`` é o recorte de ``read_granular`` -- o denominador da guarda (RF-05).

    Amarrado a ``read_granular`` de propósito: um denominador mais largo que a pipeline
    marcaria o cache como defasado para sempre. O teste-guarda que liga este recorte ao de
    ``apps.features.selectors.count_source_bouts`` vive em
    ``apps/features/tests/test_selectors.py`` (CA-P2).
    """
    _seed_history(db_session)

    materialization = build_asof_materialization(db_session, _AS_OF)

    assert materialization.n_bouts_source == len(read_granular(db_session).bouts)


def test_build_asof_materialization_ausencia_atravessa_como_none(db_session: Session) -> None:
    """Valor ausente vira ``None`` explícito -- nunca ``NaN`` (que não é JSON válido).

    A estreia de cada lutador produz features as-of nulas; na linha sintética "agora" sobram
    as que dependem de dado que o granular sintético não tem (round-a-round, por exemplo).
    O que importa é que a ausência chegue ao JSONB como nulo, não como ``float('nan')``.
    """
    _seed_history(db_session)

    materialization = build_asof_materialization(db_session, _AS_OF)

    valores = [valor for row in materialization.rows for valor in row.features.values()]
    assert any(valor is None for valor in valores)
    assert not any(isinstance(valor, float) and math.isnan(valor) for valor in valores)


def test_materialize_asof_features_e_idempotente(db_session: Session) -> None:
    """CA-01/RF-02: reexecutar mantém contagem e conteúdo; só ``materialized_at`` muda.

    O sinal demonstrável desta camada é o **estado do banco**: rodar o estágio duas vezes
    sobre o mesmo granular não pode criar linha nova nem alterar payload. ``materialized_at``
    é metadado de geração e é refrescado de propósito -- é ele que a API expõe para dizer
    quando o cache foi reconstruído.
    """
    _seed_history(db_session)

    primeiro = materialize_asof_features(db_session, build_asof_materialization(db_session, _AS_OF))
    antes = _linhas_do_cache(db_session)

    segundo = materialize_asof_features(db_session, build_asof_materialization(db_session, _AS_OF))
    depois = _linhas_do_cache(db_session)

    assert primeiro == segundo == len(antes)
    assert set(antes) == set(depois)
    for fighter_id, (features, style_history, materialized_at, source) in antes.items():
        novas_features, novo_historico, novo_instante, nova_source = depois[fighter_id]
        assert novas_features == features
        assert novo_historico == style_history
        assert nova_source == source == SOURCE
        assert novo_instante > materialized_at


def test_materialize_asof_features_grava_ausencia_como_null_no_jsonb(
    db_session: Session,
) -> None:
    """A ausência chega ao JSONB como ``null``, jamais como a string ``"NaN"``.

    ``NaN`` não é JSON válido; deixá-lo atravessar produziria ou erro de serialização ou --
    pior -- uma string que passaria despercebida e envenenaria o alinhamento no serving.
    """
    _seed_history(db_session)

    materialize_asof_features(db_session, build_asof_materialization(db_session, _AS_OF))

    valores = [
        valor
        for features, _historico, _instante, _source in _linhas_do_cache(db_session).values()
        for valor in features.values()
    ]
    assert any(valor is None for valor in valores)
    assert not any(isinstance(valor, str) for valor in valores)


def test_materialize_asof_features_reflete_luta_nova_apos_rematerializar(
    db_session: Session,
) -> None:
    """``n_bouts_source`` acompanha o granular quando o cache é reconstruído.

    O par desta asserção vive em ``apps/features/tests/test_selectors.py``: lá a luta nova
    **sem** re-materializar faz ``stale`` virar ``true``. Aqui o que se exige é o outro lado
    -- re-materializar realinha a contagem, e o cache volta a estar em dia.
    """
    ids = _seed_history(db_session)
    materialize_asof_features(db_session, build_asof_materialization(db_session, _AS_OF))
    antes = _n_bouts_source(db_session)

    _seed_future_bout(db_session, ids["Fighter Alpha"], ids["Fighter Bravo"])
    materialize_asof_features(db_session, build_asof_materialization(db_session, _AS_OF))

    assert _n_bouts_source(db_session) == antes + 1
