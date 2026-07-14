"""Testes da predição de confronto hipotético A vs B "as-of agora" (serving, fase 2).

Cobrem ``analysis.predict.predict_matchup``: dado dois ``fighter_id``, constrói o vetor
de features do confronto reusando a engenharia point-in-time (rolling/trajetória/matchup),
carrega o modelo persistido e devolve a probabilidade de cada canto vencer.

Estratégia de teste (Postgres transacional, sem tocar quota externa): semeia um histórico
granular sintético, roda a pipeline real (``run_materialize`` -> ``bout_features``), treina e
persiste um modelo pequeno num ``tmp_path`` e prediz um confronto entre dois lutadores desse
histórico. As asserções são estruturais (probabilidades coerentes, vencedor entre os dois) e
de **determinismo** -- a predição não afirma acurácia (o teto real é a linha de mercado).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from analysis.model import run_training, save_artifact
from analysis.predict import MatchupPrediction, predict_matchup
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
