"""Testes da gravação de predições (Service) contra o Postgres de teste transacional.

Cobrem o que a Slice 07 existe para garantir (CA-08 da SPEC 007): a gravação é
**idempotente na chave natural** ``(bout_id, model_version)`` -- prever a mesma luta com
o mesmo modelo atualiza a linha existente em vez de duplicar -- e uma versão de modelo
diferente gera linha nova, que é o que faz a série temporal do walk-forward existir.

A Slice 08 acrescenta os testes de ``predict_event_card``: uma linha por luta **predita** (as
indisponíveis não geram registro), com ``source="api"`` e a versão do artefato, mantendo a
idempotência ao prever o mesmo card duas vezes -- e a prova de que o service **commita** (a
sessão da API nunca commita sozinha) -- mais os dois ramos de elegibilidade do card: luta sem os
dois cantos cadastrados e lutador escalado em mais de uma luta do mesmo card, que viram
``unavailable_reason`` sem derrubar as demais. O histórico sintético, o treino e o card vêm dos
helpers dos testes de serving e de API, para não duplicar seed.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analysis.model import load_artifact
from analysis.predict import CardPrediction, predict_card
from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.bouts.tests.factories import BoutFactory, EventFactory
from apps.fighters.tests.factories import FighterFactory
from apps.predictions.models import BoutPrediction
from apps.predictions.services import (
    SOURCE_API,
    SOURCE_WALK_FORWARD,
    predict_event_card,
    record_prediction,
)
from apps.predictions.tests.test_api import _seed_card
from tests.analysis.test_predict import _make_fighter, _seed_history, _train_and_persist

VERSAO = "2026-08-31T10:00:00+00:00"
OUTRA_VERSAO = "2026-09-15T10:00:00+00:00"


def _seed_bout(session: Session, *, winner_id: int | None = None) -> int:
    """Semeia evento + luta e devolve o ``bout_id`` persistido."""
    event = EventFactory.build(date=date(2026, 8, 1))
    session.add(event)
    session.flush()
    bout = BoutFactory.build(event_id=event.id, winner_id=winner_id)
    session.add(bout)
    session.flush()
    return bout.id


def _seed_fighter(session: Session) -> int:
    """Semeia um lutador e devolve o ``fighter_id`` persistido."""
    fighter = FighterFactory.build()
    session.add(fighter)
    session.flush()
    return fighter.id


def _contagem(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(BoutPrediction)) or 0


def test_record_prediction_grava_a_predicao(db_session: Session) -> None:
    """A gravação persiste vencedor previsto, probabilidade, versão e contagem de features."""
    bout_id = _seed_bout(db_session)
    fighter_id = _seed_fighter(db_session)

    prediction_id = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=fighter_id,
        prob_predicted_winner=0.62,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_API,
    )

    gravada = db_session.get(BoutPrediction, prediction_id)
    assert gravada is not None
    assert gravada.bout_id == bout_id
    assert gravada.predicted_winner_id == fighter_id
    assert gravada.prob_predicted_winner == 0.62
    assert gravada.model_version == VERSAO
    assert gravada.n_features == 39


def test_record_prediction_e_idempotente_na_chave_natural(db_session: Session) -> None:
    """CA-08: gravar 2x a mesma ``(bout_id, model_version)`` mantém **uma** linha.

    O id devolvido é o mesmo (é a mesma linha, não uma nova) e a **predição** reflete a
    última afirmação daquele modelo sobre aquela luta. ``source`` fica de fora: ele registra
    a procedência da primeira gravação e é preservado -- ver
    ``test_record_prediction_preserva_o_instante_e_a_origem_da_primeira_gravacao``.
    """
    bout_id = _seed_bout(db_session)
    primeiro_previsto = _seed_fighter(db_session)
    segundo_previsto = _seed_fighter(db_session)

    primeiro = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=primeiro_previsto,
        prob_predicted_winner=0.55,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_API,
    )
    segundo = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=segundo_previsto,
        prob_predicted_winner=0.71,
        model_version=VERSAO,
        n_features=42,
        source=SOURCE_WALK_FORWARD,
    )

    assert primeiro == segundo
    assert _contagem(db_session) == 1
    atualizada = db_session.get(BoutPrediction, segundo)
    assert atualizada is not None
    db_session.refresh(atualizada)
    assert atualizada.predicted_winner_id == segundo_previsto
    assert atualizada.prob_predicted_winner == 0.71
    assert atualizada.n_features == 42
    assert atualizada.source == SOURCE_API


def test_record_prediction_preserva_o_instante_e_a_origem_da_primeira_gravacao(
    db_session: Session,
) -> None:
    """Reprever a mesma luta atualiza a predição, **nunca** o eixo temporal do registro.

    ``predicted_at`` responde "quando esta predição foi feita", e é essa coluna que ordena a
    série da curva walk-forward (``apps.predictions.selectors.get_prediction_outcomes``).
    Refrescá-la no ``DO UPDATE`` a transformaria em "a última vez que alguém pediu a mesma
    predição" -- e, como ``GET /api/v1/predict/event/{event_id}`` grava, cada render da tela
    envelheceria o registro histórico para a frente. ``source`` é a mesma classe de dado: diz de
    onde veio a **primeira** gravação daquela predição, e sobrescrevê-la apagaria a procedência.

    A probabilidade e o vencedor previsto, ao contrário, são a mesma predição recomputada e
    podem mudar -- é o que separa esta trava de um simples ``DO NOTHING``.
    """
    bout_id = _seed_bout(db_session)
    fighter_id = _seed_fighter(db_session)

    prediction_id = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=fighter_id,
        prob_predicted_winner=0.55,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_WALK_FORWARD,
    )
    gravada = db_session.get(BoutPrediction, prediction_id)
    assert gravada is not None
    instante_original = gravada.predicted_at

    # Espera curta e explícita: sem ela, as duas gravações poderiam cair no mesmo microssegundo
    # e o teste passaria mesmo com o refresh de volta.
    time.sleep(0.01)

    regravada_id = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=fighter_id,
        prob_predicted_winner=0.83,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_API,
    )

    assert regravada_id == prediction_id
    regravada = db_session.get(BoutPrediction, regravada_id)
    assert regravada is not None
    db_session.refresh(regravada)
    assert regravada.prob_predicted_winner == 0.83
    assert regravada.predicted_at == instante_original
    assert regravada.source == SOURCE_WALK_FORWARD


def test_record_prediction_versao_nova_gera_linha_nova(db_session: Session) -> None:
    """CA-08: a mesma luta com ``model_version`` diferente acumula linhas.

    É essa acumulação que forma a série temporal por versão de modelo -- a curva de
    refino do walk-forward existe como dado por causa dela.
    """
    bout_id = _seed_bout(db_session)
    fighter_id = _seed_fighter(db_session)

    primeiro = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=fighter_id,
        prob_predicted_winner=0.55,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_WALK_FORWARD,
    )
    segundo = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=fighter_id,
        prob_predicted_winner=0.58,
        model_version=OUTRA_VERSAO,
        n_features=45,
        source=SOURCE_WALK_FORWARD,
    )

    assert primeiro != segundo
    assert _contagem(db_session) == 2


def test_record_prediction_carimba_source_e_instante_tz_aware(db_session: Session) -> None:
    """Rastreio: ``source`` é gravado como passado e ``predicted_at`` é tz-aware em UTC."""
    bout_id = _seed_bout(db_session)
    fighter_id = _seed_fighter(db_session)
    antes = datetime.now(UTC)

    prediction_id = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=fighter_id,
        prob_predicted_winner=0.5,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_WALK_FORWARD,
    )

    gravada = db_session.get(BoutPrediction, prediction_id)
    assert gravada is not None
    assert gravada.source == SOURCE_WALK_FORWARD
    assert gravada.predicted_at.tzinfo is not None
    assert antes <= gravada.predicted_at <= datetime.now(UTC)


def test_record_prediction_aceita_vencedor_previsto_nulo(db_session: Session) -> None:
    """Sem vencedor previsto a linha ainda é gravável (simetria com ``bouts.winner_id``)."""
    bout_id = _seed_bout(db_session)

    prediction_id = record_prediction(
        db_session,
        bout_id=bout_id,
        predicted_winner_id=None,
        prob_predicted_winner=0.5,
        model_version=VERSAO,
        n_features=39,
        source=SOURCE_API,
    )

    gravada = db_session.get(BoutPrediction, prediction_id)
    assert gravada is not None
    assert gravada.predicted_winner_id is None


def _predicoes(session: Session) -> list[BoutPrediction]:
    """Predições gravadas, em ordem determinística por luta."""
    stmt = select(BoutPrediction).order_by(BoutPrediction.bout_id)
    return list(session.scalars(stmt).all())


def test_predict_event_card_registra_uma_linha_por_luta_predita(
    db_session: Session, tmp_path: Path
) -> None:
    """Cada luta predita vira uma linha com ``source="api"`` e a versão do modelo do artefato.

    A luta impredizível do mesmo card **não** gera linha: sem palpite não há o que registrar.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    sem_historico = _make_fighter("Fighter Sem Historico", 195)
    db_session.add(sem_historico)
    db_session.flush()
    ids["Fighter Sem Historico"] = int(sem_historico.id)
    event_id = _seed_card(
        db_session,
        ids,
        [("Fighter Alpha", "Fighter Sem Historico"), ("Fighter Bravo", "Fighter Charlie")],
    )

    card = predict_event_card(db_session, event_id, tmp_path)

    artefato = load_artifact(tmp_path)
    predita = next(bout for bout in card.bouts if bout.unavailable_reason is None)
    impredizivel = next(bout for bout in card.bouts if bout.unavailable_reason is not None)
    gravadas = _predicoes(db_session)
    assert [linha.bout_id for linha in gravadas] == [predita.bout_id]
    assert impredizivel.bout_id not in {linha.bout_id for linha in gravadas}
    gravada = gravadas[0]
    assert gravada.source == SOURCE_API
    assert gravada.model_version == artefato.trained_at
    assert gravada.n_features == len(artefato.feature_names)
    assert gravada.predicted_winner_id == predita.predicted_winner_id
    assert gravada.prob_predicted_winner == pytest.approx(
        max(predita.prob_red_wins or 0.0, predita.prob_blue_wins or 0.0)
    )


def test_predict_event_card_e_idempotente_por_versao_de_modelo(
    db_session: Session, tmp_path: Path
) -> None:
    """CA-08 reusado: prever o mesmo card duas vezes mantém **uma** linha por luta."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(
        db_session, ids, [("Fighter Alpha", "Fighter Delta"), ("Fighter Bravo", "Fighter Charlie")]
    )

    predict_event_card(db_session, event_id, tmp_path)
    ids_primeira = [linha.id for linha in _predicoes(db_session)]
    predict_event_card(db_session, event_id, tmp_path)

    assert len(ids_primeira) == 2
    assert [linha.id for linha in _predicoes(db_session)] == ids_primeira
    assert _contagem(db_session) == 2


def test_predict_event_card_commita_a_transacao(db_session: Session, tmp_path: Path) -> None:
    """A gravação sobrevive a um rollback posterior -- ou seja, o service commitou.

    ``get_session`` (a dependência da API) nunca commita: a v1 nasceu somente-leitura. Sem o
    ``commit`` explícito do service, o endpoint responderia 200 sem ter gravado nada, porque a
    sessão do request é descartada no teardown. O ``rollback`` aqui simula esse descarte.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(db_session, ids, [("Fighter Alpha", "Fighter Delta")])

    predict_event_card(db_session, event_id, tmp_path)
    db_session.rollback()

    assert _contagem(db_session) == 1


def test_predict_event_card_roda_duas_execucoes_por_card(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O card inteiro custa **duas** passadas da pipeline -- uma por ordem de canto.

    O caminho ingênuo (chamar o serving por luta) custaria duas execuções por luta: 26 num card
    de 13. As duas ordens continuam separadas de propósito -- fundi-las faria cada lutador ter
    duas lutas sintéticas na mesma passada e corromperia as janelas ``shift(1)``.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(
        db_session, ids, [("Fighter Alpha", "Fighter Delta"), ("Fighter Bravo", "Fighter Charlie")]
    )
    execucoes: list[int] = []

    def contando(
        session: Session, pares: Sequence[tuple[int, int]], directory: Path
    ) -> CardPrediction:
        execucoes.append(len(pares))
        return predict_card(session, pares, directory)

    monkeypatch.setattr("apps.predictions.services.predict_card", contando)

    predict_event_card(db_session, event_id, tmp_path)

    assert execucoes == [2, 2]


def _seed_bout_sem_canto_azul(session: Session, event_id: int, fighter_id: int) -> int:
    """Acrescenta ao card uma luta mal formada (só o canto vermelho); devolve o ``bout_id``.

    Dado sujo plausível numa carga incremental: o par não veio completo. Serve para exercitar
    o ramo de elegibilidade que exige exatamente um canto vermelho e um azul.
    """
    bout = Bout(
        event_id=event_id,
        winner_id=None,
        method=BoutMethod.DECISION,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source="kaggle",
    )
    session.add(bout)
    session.flush()
    session.add(
        BoutFighter(bout_id=bout.id, fighter_id=fighter_id, corner=Corner.RED, source="kaggle")
    )
    session.flush()
    return int(bout.id)


def test_predict_event_card_marca_luta_sem_os_dois_cantos(
    db_session: Session, tmp_path: Path
) -> None:
    """Luta sem os dois cantos volta com motivo e sem palpite; as demais seguem preditas.

    Card mal formado é estado de **uma** luta, não erro do card: o par incompleto não vira
    luta sintética (o lote exige um vermelho e um azul) e também não vira linha em
    ``bout_predictions`` -- sem palpite não há o que afirmar.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(db_session, ids, [("Fighter Alpha", "Fighter Delta")])
    bout_id = _seed_bout_sem_canto_azul(db_session, event_id, ids["Fighter Bravo"])

    card = predict_event_card(db_session, event_id, tmp_path)

    predita, mal_formada = card.bouts
    assert mal_formada.bout_id == bout_id
    assert mal_formada.unavailable_reason is not None
    assert "sem os dois cantos" in mal_formada.unavailable_reason
    assert mal_formada.red is None
    assert mal_formada.blue is None
    assert mal_formada.prob_red_wins is None
    assert mal_formada.prob_blue_wins is None
    assert mal_formada.predicted_winner_id is None
    assert predita.unavailable_reason is None
    assert predita.prob_red_wins is not None
    assert [linha.bout_id for linha in _predicoes(db_session)] == [predita.bout_id]


def test_predict_event_card_marca_lutador_escalado_duas_vezes(
    db_session: Session, tmp_path: Path
) -> None:
    """Lutador em duas lutas do mesmo card: as duas saem com motivo, o card não cai.

    É o ramo que protege a invariante do lote (uma luta sintética por lutador): sem ele, os
    dois pares repetidos entrariam na mesma passada e a segunda sintética consumiria a
    primeira no ``shift(1)`` -- ou, no desenho atual, ``predict_card`` levantaria ``ValueError``
    e derrubaria o card inteiro. A luta sem repetição do mesmo card segue predita e é a única
    registrada.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    event_id = _seed_card(
        db_session,
        ids,
        [
            ("Fighter Alpha", "Fighter Delta"),
            ("Fighter Bravo", "Fighter Charlie"),
            ("Fighter Alpha", "Fighter Delta"),
        ],
    )
    repetidos = ", ".join(
        str(fighter_id) for fighter_id in sorted([ids["Fighter Alpha"], ids["Fighter Delta"]])
    )

    card = predict_event_card(db_session, event_id, tmp_path)

    primeira, predita, duplicada = card.bouts
    for indisponivel in (primeira, duplicada):
        assert indisponivel.unavailable_reason is not None
        assert f"id {repetidos} escalado" in indisponivel.unavailable_reason
        assert indisponivel.prob_red_wins is None
        assert indisponivel.prob_blue_wins is None
        assert indisponivel.predicted_winner_id is None
        # Os cantos continuam expostos: o consumidor vê a luta no card, só sem palpite.
        assert indisponivel.red is not None
        assert indisponivel.blue is not None
    assert predita.unavailable_reason is None
    assert predita.prob_red_wins is not None
    assert [linha.bout_id for linha in _predicoes(db_session)] == [predita.bout_id]
