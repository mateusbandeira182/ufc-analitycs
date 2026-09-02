"""Testes da materialização da matriz de confronto em ``bout_features`` -- Plano 005-05.

Cobrem a Slice 05 (SPEC 005, M4): persistir o cache reconstrutível ``bout_features`` a
partir da ``MatchupMatrix`` da Slice 04, via upsert idempotente por ``bout_id``. As
asserções são sobre o **estado do banco** (o sinal demonstrável da ingestão), contra o
Postgres de teste transacional.

Reconciliação com o contrato real da Slice 04: ``materialize_features`` recebe a
``MatchupMatrix`` (dataclass com ``frame`` + ``feature_columns`` + ``target_column``), não
um ``DataFrame`` cru. O snippet ilustrativo do plano derivava as features excluindo apenas
``bout_id``/``winner_corner`` -- o que despejaria identidade/contexto/desfecho (``result_a``,
``method_a``, ``fighter_id_a``...) no JSONB e vazaria. A ``MatchupMatrix`` já carrega a lista
exata de ``feature_columns`` (``*_a``/``*_b``/``*_diff``), que é a fonte da verdade do que é
feature. O alvo ``winner_corner`` chega como ``"R"``/``"B"`` (matchup) e é mapeado para o
enum ``Corner`` (``red``/``blue``) na borda.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analysis.dataset import build_dataset, read_bout_features
from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.features.models import BoutFeatures
from apps.fighters.models import Fighter
from ingestion.features.cli import run_materialize
from ingestion.features.division import (
    DIVISION_FINISH_RATE_PRIOR,
    IS_WOMENS_DIVISION,
    WEIGHT_CLASS_LBS,
)
from ingestion.features.matchup import (
    FORMAT_FEATURES,
    IS_TITLE_BOUT,
    SCHEDULED_ROUNDS,
    MatchupMatrix,
)
from ingestion.features.materialize import SOURCE, _to_corner, materialize_features
from ingestion.features.rolling import (
    BODY_ACCURACY_R3,
    CAREER_MINUTES_BEFORE,
    CLINCH_ACCURACY_R3,
    CONTROL_TIME_AVG_TREND,
    DISTANCE_ACCURACY_R3,
    FIVE_ROUND_BOUTS_BEFORE,
    GROUND_ACCURACY_R3,
    HEAD_ACCURACY_R3,
    KNOCKDOWNS_AVG_R3,
    KNOCKDOWNS_PM_R3,
    KO_LOSSES_PRIOR,
    LEG_ACCURACY_R3,
    ROUND1_SIG_STRIKE_SHARE_R3,
    SHARE_HEAD_R3,
    SIG_STRIKE_ACCURACY_R3,
    SIG_STRIKES_ABSORBED_PM_TREND,
    SIG_STRIKES_LANDED_PM_TREND,
    SUBMISSION_ATTEMPTS_AVG_R3,
    SUBMISSION_LOSSES_PRIOR,
    TAKEDOWN_ACCURACY_R3,
    TAKEDOWN_DEFENSE_TREND,
    TAKEDOWNS_LANDED_AVG_TREND,
    TOTAL_TO_SIG_STRIKE_RATIO_R3,
)
from ingestion.normalize import normalize_name

_FEATURE_COLUMNS = ["sig_strikes_pm_asof_a", "sig_strikes_pm_asof_b", "sig_strikes_pm_asof_diff"]

# As 12 bases as-of do bloco A da SPEC 009 (Slice 01): precisão (8), poder (2), ameaça de
# grappling (1) e trabalho fora do golpe significativo (1).
_BLOCO_A_BASES: tuple[str, ...] = (
    SIG_STRIKE_ACCURACY_R3,
    HEAD_ACCURACY_R3,
    BODY_ACCURACY_R3,
    LEG_ACCURACY_R3,
    DISTANCE_ACCURACY_R3,
    CLINCH_ACCURACY_R3,
    GROUND_ACCURACY_R3,
    TAKEDOWN_ACCURACY_R3,
    KNOCKDOWNS_PM_R3,
    KNOCKDOWNS_AVG_R3,
    SUBMISSION_ATTEMPTS_AVG_R3,
    TOTAL_TO_SIG_STRIKE_RATIO_R3,
)


def _seed_bout(db_session: Session, *, decided: bool = True) -> int:
    """Semeia uma luta com dois cantos e devolve o ``bout_id`` (FK de ``bout_features``).

    ``decided=True`` define um vencedor (KO/TKO); ``decided=False`` deixa ``winner_id``
    nulo (empate/no contest), útil para o alvo nulo.
    """
    red, blue = (
        Fighter(
            name=name,
            name_normalized=normalize_name(name),
            nickname=None,
            date_of_birth=None,
            height_cm=None,
            reach_cm=None,
            stance=None,
            wins=0,
            losses=0,
            draws=0,
            source="kaggle",
        )
        for name in ("Red Corner", "Blue Corner")
    )
    db_session.add_all([red, blue])
    event = Event(name="UFC Test", date=date(2024, 1, 1), location=None, source="kaggle")
    db_session.add(event)
    db_session.flush()
    bout = Bout(
        event_id=event.id,
        winner_id=red.id if decided else None,
        method=BoutMethod.KO_TKO if decided else BoutMethod.NO_CONTEST,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source="kaggle",
    )
    db_session.add(bout)
    db_session.flush()
    db_session.add_all(
        [
            BoutFighter(
                bout_id=bout.id,
                fighter_id=red.id,
                corner=Corner.RED,
                knockdowns=None,
                sig_strikes_landed=None,
                sig_strikes_attempted=None,
                takedowns_landed=None,
                takedowns_attempted=None,
                submission_attempts=None,
                control_time_seconds=None,
                source="kaggle",
            ),
            BoutFighter(
                bout_id=bout.id,
                fighter_id=blue.id,
                corner=Corner.BLUE,
                knockdowns=None,
                sig_strikes_landed=None,
                sig_strikes_attempted=None,
                takedowns_landed=None,
                takedowns_attempted=None,
                submission_attempts=None,
                control_time_seconds=None,
                source="kaggle",
            ),
        ]
    )
    db_session.flush()
    return int(bout.id)


def _matrix(rows: list[dict[str, object]]) -> MatchupMatrix:
    """Monta uma ``MatchupMatrix`` mínima a partir de linhas bout-level à mão."""
    frame = pd.DataFrame(rows)
    return MatchupMatrix(
        frame=frame,
        feature_columns=_FEATURE_COLUMNS,
        target_column="winner_corner",
        excluded_no_result=0,
        red_corner_win_rate=0.5,
    )


def test_materialize_grava_uma_linha_por_bout_com_features_e_alvo(db_session: Session) -> None:
    """CA-02: uma linha por bout; features JSONB sem o alvo; ``winner_corner`` -> enum."""
    bout_id = _seed_bout(db_session)
    matrix = _matrix(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": 4.0,
                "sig_strikes_pm_asof_b": 3.0,
                "sig_strikes_pm_asof_diff": 1.0,
                "winner_corner": "R",
            }
        ]
    )

    inserted = materialize_features(db_session, matrix)

    assert inserted == 1
    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    assert row.features == {
        "sig_strikes_pm_asof_a": 4.0,
        "sig_strikes_pm_asof_b": 3.0,
        "sig_strikes_pm_asof_diff": 1.0,
    }
    # O alvo é separado das features (não entra no JSONB) e mapeado para o enum Corner.
    assert "winner_corner" not in row.features
    assert row.target_winner_corner is Corner.RED


def test_materialize_registra_source_e_generated_at_tz_aware(db_session: Session) -> None:
    """CA-03: cada linha carimba ``source`` e ``generated_at`` timezone-aware em UTC."""
    bout_id = _seed_bout(db_session)
    antes = datetime.now(UTC)
    matrix = _matrix(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": 1.0,
                "sig_strikes_pm_asof_b": 2.0,
                "sig_strikes_pm_asof_diff": -1.0,
                "winner_corner": "B",
            }
        ]
    )

    materialize_features(db_session, matrix)

    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    assert row.source == SOURCE
    assert row.generated_at.tzinfo is not None
    assert row.generated_at.utcoffset() == UTC.utcoffset(None)
    assert row.generated_at >= antes


def test_materialize_nan_vira_null_explicito_no_jsonb(db_session: Session) -> None:
    """CA-03: ``NaN``/``NA`` do Pandas viram ``null`` explícito no JSONB (estreia sem histórico)."""
    bout_id = _seed_bout(db_session)
    matrix = _matrix(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": float("nan"),
                "sig_strikes_pm_asof_b": pd.NA,
                "sig_strikes_pm_asof_diff": 2.0,
                "winner_corner": "R",
            }
        ]
    )

    materialize_features(db_session, matrix)

    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    assert row.features["sig_strikes_pm_asof_a"] is None
    assert row.features["sig_strikes_pm_asof_b"] is None
    assert row.features["sig_strikes_pm_asof_diff"] == 2.0


def test_materialize_alvo_nulo_para_nc_draw(db_session: Session) -> None:
    """CA-02/CA-03: um bout sem vencedor definido (``winner_corner`` NA) grava alvo nulo."""
    bout_id = _seed_bout(db_session, decided=False)
    matrix = _matrix(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": 1.0,
                "sig_strikes_pm_asof_b": 1.0,
                "sig_strikes_pm_asof_diff": 0.0,
                "winner_corner": pd.NA,
            }
        ]
    )

    materialize_features(db_session, matrix)

    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    assert row.target_winner_corner is None


def test_materialize_converte_numpy_para_tipos_nativos(db_session: Session) -> None:
    """CA-03: tipos numpy/pandas (``Int64``) viram tipos Python nativos serializáveis em JSON."""
    bout_id = _seed_bout(db_session)
    frame = pd.DataFrame(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": 4.0,
                "sig_strikes_pm_asof_b": 3.0,
                "sig_strikes_pm_asof_diff": 1,
                "winner_corner": "R",
            }
        ]
    )
    # Coluna de diferencial como inteiro nullable (Int64) -- espelha o dtype real do matchup.
    frame["sig_strikes_pm_asof_diff"] = frame["sig_strikes_pm_asof_diff"].astype("Int64")
    matrix = MatchupMatrix(
        frame=frame,
        feature_columns=_FEATURE_COLUMNS,
        target_column="winner_corner",
        excluded_no_result=0,
        red_corner_win_rate=1.0,
    )

    materialize_features(db_session, matrix)

    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    valor = row.features["sig_strikes_pm_asof_diff"]
    assert valor == 1
    assert type(valor) is int  # tipo Python nativo, não numpy/pandas


def _snapshot_sem_generated_at(db_session: Session) -> dict[int, tuple[object, object, str]]:
    """Conteúdo determinístico de ``bout_features`` (features/alvo/source), sem ``generated_at``."""
    linhas = db_session.execute(select(BoutFeatures)).scalars().all()
    return {
        linha.bout_id: (linha.features, linha.target_winner_corner, linha.source)
        for linha in linhas
    }


def test_materialize_e_idempotente_e_nao_toca_o_granular(db_session: Session) -> None:
    """CA-05: rodar 2x mantém contagem e conteúdo; ``bouts``/``bout_fighters`` intocados."""
    bout_id = _seed_bout(db_session)
    matrix = _matrix(
        [
            {
                "bout_id": bout_id,
                "sig_strikes_pm_asof_a": 4.0,
                "sig_strikes_pm_asof_b": 3.0,
                "sig_strikes_pm_asof_diff": 1.0,
                "winner_corner": "R",
            }
        ]
    )
    bouts_antes = db_session.scalar(select(func.count()).select_from(Bout))
    bout_fighters_antes = db_session.scalar(select(func.count()).select_from(BoutFighter))

    materialize_features(db_session, matrix)
    primeiro = _snapshot_sem_generated_at(db_session)
    materialize_features(db_session, matrix)
    segundo = _snapshot_sem_generated_at(db_session)

    # Mesma contagem e mesmo conteúdo (excluindo generated_at, que pode ser refrescado).
    assert db_session.scalar(select(func.count()).select_from(BoutFeatures)) == 1
    assert primeiro == segundo
    # Granular intocado: nenhuma escrita/alteração em bouts/bout_fighters.
    assert db_session.scalar(select(func.count()).select_from(Bout)) == bouts_antes
    assert db_session.scalar(select(func.count()).select_from(BoutFighter)) == bout_fighters_antes


def test_materialize_atualiza_conteudo_no_reprocessamento(db_session: Session) -> None:
    """CA-05: um rebuild com features diferentes atualiza a linha (upsert), sem duplicar."""
    bout_id = _seed_bout(db_session)
    base_row: dict[str, object] = {
        "bout_id": bout_id,
        "sig_strikes_pm_asof_a": 4.0,
        "sig_strikes_pm_asof_b": 3.0,
        "sig_strikes_pm_asof_diff": 1.0,
        "winner_corner": "R",
    }

    materialize_features(db_session, _matrix([base_row]))
    atualizado = {**base_row, "sig_strikes_pm_asof_diff": 9.0}
    materialize_features(db_session, _matrix([atualizado]))

    assert db_session.scalar(select(func.count()).select_from(BoutFeatures)) == 1
    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    db_session.refresh(row)
    assert row.features["sig_strikes_pm_asof_diff"] == 9.0


def test_run_materialize_roda_pipeline_completa_e_popula_bout_features(
    db_session: Session,
) -> None:
    """CA-04: ``run_materialize`` roda a pipeline (long->rolling->trajectory->matchup) e persiste.

    Semeia uma luta decidida e confirma que a tabela derivada é populada com uma linha por
    bout decidido, com o alvo mapeado -- observável por consulta ao banco (sinal da ingestão).
    """
    bout_id = _seed_bout(db_session)

    inseridas = run_materialize(db_session)

    assert inseridas == 1
    row = db_session.get(BoutFeatures, bout_id)
    assert row is not None
    assert row.source == SOURCE
    assert row.target_winner_corner is Corner.RED
    # Rodar de novo é idempotente: mesma contagem (upsert por bout_id).
    run_materialize(db_session)
    assert db_session.scalar(select(func.count()).select_from(BoutFeatures)) == 1


def _seed_fighter(db_session: Session, name: str) -> Fighter:
    """Semeia um lutador mínimo e devolve o model já com id."""
    fighter = Fighter(
        name=name,
        name_normalized=normalize_name(name),
        nickname=None,
        date_of_birth=date(1990, 1, 1),
        height_cm=None,
        reach_cm=None,
        stance=None,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
    )
    db_session.add(fighter)
    db_session.flush()
    return fighter


def _seed_two_bouts_with_splits_and_rounds(db_session: Session) -> int:
    """Semeia o lutador A com duas lutas (splits + round-a-round na 1a); devolve o bout_id da 2a.

    A vence as duas por decisão contra oponentes distintos, splits preenchidos nos dois
    cantos de A, e round-a-round só na 1a luta (r1=15, r2=5 -> round1 share 0.75). A 2a luta
    passa a ter features as-of de perfil de striking e de dinâmica por round.

    O canto de A também carrega as colunas cruas do bloco A da SPEC 009 (as tentadas, os
    knockdowns e as tentativas de finalização), para que a 2a luta tenha valor numérico real
    nas 12 features de precisão/poder/ameaça/trabalho total: cada luta de A conecta 30 de 60
    golpes significativos (cabeça 20/40, corpo 5/10, perna 5/10, distância 18/36, clinch
    6/12, solo 6/12), acerta 1 de 2 quedas, marca 1 knockdown e 2 tentativas de finalização,
    em 15 minutos de luta (3 rounds cheios).
    """
    a = _seed_fighter(db_session, "Fighter A")
    b = _seed_fighter(db_session, "Opponent B")
    c = _seed_fighter(db_session, "Opponent C")
    evt1 = Event(name="UFC 1: Test", date=date(2023, 1, 1), location=None, source="kaggle")
    evt2 = Event(name="UFC 2: Test", date=date(2023, 6, 1), location=None, source="kaggle")
    db_session.add_all([evt1, evt2])
    db_session.flush()

    second_bout_id = 0
    for event, opponent in ((evt1, b), (evt2, c)):
        bout = Bout(
            event_id=event.id,
            winner_id=a.id,
            method=BoutMethod.KO_TKO if event is evt1 else BoutMethod.DECISION,
            round=3,
            ending_time_seconds=300,
            weight_class="Lightweight",
            title_bout=False,
            scheduled_rounds=3,
            source="kaggle",
        )
        db_session.add(bout)
        db_session.flush()
        second_bout_id = int(bout.id)
        a_corner = BoutFighter(
            bout_id=bout.id,
            fighter_id=a.id,
            corner=Corner.RED,
            knockdowns=1,
            sig_strikes_landed=30,
            sig_strikes_attempted=60,
            takedowns_landed=1,
            takedowns_attempted=2,
            submission_attempts=2,
            control_time_seconds=None,
            total_strikes_landed=30,
            total_strikes_attempted=None,
            head_landed=20,
            head_attempted=40,
            body_landed=5,
            body_attempted=10,
            leg_landed=5,
            leg_attempted=10,
            distance_landed=18,
            distance_attempted=36,
            clinch_landed=6,
            clinch_attempted=12,
            ground_landed=6,
            ground_attempted=12,
            reversals=None,
            source="kaggle",
        )
        db_session.add_all(
            [
                a_corner,
                BoutFighter(
                    bout_id=bout.id,
                    fighter_id=opponent.id,
                    corner=Corner.BLUE,
                    knockdowns=None,
                    sig_strikes_landed=10,
                    sig_strikes_attempted=None,
                    takedowns_landed=None,
                    takedowns_attempted=None,
                    submission_attempts=None,
                    control_time_seconds=None,
                    total_strikes_landed=None,
                    total_strikes_attempted=None,
                    head_landed=None,
                    head_attempted=None,
                    body_landed=None,
                    body_attempted=None,
                    leg_landed=None,
                    leg_attempted=None,
                    distance_landed=None,
                    distance_attempted=None,
                    clinch_landed=None,
                    clinch_attempted=None,
                    ground_landed=None,
                    ground_attempted=None,
                    reversals=None,
                    source="kaggle",
                ),
            ]
        )
        db_session.flush()
        if event is evt1:
            db_session.add_all(
                [
                    BoutFighterRound(
                        bout_fighter_id=a_corner.id,
                        round=rnd,
                        knockdowns=None,
                        sig_strikes_landed=sig,
                        sig_strikes_attempted=None,
                        takedowns_landed=None,
                        takedowns_attempted=None,
                        submission_attempts=None,
                        control_time_seconds=None,
                        total_strikes_landed=None,
                        total_strikes_attempted=None,
                        head_landed=None,
                        head_attempted=None,
                        body_landed=None,
                        body_attempted=None,
                        leg_landed=None,
                        leg_attempted=None,
                        distance_landed=None,
                        distance_attempted=None,
                        clinch_landed=None,
                        clinch_attempted=None,
                        ground_landed=None,
                        ground_attempted=None,
                        reversals=None,
                        source="cito",
                    )
                    for rnd, sig in {1: 15, 2: 5}.items()
                ]
            )
            db_session.flush()
    return second_bout_id


def test_run_materialize_grava_features_novas_e_e_idempotente(db_session: Session) -> None:
    """CA-03: as features novas (share/round1) entram no payload; re-materializar é idempotente.

    A 2a luta de A tem perfil de striking as-of (``share_head_r3_a`` = 20/30) e dinâmica por
    round (``round1_sig_strike_share_r3_a`` = 0.75). Re-executar mantém a contagem (upsert por
    ``bout_id``) e ``NaN`` vira ``None`` no JSONB (nunca ``inf``).
    """
    second_bout_id = _seed_two_bouts_with_splits_and_rounds(db_session)

    run_materialize(db_session)
    count_primeiro = db_session.scalar(select(func.count()).select_from(BoutFeatures))

    row = db_session.get(BoutFeatures, second_bout_id)
    assert row is not None
    # As features novas estão no payload da 2a luta (as-of, só a 1a como histórico).
    assert row.features[f"{SHARE_HEAD_R3}_a"] == pytest.approx(20 / 30)
    assert row.features[f"{ROUND1_SIG_STRIKE_SHARE_R3}_a"] == pytest.approx(0.75)
    # Os splits raw da luta corrente NÃO vazam para o payload (anti-leakage).
    assert "head_landed_a" not in row.features
    assert "reversals_a" not in row.features
    # NaN -> None explícito (o canto oposto é estreia -> as-of ausente).
    assert row.features[f"{SHARE_HEAD_R3}_b"] is None
    # inf jamais entra no JSONB.
    for valor in row.features.values():
        assert valor != float("inf")

    # Re-materializar mantém a contagem (idempotência por bout_id).
    run_materialize(db_session)
    count_segundo = db_session.scalar(select(func.count()).select_from(BoutFeatures))
    assert count_primeiro == count_segundo


def test_payload_do_bloco_a_e_numerico_ou_nulo_e_sobrevive_ao_dataset(
    db_session: Session,
) -> None:
    """CA-06 da SPEC 009: as 36 colunas do bloco A chegam ao JSONB e sobrevivem ao dataset.

    Teste de aceitação da Slice 01: cada uma das 12 bases (precisão, poder, ameaça de
    grappling, trabalho fora do significativo) vira o trio ``_a``/``_b``/``_diff`` no payload
    de ``bout_features``, sempre como número JSON ou ``null`` -- jamais string, jamais
    ``NaN`` (que não é JSON válido). Os valores da 2a luta de A são as-of da 1a: 30/60 de
    precisão significativa, 1 knockdown por luta, 2 tentativas de finalização por luta e
    razão total/significativo de 30/30.

    Sobre ``build_dataset``: neste fixture mínimo o canto azul é **sempre** estreante (dois
    oponentes distintos, um por luta), então as colunas ``_b``/``_diff`` do bloco nascem 100%
    nulas e o descarte delas é a degradação já testada de coluna toda-nula. A asserção
    aqui é sobre as colunas com dado -- nenhuma delas pode sumir em silêncio, que é
    exatamente o modo de falha que escondeu 21 features do M5 por meses.
    """
    second_bout_id = _seed_two_bouts_with_splits_and_rounds(db_session)

    run_materialize(db_session)

    row = db_session.get(BoutFeatures, second_bout_id)
    assert row is not None
    esperadas = {f"{base}{sufixo}" for base in _BLOCO_A_BASES for sufixo in ("_a", "_b", "_diff")}
    assert len(esperadas) == 36
    assert esperadas <= set(row.features)
    for coluna in esperadas:
        valor = row.features[coluna]
        assert valor is None or isinstance(valor, int | float), coluna
        assert not (isinstance(valor, float) and math.isnan(valor)), coluna

    # Valores as-of da 2a luta (derivados só da 1a), no canto vermelho.
    assert row.features[f"{SIG_STRIKE_ACCURACY_R3}_a"] == pytest.approx(0.5)
    assert row.features[f"{HEAD_ACCURACY_R3}_a"] == pytest.approx(0.5)
    assert row.features[f"{TAKEDOWN_ACCURACY_R3}_a"] == pytest.approx(0.5)
    assert row.features[f"{KNOCKDOWNS_AVG_R3}_a"] == pytest.approx(1.0)
    assert row.features[f"{KNOCKDOWNS_PM_R3}_a"] == pytest.approx(1 / 15)
    assert row.features[f"{SUBMISSION_ATTEMPTS_AVG_R3}_a"] == pytest.approx(2.0)
    assert row.features[f"{TOTAL_TO_SIG_STRIKE_RATIO_R3}_a"] == pytest.approx(1.0)

    # Nenhuma coluna do bloco com dado é descartada em silêncio pelo dataset preditivo.
    dataset = build_dataset(read_bout_features(db_session))
    com_dado = {f"{base}_a" for base in _BLOCO_A_BASES}
    assert com_dado <= set(dataset.feature_names)


def test_to_corner_valor_inesperado_levanta_value_error_claro() -> None:
    """Alvo fora de ``R``/``B`` falha com ``ValueError`` claro (não ``KeyError`` sem contexto)."""
    assert _to_corner("R") is Corner.RED
    assert _to_corner("B") is Corner.BLUE
    assert _to_corner(None) is None
    with pytest.raises(ValueError, match="inesperado"):
        _to_corner("X")


def test_features_de_divisao_chegam_ao_jsonb_como_coluna_unica_e_sobrevivem_ao_dataset(
    db_session: Session,
) -> None:
    """CA-13/CA-14 da SPEC 009: as três features de divisão vão do granular ao treino.

    Teste de aceitação da Slice 02, ponta a ponta pelo caminho real (``run_materialize``):

    1. as três chegam ao payload JSONB como **coluna única** -- nem ``_a``/``_b``, nem
       ``_diff`` degenerado, nem o rótulo cru ``weight_class`` (string, que seria descartada
       em silêncio por ``analysis.dataset._numeric_feature_columns``);
    2. os valores são número ou ``null``, jamais string, jamais ``NaN`` (que não é JSON
       válido);
    3. nenhuma delas é descartada por ``build_dataset`` -- é exatamente o modo de falha
       silencioso que escondeu 21 features do M5 por meses.

    Valores esperados na 2a luta de A (peso-leve, masculino): 155 libras, 0 de divisão
    feminina, e taxa base 1,0 (a única luta anterior da divisão terminou em KO/TKO).
    """
    second_bout_id = _seed_two_bouts_with_splits_and_rounds(db_session)

    run_materialize(db_session)

    row = db_session.get(BoutFeatures, second_bout_id)
    assert row is not None
    esperadas = (WEIGHT_CLASS_LBS, IS_WOMENS_DIVISION, DIVISION_FINISH_RATE_PRIOR)
    for coluna in esperadas:
        assert coluna in row.features, coluna
        valor = row.features[coluna]
        assert isinstance(valor, int | float), coluna
        assert not (isinstance(valor, float) and math.isnan(valor)), coluna
        # Coluna ÚNICA: nada de par de canto nem de diferencial degenerado no payload.
        for sufixo in ("_a", "_b", "_diff"):
            assert f"{coluna}{sufixo}" not in row.features, f"{coluna}{sufixo}"
    # O rótulo cru é contexto, não feature: nenhuma das três entra como PAR de canto.
    for sufixo in ("_a", "_b", "_diff"):
        assert f"weight_class{sufixo}" not in row.features
        assert f"scheduled_rounds{sufixo}" not in row.features
        assert f"title_bout{sufixo}" not in row.features
    # Nus, a divisão e o indicador de título também ficam fora (as features deles têm nome
    # próprio). ``scheduled_rounds`` é a exceção deliberada da Slice 03: a coluna única É a
    # feature bout-level do bloco B2, testada em
    # ``test_features_de_formato_chegam_ao_jsonb_e_sobrevivem_ao_dataset``.
    assert "weight_class" not in row.features
    assert "title_bout" not in row.features

    assert row.features[WEIGHT_CLASS_LBS] == pytest.approx(155.0)
    assert row.features[IS_WOMENS_DIVISION] == pytest.approx(0.0)
    assert row.features[DIVISION_FINISH_RATE_PRIOR] == pytest.approx(1.0)

    # Nenhuma das três é descartada em silêncio a caminho do treino.
    dataset = build_dataset(read_bout_features(db_session))
    assert set(esperadas) <= set(dataset.feature_names)


def test_features_de_formato_chegam_ao_jsonb_e_sobrevivem_ao_dataset(
    db_session: Session,
) -> None:
    """CA-13/CA-14 da SPEC 009: as 5 colunas do bloco B2 vão do granular ao treino.

    Teste de aceitação da Slice 03, ponta a ponta pelo caminho real (``run_materialize``):

    1. ``is_title_bout`` e ``scheduled_rounds`` chegam ao payload como **coluna única** --
       sem ``_a``/``_b`` e sem ``_diff`` degenerado --, e ``five_round_bouts_before`` chega
       como o trio de canto (é feature do lutador, não da luta);
    2. os valores são número ou ``null``, jamais string, jamais ``NaN`` (que não é JSON
       válido);
    3. nenhuma das cinco é descartada em silêncio por ``build_dataset`` -- e a de nome
       colidente (``scheduled_rounds``, homônima da coluna crua excluída) é justamente a que
       sumiria sem a precedência da allowlist.

    A 2a luta de A vira disputa de cinturão de cinco rounds; a 1a foi de três. Logo, na 2a:
    ``is_title_bout`` = 1, ``scheduled_rounds`` = 5 e ``five_round_bouts_before_a`` = **0**
    -- zero legítimo (A tem histórico e nunca fez cinco rounds), distinto do ``null`` do
    canto azul, que é estreante.
    """
    second_bout_id = _seed_two_bouts_with_splits_and_rounds(db_session)
    segunda = db_session.get(Bout, second_bout_id)
    assert segunda is not None
    segunda.title_bout = True
    segunda.scheduled_rounds = 5
    db_session.flush()

    run_materialize(db_session)

    row = db_session.get(BoutFeatures, second_bout_id)
    assert row is not None
    esperadas = [
        *FORMAT_FEATURES,
        *(f"{FIVE_ROUND_BOUTS_BEFORE}{sufixo}" for sufixo in ("_a", "_b", "_diff")),
    ]
    assert len(esperadas) == 5
    for coluna in esperadas:
        assert coluna in row.features, coluna
        valor = row.features[coluna]
        assert valor is None or isinstance(valor, int | float), coluna
        assert not (isinstance(valor, float) and math.isnan(valor)), coluna
    # Bout-level é coluna ÚNICA: nada de par de canto nem de diferencial degenerado.
    for coluna in FORMAT_FEATURES:
        for sufixo in ("_a", "_b", "_diff"):
            assert f"{coluna}{sufixo}" not in row.features, f"{coluna}{sufixo}"

    assert row.features[IS_TITLE_BOUT] == pytest.approx(1.0)
    assert row.features[SCHEDULED_ROUNDS] == pytest.approx(5.0)
    # Zero legítimo no canto vermelho (a única luta anterior de A foi de 3 rounds)...
    assert row.features[f"{FIVE_ROUND_BOUTS_BEFORE}_a"] == pytest.approx(0.0)
    # ...e ausência explícita no azul, que é estreante -- zero e nulo não se confundem.
    assert row.features[f"{FIVE_ROUND_BOUTS_BEFORE}_b"] is None

    # Nenhuma coluna com dado é descartada em silêncio a caminho do treino. ``_b``/``_diff``
    # nascem 100% nulas neste fixture mínimo (o azul é sempre estreante) e caem pela
    # degradação já testada de coluna toda-nula, não por descarte silencioso.
    dataset = build_dataset(read_bout_features(db_session))
    com_dado = {*FORMAT_FEATURES, f"{FIVE_ROUND_BOUTS_BEFORE}_a"}
    assert com_dado <= set(dataset.feature_names)


# As 8 bases as-of do bloco C da SPEC 009 (Slice 04): duas contagens de derrota por método, a
# quilometragem em minutos e as cinco tendências. Todas por lutador -- viram o trio de canto.
_BLOCO_C_BASES: tuple[str, ...] = (
    KO_LOSSES_PRIOR,
    SUBMISSION_LOSSES_PRIOR,
    CAREER_MINUTES_BEFORE,
    SIG_STRIKES_LANDED_PM_TREND,
    SIG_STRIKES_ABSORBED_PM_TREND,
    TAKEDOWNS_LANDED_AVG_TREND,
    TAKEDOWN_DEFENSE_TREND,
    CONTROL_TIME_AVG_TREND,
)


def test_payload_do_bloco_c_e_numerico_ou_nulo_e_sobrevive_ao_dataset(
    db_session: Session,
) -> None:
    """CA-08/CA-09 da SPEC 009: as 24 colunas do bloco C vão do granular ao treino.

    Teste de aceitação da Slice 04, ponta a ponta pelo caminho real (``run_materialize``):

    1. cada uma das 8 bases vira o trio ``_a``/``_b``/``_diff`` no payload de
       ``bout_features`` -- o bloco C é todo por lutador, nenhuma coluna bout-level;
    2. os valores são número JSON ou ``null``, jamais string, jamais ``NaN``/``inf`` (que não
       são JSON válido);
    3. nenhuma coluna com dado é descartada em silêncio por ``build_dataset`` -- o modo de
       falha que escondeu 21 features do M5 por meses;
    4. o granular fica intocado (``bouts``, ``bout_fighters``, ``bout_fighter_rounds``,
       ``fighters``, ``events``): a re-materialização escreve só no cache.

    Valores as-of da 2a luta de A, derivados só da 1a (vitória por KO em 3 rounds cheios): a
    quilometragem é 15 minutos, ele nunca perdeu (as duas contagens em ``0`` -- **zero
    legítimo**, distinto do ``null`` do canto azul, que é estreante) e as três tendências
    computáveis são exatamente ``0`` porque, com uma única luta anterior, a janela de 3 e a
    carreira cobrem o mesmo conjunto. A defesa de queda e o tempo de controle não têm dado
    nesta luta, então as duas tendências restantes são ``null`` -- ausência, não ``0``.
    """
    second_bout_id = _seed_two_bouts_with_splits_and_rounds(db_session)
    contagens_antes = {
        modelo: db_session.scalar(select(func.count()).select_from(modelo))
        for modelo in (Bout, BoutFighter, BoutFighterRound, Fighter, Event)
    }

    run_materialize(db_session)

    row = db_session.get(BoutFeatures, second_bout_id)
    assert row is not None
    esperadas = {f"{base}{sufixo}" for base in _BLOCO_C_BASES for sufixo in ("_a", "_b", "_diff")}
    assert len(esperadas) == 24
    assert esperadas <= set(row.features)
    for coluna in sorted(esperadas):
        valor = row.features[coluna]
        assert valor is None or isinstance(valor, int | float), coluna
        assert not (isinstance(valor, float) and math.isnan(valor)), coluna
        assert valor != float("inf"), coluna

    assert row.features[f"{KO_LOSSES_PRIOR}_a"] == pytest.approx(0.0)
    assert row.features[f"{SUBMISSION_LOSSES_PRIOR}_a"] == pytest.approx(0.0)
    assert row.features[f"{CAREER_MINUTES_BEFORE}_a"] == pytest.approx(15.0)
    assert row.features[f"{SIG_STRIKES_LANDED_PM_TREND}_a"] == pytest.approx(0.0)
    assert row.features[f"{SIG_STRIKES_ABSORBED_PM_TREND}_a"] == pytest.approx(0.0)
    assert row.features[f"{TAKEDOWNS_LANDED_AVG_TREND}_a"] == pytest.approx(0.0)
    assert row.features[f"{TAKEDOWN_DEFENSE_TREND}_a"] is None
    assert row.features[f"{CONTROL_TIME_AVG_TREND}_a"] is None
    # Zero e ausência não se confundem: o canto azul é estreante nas duas lutas.
    assert row.features[f"{KO_LOSSES_PRIOR}_b"] is None
    assert row.features[f"{CAREER_MINUTES_BEFORE}_b"] is None

    # Nenhuma coluna com dado é descartada em silêncio a caminho do treino. As colunas
    # ``_b``/``_diff`` nascem 100% nulas neste fixture mínimo (o azul é sempre estreante) e
    # caem pela degradação já testada de coluna toda-nula, não por descarte silencioso.
    dataset = build_dataset(read_bout_features(db_session))
    com_dado = {
        f"{base}_a"
        for base in _BLOCO_C_BASES
        if base not in (TAKEDOWN_DEFENSE_TREND, CONTROL_TIME_AVG_TREND)
    }
    assert com_dado <= set(dataset.feature_names)

    # CA-09: o granular é fonte da verdade e não é escrito por nenhuma slice desta SPEC.
    for modelo, antes in contagens_antes.items():
        assert db_session.scalar(select(func.count()).select_from(modelo)) == antes, modelo
