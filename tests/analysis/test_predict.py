"""Testes da predição de confronto hipotético A vs B "as-of agora" (serving, fase 2).

Cobrem ``analysis.predict.predict_matchup``: dado dois ``fighter_id``, constrói o vetor
de features do confronto reusando a engenharia point-in-time (rolling/trajetória/matchup),
carrega o modelo persistido e devolve a probabilidade de cada canto vencer.

Estratégia de teste (Postgres transacional, sem tocar quota externa): semeia um histórico
granular sintético, roda a pipeline real (``run_materialize`` -> ``bout_features``), treina e
persiste um modelo pequeno num ``tmp_path`` e prediz um confronto entre dois lutadores desse
histórico. As asserções são estruturais (probabilidades coerentes, vencedor entre os dois) e
de **determinismo** -- a predição não afirma acurácia (o teto real é a linha de mercado).

Cobrem também o recorte de "ter histórico" da Slice 08: não basta o lutador aparecer no
granular, é preciso ter luta **anterior a hoje**. O caso que discrimina isso é o do lutador
cuja única luta cadastrada é a futura do próprio card (``_seed_future_bout``) -- sem o recorte,
a predição sairia fabricada sobre um vetor inteiramente ``NaN``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from analysis.model import load_artifact, run_training, save_artifact
from analysis.predict import MatchupPrediction, predict_card, predict_matchup
from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.enums import Stance
from apps.fighters.models import Fighter
from ingestion.features.cli import run_materialize
from ingestion.normalize import normalize_name

# Roster sintético: nome -> alcance (cm). O alcance é o sinal preditivo (quem tem mais
# alcance vence), variado por canto para produzir alvos R e B (ambas as classes).
_ROSTER: dict[str, int] = {
    "Fighter Alpha": 205,
    "Fighter Bravo": 198,
    "Fighter Charlie": 191,
    "Fighter Delta": 184,
}


def _make_fighter(name: str, reach_cm: int) -> Fighter:
    """Lutador sintético com alcance/altura/base -- insumo das features de trajetória."""
    return Fighter(
        name=name,
        name_normalized=normalize_name(name),
        nickname=None,
        date_of_birth=date(1990, 1, 1),
        height_cm=180,
        reach_cm=reach_cm,
        stance=Stance.ORTHODOX,
        weight_kg=None,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
    )


def _bout_fighter(bout_id: int, fighter_id: int, corner: Corner, sig_strikes: int) -> BoutFighter:
    """Um canto com box-score granular mínimo (base das features de forma recente)."""
    return BoutFighter(
        bout_id=bout_id,
        fighter_id=fighter_id,
        corner=corner,
        knockdowns=0,
        sig_strikes_landed=sig_strikes,
        sig_strikes_attempted=sig_strikes * 2,
        takedowns_landed=1,
        takedowns_attempted=3,
        submission_attempts=0,
        control_time_seconds=60,
        source="kaggle",
    )


def _seed_history(session: Session) -> dict[str, int]:
    """Semeia um histórico granular round-robin e devolve o mapa nome -> ``fighter_id``.

    Cada par do roster se enfrenta várias vezes em datas crescentes; o de maior alcance
    vence, e a atribuição de canto alterna para produzir alvos R e B. Popula
    fighters/events/bouts/bout_fighters -- a fonte de verdade que a pipeline lê.
    """
    fighters = {name: _make_fighter(name, reach) for name, reach in _ROSTER.items()}
    session.add_all(fighters.values())
    session.flush()
    ids = {name: int(f.id) for name, f in fighters.items()}

    names = list(_ROSTER)
    pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1 :]]
    day = 0
    flip = False
    for _ in range(4):  # quatro rodadas de todos-contra-todos
        for name_x, name_y in pairs:
            # Alterna qual lutador ocupa o canto vermelho (varia o alvo R/B).
            red_name, blue_name = (name_x, name_y) if flip else (name_y, name_x)
            flip = not flip
            winner_name = name_x if _ROSTER[name_x] > _ROSTER[name_y] else name_y

            event = Event(
                name=f"UFC {day}",
                date=date(2019, 1, 1) + timedelta(days=30 * day),
                location=None,
                source="kaggle",
            )
            session.add(event)
            session.flush()
            bout = Bout(
                event_id=event.id,
                winner_id=ids[winner_name],
                method=BoutMethod.DECISION,
                round=3,
                ending_time_seconds=300,
                weight_class=None,
                source="kaggle",
            )
            session.add(bout)
            session.flush()
            session.add_all(
                [
                    _bout_fighter(bout.id, ids[red_name], Corner.RED, sig_strikes=40),
                    _bout_fighter(bout.id, ids[blue_name], Corner.BLUE, sig_strikes=35),
                ]
            )
            session.flush()
            day += 1
    return ids


def _train_and_persist(session: Session, directory: Path) -> None:
    """Materializa ``bout_features`` a partir do granular, treina e persiste o artefato."""
    run_materialize(session)
    result = run_training(session, test_fraction=0.25, random_state=0)
    save_artifact(result, directory=directory)


def _seed_future_bout(session: Session, red_fighter_id: int, blue_fighter_id: int) -> int:
    """Semeia uma luta ainda **não realizada** (evento datado à frente de hoje).

    Devolve o ``bout_id``. Sem vencedor e sem box-score: é o estado exato de um card por vir,
    já cadastrado no granular mas sem nada a extrair dele. A data é relativa a hoje, e não
    fixa, porque a "futuridade" da luta é justamente o que o cenário exerce. O ``method`` é
    placeholder (a coluna é NOT NULL no schema atual, que nasceu para eventos já ocorridos);
    nada na predição o consome.
    """
    futuro = datetime.now(UTC).date() + timedelta(days=90)
    event = Event(name="UFC Card Futuro", date=futuro, location=None, source="kaggle")
    session.add(event)
    session.flush()
    bout = Bout(
        event_id=event.id,
        winner_id=None,
        method=BoutMethod.DECISION,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source="kaggle",
    )
    session.add(bout)
    session.flush()
    session.add_all(
        [
            BoutFighter(
                bout_id=bout.id, fighter_id=red_fighter_id, corner=Corner.RED, source="kaggle"
            ),
            BoutFighter(
                bout_id=bout.id, fighter_id=blue_fighter_id, corner=Corner.BLUE, source="kaggle"
            ),
        ]
    )
    session.flush()
    return int(bout.id)


def test_predict_matchup_devolve_probabilidades_coerentes(
    db_session: Session, tmp_path: Path
) -> None:
    """A predição devolve probabilidades complementares e um vencedor entre os dois."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    prediction = predict_matchup(
        db_session, ids["Fighter Alpha"], ids["Fighter Delta"], directory=tmp_path
    )

    assert isinstance(prediction, MatchupPrediction)
    assert 0.0 <= prediction.prob_a_wins <= 1.0
    assert 0.0 <= prediction.prob_b_wins <= 1.0
    assert prediction.prob_a_wins + prediction.prob_b_wins == pytest.approx(1.0)
    assert prediction.predicted_winner_id in {ids["Fighter Alpha"], ids["Fighter Delta"]}
    # O vencedor previsto casa com o canto de maior probabilidade.
    esperado = (
        ids["Fighter Alpha"]
        if prediction.prob_a_wins >= prediction.prob_b_wins
        else ids["Fighter Delta"]
    )
    assert prediction.predicted_winner_id == esperado


def test_predict_matchup_e_deterministico(db_session: Session, tmp_path: Path) -> None:
    """Duas predições do mesmo confronto sobre o mesmo artefato são idênticas."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    primeiro = predict_matchup(
        db_session, ids["Fighter Alpha"], ids["Fighter Bravo"], directory=tmp_path
    )
    segundo = predict_matchup(
        db_session, ids["Fighter Alpha"], ids["Fighter Bravo"], directory=tmp_path
    )

    assert primeiro == segundo


def test_predict_matchup_coloca_a_no_canto_vermelho(db_session: Session, tmp_path: Path) -> None:
    """A convenção A = red é aplicada: trocar A e B recomputa o confronto do outro canto.

    O modelo aprende a vantagem do canto vermelho (baseline ~0.58), logo a predição **não** é
    simétrica por design -- A é sempre avaliado como o canto vermelho. O teste garante apenas
    que ambas as ordens produzem probabilidades válidas e complementares (não afirma simetria).
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    ab = predict_matchup(db_session, ids["Fighter Alpha"], ids["Fighter Delta"], directory=tmp_path)
    ba = predict_matchup(db_session, ids["Fighter Delta"], ids["Fighter Alpha"], directory=tmp_path)

    assert ab.prob_a_wins + ab.prob_b_wins == pytest.approx(1.0)
    assert ba.prob_a_wins + ba.prob_b_wins == pytest.approx(1.0)
    assert ab.predicted_winner_id in {ids["Fighter Alpha"], ids["Fighter Delta"]}
    assert ba.predicted_winner_id in {ids["Fighter Alpha"], ids["Fighter Delta"]}


def test_predict_matchup_lutador_sem_historico_falha_claro(
    db_session: Session, tmp_path: Path
) -> None:
    """Um ``fighter_id`` sem lutas no granular falha visível (não fabrica predição)."""
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    sem_historico = _make_fighter("Fighter Sem Historico", 195)
    db_session.add(sem_historico)
    db_session.flush()

    with pytest.raises(ValueError, match="histórico"):
        predict_matchup(db_session, ids["Fighter Alpha"], int(sem_historico.id), directory=tmp_path)


def test_predict_matchup_lutador_so_com_luta_futura_falha_claro(
    db_session: Session, tmp_path: Path
) -> None:
    """Lutador cuja ÚNICA luta cadastrada é **futura** não tem histórico -- falha, não fabrica.

    Regressão do bug corrigido na Slice 08: aparecer no granular não basta. A luta do card
    ainda não aconteceu, então todo o box-score dela é nulo e o ``shift(1)`` a descarta da
    própria linha. Sem o recorte por data de ``_fighters_with_past_bouts``, o estreante passaria
    no teste de histórico **pela própria luta que se quer prever** e a predição sairia como um
    0.5 fabricado sobre um vetor inteiramente ``NaN``. Distinto de
    ``test_predict_matchup_lutador_sem_historico_falha_claro``, onde o lutador não aparece no
    granular de forma alguma -- aquele cenário passaria mesmo com o recorte revertido.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    estreante = _make_fighter("Fighter Estreante", 195)
    db_session.add(estreante)
    db_session.flush()
    _seed_future_bout(db_session, ids["Fighter Alpha"], int(estreante.id))

    with pytest.raises(ValueError, match="histórico"):
        predict_matchup(db_session, ids["Fighter Alpha"], int(estreante.id), directory=tmp_path)


def test_predict_card_equivale_as_chamadas_individuais(db_session: Session, tmp_path: Path) -> None:
    """Predizer N pares numa passada dá as MESMAS probabilidades que N chamadas isoladas.

    É a prova de que reaproveitar a frame longa entre as lutas do card não contamina as
    features: cada lutador tem uma única luta sintética no lote, então o ``shift(1)`` por
    lutador enxerga exatamente o mesmo histórico que enxergaria numa chamada isolada.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    pares = [
        (ids["Fighter Alpha"], ids["Fighter Delta"]),
        (ids["Fighter Bravo"], ids["Fighter Charlie"]),
    ]

    card = predict_card(db_session, pares, directory=tmp_path)

    assert card.model_version == load_artifact(tmp_path).trained_at
    assert card.n_features == len(load_artifact(tmp_path).feature_names)
    assert [(m.fighter_a_id, m.fighter_b_id) for m in card.matchups] == pares
    for par, previsto in zip(pares, card.matchups, strict=True):
        individual = predict_matchup(db_session, par[0], par[1], directory=tmp_path)
        assert previsto.prob_a_wins == pytest.approx(individual.prob_a_wins)
        assert previsto.missing_history_ids == ()


def test_predict_card_marca_par_sem_historico_sem_afetar_os_demais(
    db_session: Session, tmp_path: Path
) -> None:
    """Um par impredizível volta marcado; os outros pares do mesmo lote seguem preditos.

    Diferença deliberada em relação a ``predict_matchup``: no lote, a ausência de histórico
    é **estado de um item**, não erro da chamada -- uma luta impredizível não derruba o card.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)
    sem_historico = _make_fighter("Fighter Sem Historico", 195)
    db_session.add(sem_historico)
    db_session.flush()
    orfao = int(sem_historico.id)

    card = predict_card(
        db_session,
        [(ids["Fighter Alpha"], orfao), (ids["Fighter Bravo"], ids["Fighter Charlie"])],
        directory=tmp_path,
    )

    impredizivel, predito = card.matchups
    assert impredizivel.prob_a_wins is None
    assert impredizivel.missing_history_ids == (orfao,)
    assert predito.prob_a_wins is not None
    assert predito.missing_history_ids == ()


def test_predict_card_recusa_lutador_repetido_no_lote(db_session: Session, tmp_path: Path) -> None:
    """Um ``fighter_id`` em dois pares do mesmo lote levanta ``ValueError`` (invariante).

    Duas lutas sintéticas do mesmo lutador na mesma passada fariam a segunda consumir a
    primeira via ``shift(1)`` -- e a primeira tem box-score todo nulo. Em vez de devolver
    features corrompidas em silêncio, o lote falha visível.
    """
    ids = _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    with pytest.raises(ValueError, match="mais de uma luta"):
        predict_card(
            db_session,
            [
                (ids["Fighter Alpha"], ids["Fighter Delta"]),
                (ids["Fighter Alpha"], ids["Fighter Bravo"]),
            ],
            directory=tmp_path,
        )


def test_predict_card_sem_pares_nao_toca_o_granular(db_session: Session, tmp_path: Path) -> None:
    """Lote vazio devolve só a identidade do modelo -- a pipeline não roda sem pares.

    Sustenta o card vazio da API: um evento sem lutas ainda responde a versão do modelo,
    sem pagar a leitura do granular nem quebrar o pivô com uma frame sintética vazia.
    """
    _seed_history(db_session)
    _train_and_persist(db_session, tmp_path)

    card = predict_card(db_session, [], directory=tmp_path)

    assert card.matchups == []
    assert card.model_version == load_artifact(tmp_path).trained_at
