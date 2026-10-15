"""Testes do CLI de features -- ``run_build`` (dispatch por estágio) -- CA-01 do Plano 005-01.

Exercitam o entrypoint testável: o estágio ``long`` devolve a frame longa (mesma
contagem de participações) e um estágio desconhecido levanta ``ValueError`` claro. O
``main`` fino (abre ``SessionLocal``, não commita) é verificado por execução manual no
DoD da sprint, como ``ingestion/seed.py::main``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter, BoutFighterRound
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.features.asof import MATERIALIZE_ASOF_COMMAND
from ingestion.features.cli import _enriched_long_frame, main, run_build, run_check, run_materialize
from ingestion.features.freshness import StaleFeatureCacheError
from ingestion.features.materialize import MATERIALIZE_COMMAND
from ingestion.features.rolling import (
    RECENT_FORM_FEATURES,
    ROUND1_SIG_STRIKE_SHARE_R3,
    SHARE_HEAD_R3,
    WIN_RATE_PRIOR,
)
from ingestion.normalize import normalize_name


class _SessionFalsa:
    """``Session`` de fachada para o ``main``: registra a intenção de ``commit``.

    O ``main`` abre ``SessionLocal()`` num ``with`` e commita no sucesso dos estágios de
    escrita. Commitar de verdade aqui quebraria o isolamento da suíte (o rollback do teardown
    é o que devolve o banco limpo), e o que o teste precisa afirmar é exatamente **que o
    commit acontece** -- por isso a fachada registra em vez de executar.
    """

    def __init__(self, chamadas: list[str]) -> None:
        self._chamadas = chamadas

    def __enter__(self) -> _SessionFalsa:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def commit(self) -> None:
        self._chamadas.append("commit")


def _seed_one_bout(db_session: Session) -> None:
    """Semeia uma luta com dois cantos, suficiente para a contagem do CLI."""
    fighters = []
    for name in ("Alexander Volkanovski", "Ilia Topuria"):
        fighter = Fighter(
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
        db_session.add(fighter)
        fighters.append(fighter)
    event = Event(name="UFC 300: Test", date=date(2024, 4, 13), location=None, source="kaggle")
    db_session.add(event)
    db_session.flush()
    bout = Bout(
        event_id=event.id,
        winner_id=fighters[1].id,
        method=BoutMethod.KO_TKO,
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
                fighter_id=fighters[0].id,
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
                fighter_id=fighters[1].id,
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


def test_run_build_long_devolve_a_frame(db_session: Session) -> None:
    """CA-01: ``run_build(stage="long")`` devolve a frame longa (2 linhas para 1 bout)."""
    _seed_one_bout(db_session)

    df = run_build(db_session, stage="long")

    assert len(df) == 2
    assert list(df["result"]) == ["loss", "win"]


def test_run_build_estagio_desconhecido_levanta_value_error(db_session: Session) -> None:
    """CA-01: um estágio não suportado levanta ``ValueError`` claro, sem tocar o banco."""
    with pytest.raises(ValueError, match="Estágio desconhecido"):
        run_build(db_session, stage="inexistente")


def _seed_two_bouts_same_fighter(db_session: Session) -> None:
    """Semeia duas lutas de um mesmo lutador (A) para exercitar o estágio ``rolling``.

    A vence a 1a (KO/TKO) e a 2a (decisão), contra oponentes distintos, com ``round`` e
    ``ending_time_seconds`` preenchidos para que as taxas por minuto sejam calculáveis.
    """
    names = ("Fighter A", "Opponent B", "Opponent C")
    a, b, c = (
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
        for name in names
    )
    db_session.add_all([a, b, c])
    evt1 = Event(name="UFC 1: Test", date=date(2023, 1, 1), location=None, source="kaggle")
    evt2 = Event(name="UFC 2: Test", date=date(2023, 6, 1), location=None, source="kaggle")
    db_session.add_all([evt1, evt2])
    db_session.flush()

    def _add(event: Event, opponent: Fighter, method: BoutMethod) -> None:
        bout = Bout(
            event_id=event.id,
            winner_id=a.id,
            method=method,
            round=3,
            ending_time_seconds=300,
            weight_class=None,
            source="kaggle",
        )
        db_session.add(bout)
        db_session.flush()
        db_session.add_all(
            [
                BoutFighter(
                    bout_id=bout.id,
                    fighter_id=a.id,
                    corner=Corner.RED,
                    knockdowns=None,
                    sig_strikes_landed=30,
                    sig_strikes_attempted=None,
                    takedowns_landed=1,
                    takedowns_attempted=2,
                    submission_attempts=None,
                    control_time_seconds=60,
                    source="kaggle",
                ),
                BoutFighter(
                    bout_id=bout.id,
                    fighter_id=opponent.id,
                    corner=Corner.BLUE,
                    knockdowns=None,
                    sig_strikes_landed=10,
                    sig_strikes_attempted=None,
                    takedowns_landed=0,
                    takedowns_attempted=1,
                    submission_attempts=None,
                    control_time_seconds=0,
                    source="kaggle",
                ),
            ]
        )
        db_session.flush()

    _add(evt1, b, BoutMethod.KO_TKO)
    _add(evt2, c, BoutMethod.DECISION)


def test_run_build_rolling_enriquece_com_features_de_forma_recente(db_session: Session) -> None:
    """CA-05: ``run_build(stage="rolling")`` compõe a frame longa com as features de forma."""
    _seed_two_bouts_same_fighter(db_session)

    df = run_build(db_session, stage="rolling")

    assert set(RECENT_FORM_FEATURES).issubset(set(df.columns))
    # A 2a luta de A tem 1 luta anterior -> win rate acumulado definido (não NaN).
    fighter_a = df.loc[df["fighter_name"] == "Fighter A"].sort_values("event_date")
    assert fighter_a[WIN_RATE_PRIOR].iloc[1] == 1.0
    # Estreia (1a luta de A) -> NaN explícito.
    assert fighter_a[WIN_RATE_PRIOR].iloc[0] != fighter_a[WIN_RATE_PRIOR].iloc[0]


def test_run_build_rolling_e_deterministico(db_session: Session) -> None:
    """CA-05: rodar o estágio ``rolling`` duas vezes produz frames idênticas."""
    _seed_two_bouts_same_fighter(db_session)

    primeira = run_build(db_session, stage="rolling")
    segunda = run_build(db_session, stage="rolling")

    pd.testing.assert_frame_equal(primeira, segunda)


def test_enriched_long_frame_inclui_perfil_de_striking_e_dinamica_por_round(
    db_session: Session,
) -> None:
    """CA-01/CA-02: a frame enriquecida do M5 traz as shares de striking e a dinâmica por round.

    Semeia duas lutas de um lutador com splits preenchidos e round-a-round na 1a luta; a 2a
    luta enxerga o histórico point-in-time. Prova o encadeamento completo (long -> rolling ->
    trajectory -> round dynamics) via ``_enriched_long_frame``.
    """
    _seed_two_bouts_same_fighter(db_session)
    bout_fighters = (
        db_session.execute(
            select(BoutFighter).where(BoutFighter.fighter_id == _fighter_a_id(db_session))
        )
        .scalars()
        .all()
    )
    ordenados = sorted(bout_fighters, key=lambda bf: bf.bout_id)
    for bf in ordenados:
        bf.head_landed = 20
        bf.body_landed = 5
        bf.leg_landed = 5
        bf.distance_landed = 18
        bf.clinch_landed = 6
        bf.ground_landed = 6
    # Round-a-round só na 1a luta de A: r1=15, r2=5 -> round1 share 0.75.
    primeira = ordenados[0]
    db_session.add_all(
        [
            BoutFighterRound(
                bout_fighter_id=primeira.id,
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

    df = _enriched_long_frame(db_session)

    assert SHARE_HEAD_R3 in df.columns
    assert ROUND1_SIG_STRIKE_SHARE_R3 in df.columns
    do_a = (
        df.loc[df["fighter_name"] == "Fighter A"].sort_values("event_date").reset_index(drop=True)
    )
    # 2a luta usa só a 1a: share de cabeça = 20/30; round1 share = 0.75.
    assert do_a[SHARE_HEAD_R3].iloc[1] == pytest.approx(20 / 30)
    assert do_a[ROUND1_SIG_STRIKE_SHARE_R3].iloc[1] == pytest.approx(0.75)
    # Estreia é NaN em ambas.
    assert pd.isna(do_a[SHARE_HEAD_R3].iloc[0])
    assert pd.isna(do_a[ROUND1_SIG_STRIKE_SHARE_R3].iloc[0])


def _fighter_a_id(session: Session) -> int:
    """Id do ``Fighter A`` semeado por ``_seed_two_bouts_same_fighter``."""
    return int(session.execute(select(Fighter.id).where(Fighter.name == "Fighter A")).scalar_one())


# ---------------------------------------------------------------------------
# Estágio ``materialize-asof`` e a desambiguação das mensagens (SPEC 011, Slice 01)
# ---------------------------------------------------------------------------


def test_main_despacha_materialize_asof_e_commita(monkeypatch: pytest.MonkeyPatch) -> None:
    """CA-01: ``build --stage materialize-asof`` roda a materialização as-of e **commita**.

    Mesmo contrato do estágio ``materialize`` (grão luta): os estágios de leitura não commitam,
    os de escrita sim -- é o que torna a idempotência observável reexecutando o comando.
    """
    chamadas: list[str] = []
    monkeypatch.setattr("ingestion.features.cli.SessionLocal", lambda: _SessionFalsa(chamadas))

    def _registrar(_session: object, **_kwargs: object) -> int:
        chamadas.append("materialize-asof")
        return 0

    monkeypatch.setattr("ingestion.features.cli.run_materialize_asof", _registrar)

    main(["build", "--stage", "materialize-asof"])

    assert chamadas == ["materialize-asof", "commit"]


def test_run_build_nao_aceita_o_estagio_de_escrita_as_of(db_session: Session) -> None:
    """``run_build`` é o despacho dos estágios de LEITURA -- o de escrita não entra por ali.

    Mesma fronteira que ``materialize`` já respeita: quem escreve tem função própria, chamada
    pelo ``main``, para que nenhum estágio read-only possa persistir por engano.
    """
    with pytest.raises(ValueError, match="Estágio desconhecido"):
        run_build(db_session, stage="materialize-asof")


def test_o_aviso_de_bout_features_nomeia_o_estagio_certo(db_session: Session) -> None:
    """CA-P1: o aviso do cache **por luta** manda rodar ``--stage materialize``, não o as-of.

    A armadilha é de substring: ``"--stage materialize"`` é **prefixo** de
    ``"--stage materialize-asof"``, então uma asserção ingênua (``in``) passaria com a mensagem
    errada. A asserção negativa é obrigatória -- sem ela, a RF-06 acabaria mandando o usuário
    reconstruir o cache errado, e o defasado continuaria defasado.
    """
    _seed_one_bout(db_session)
    run_materialize(db_session)
    _seed_two_bouts_same_fighter(db_session)

    with pytest.raises(StaleFeatureCacheError) as excinfo:
        run_check(db_session)

    mensagem = str(excinfo.value)
    # Nomeia o cache pelo grão, não só pela tabela: com dois caches as-of no projeto, "o cache
    # está defasado" deixou de ser diagnóstico.
    assert "bout_features (cache por luta)" in mensagem
    assert MATERIALIZE_COMMAND in mensagem
    assert "materialize-asof" not in mensagem


def test_as_duas_constantes_de_comando_sao_distintas() -> None:
    """CA-P1: cada cache nomeia o próprio estágio, e os dois comandos não se confundem."""
    assert MATERIALIZE_COMMAND.endswith("--stage materialize")
    assert MATERIALIZE_ASOF_COMMAND.endswith("--stage materialize-asof")
    assert MATERIALIZE_COMMAND != MATERIALIZE_ASOF_COMMAND
