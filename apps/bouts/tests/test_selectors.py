"""Testes de Selector de bouts contra o Postgres de teste transacional.

Cobrem ``get_bout_by_id``: composição com o evento correto, os dois cantos e as
stats granulares **como foram gravadas** em ``bout_fighters`` (nunca médias), e o
caso de id inexistente (``None``). Também cobrem ``get_head_to_head`` (Slice 05):
os confrontos diretos entre dois lutadores em ordem cronológica, sem vazar lutas
contra terceiros. Semeiam via factories e exercitam o selector direto, sem subir
a API.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import event
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout
from apps.bouts.selectors import get_bout_by_id, get_head_to_head
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.fighters.models import Fighter
from apps.fighters.tests.factories import FighterFactory


def _persistir_bout_completo(session: Session) -> Bout:
    """Semeia 1 evento, 2 lutadores, 1 luta e os 2 cantos com stats distintas."""
    event = EventFactory.build(name="UFC 300", date=date(2024, 4, 13))
    red = FighterFactory.build(name="Alex Pereira")
    blue = FighterFactory.build(name="Jamahal Hill")
    session.add_all([event, red, blue])
    session.flush()

    bout = BoutFactory.build(
        event_id=event.id,
        winner_id=red.id,
        method=BoutMethod.KO_TKO,
        round=1,
        ending_time_seconds=191,
        weight_class="Light Heavyweight",
    )
    session.add(bout)
    session.flush()

    session.add_all(
        [
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=red.id,
                corner=Corner.RED,
                knockdowns=1,
                sig_strikes_landed=40,
                sig_strikes_attempted=55,
                takedowns_landed=0,
                takedowns_attempted=0,
                submission_attempts=0,
                control_time_seconds=90,
            ),
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=blue.id,
                corner=Corner.BLUE,
                knockdowns=0,
                sig_strikes_landed=12,
                sig_strikes_attempted=30,
                takedowns_landed=1,
                takedowns_attempted=3,
                submission_attempts=0,
                control_time_seconds=30,
            ),
        ]
    )
    session.flush()
    return bout


def test_get_bout_by_id_compoe_evento_e_dois_cantos(db_session: Session) -> None:
    """Devolve a luta com o evento correto e os dois cantos com stats granulares."""
    bout = _persistir_bout_completo(db_session)

    detail = get_bout_by_id(db_session, bout.id)

    assert detail is not None
    assert detail.bout.id == bout.id
    assert detail.event.name == "UFC 300"
    assert detail.event.date == date(2024, 4, 13)
    assert len(detail.fighters) == 2
    assert {bf.corner for bf in detail.fighters} == {Corner.RED, Corner.BLUE}
    # Identidade do lutador de cada canto vem via ``BoutFighter.fighter`` (enrich SPA).
    por_canto = {bf.corner: bf for bf in detail.fighters}
    assert por_canto[Corner.RED].fighter.name == "Alex Pereira"
    assert por_canto[Corner.BLUE].fighter.name == "Jamahal Hill"


def test_get_bout_by_id_preserva_stats_granulares_por_canto(db_session: Session) -> None:
    """As stats vêm de ``bout_fighters`` como gravadas, distintas por canto."""
    bout = _persistir_bout_completo(db_session)

    detail = get_bout_by_id(db_session, bout.id)

    assert detail is not None
    por_canto = {bf.corner: bf for bf in detail.fighters}
    assert por_canto[Corner.RED].sig_strikes_landed == 40
    assert por_canto[Corner.RED].control_time_seconds == 90
    assert por_canto[Corner.BLUE].sig_strikes_landed == 12
    assert por_canto[Corner.BLUE].takedowns_landed == 1


def test_get_bout_by_id_inexistente_devolve_none(db_session: Session) -> None:
    """Id inexistente devolve ``None`` (sem efeito colateral)."""
    assert get_bout_by_id(db_session, 999_999) is None


def _seed_confronto(
    session: Session,
    *,
    event_name: str,
    event_date: date,
    red: Fighter,
    blue: Fighter,
    winner: Fighter | None = None,
) -> Bout:
    """Semeia 1 evento e 1 luta com os dois cantos entre ``red`` e ``blue``."""
    event = EventFactory.build(name=event_name, date=event_date)
    session.add(event)
    session.flush()

    bout = BoutFactory.build(
        event_id=event.id,
        winner_id=winner.id if winner is not None else None,
        method=BoutMethod.DECISION,
        round=3,
        ending_time_seconds=300,
        weight_class="Featherweight",
    )
    session.add(bout)
    session.flush()

    session.add_all(
        [
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=red.id,
                corner=Corner.RED,
                sig_strikes_landed=50,
                control_time_seconds=120,
            ),
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=blue.id,
                corner=Corner.BLUE,
                sig_strikes_landed=45,
                control_time_seconds=60,
            ),
        ]
    )
    session.flush()
    return bout


def test_head_to_head_retorna_confrontos_diretos_em_ordem_cronologica(
    db_session: Session,
) -> None:
    """Retorna só os bouts A-vs-B, em ordem cronológica por ``events.date``."""
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Max Holloway")
    c = FighterFactory.build(name="Islam Makhachev")
    db_session.add_all([a, b, c])
    db_session.flush()

    luta_antiga = _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=b, winner=a
    )
    luta_recente = _seed_confronto(
        db_session, event_name="UFC 276", event_date=date(2022, 7, 2), red=b, blue=a, winner=a
    )
    # Ruído: A contra um terceiro lutador -- não pode aparecer no head-to-head A-vs-B.
    _seed_confronto(
        db_session, event_name="UFC 284", event_date=date(2023, 2, 11), red=a, blue=c, winner=c
    )

    detalhes = get_head_to_head(db_session, a.id, b.id)

    assert [d.bout.id for d in detalhes] == [luta_antiga.id, luta_recente.id]


def test_head_to_head_carrega_os_dois_cantos_de_cada_confronto(db_session: Session) -> None:
    """Cada bout do resultado carrega os dois ``bout_fighters`` (stats por canto)."""
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b])
    db_session.flush()
    _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=b, winner=a
    )

    detalhes = get_head_to_head(db_session, a.id, b.id)

    assert len(detalhes) == 1
    assert {bf.fighter_id for bf in detalhes[0].fighters} == {a.id, b.id}
    assert {bf.corner for bf in detalhes[0].fighters} == {Corner.RED, Corner.BLUE}
    # Cada canto carrega a identidade do lutador (``BoutFighter.fighter``).
    assert {bf.fighter.name for bf in detalhes[0].fighters} == {
        "Alexander Volkanovski",
        "Max Holloway",
    }


def test_head_to_head_ordem_dos_argumentos_nao_altera_resultado(db_session: Session) -> None:
    """O confronto aparece independentemente de qual lutador é ``a`` ou ``b``."""
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b])
    db_session.flush()
    _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=b, winner=a
    )

    assert len(get_head_to_head(db_session, a.id, b.id)) == 1
    assert len(get_head_to_head(db_session, b.id, a.id)) == 1


def test_head_to_head_sem_confronto_direto_devolve_lista_vazia(db_session: Session) -> None:
    """Dois lutadores que nunca se enfrentaram devolvem lista vazia (não é erro)."""
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Max Holloway")
    c = FighterFactory.build(name="Islam Makhachev")
    db_session.add_all([a, b, c])
    db_session.flush()
    # A luta contra C, B contra C, mas A e B nunca entre si.
    _seed_confronto(
        db_session, event_name="UFC 284", event_date=date(2023, 2, 11), red=a, blue=c, winner=c
    )
    _seed_confronto(
        db_session, event_name="UFC 296", event_date=date(2023, 12, 16), red=b, blue=c, winner=b
    )

    assert get_head_to_head(db_session, a.id, b.id) == []


def test_head_to_head_carrega_cantos_em_lote_sem_n_mais_1(db_session: Session) -> None:
    """A carga dos cantos de N confrontos usa UMA query em lote (sem N+1).

    Semeia três revanches diretas A-vs-B (rivalidade com múltiplos confrontos) e
    conta os statements que tocam ``bout_fighters`` durante a leitura. Com a carga
    em lote, são apenas dois -- a query principal do head-to-head (que já junta os
    cantos por aliases) e a única query em lote dos cantos (``WHERE bout_id IN
    (...)``) -- número constante, que não cresce com a quantidade de bouts. A
    corretude (dois cantos por luta) é conferida em paralelo.
    """
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b])
    db_session.flush()
    for indice, ano in enumerate((2019, 2022, 2024)):
        _seed_confronto(
            db_session,
            event_name=f"UFC rivalidade {indice}",
            event_date=date(ano, 1, 1),
            red=a,
            blue=b,
            winner=a,
        )

    statements: list[str] = []

    def _capturar(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "bout_fighters" in statement:
            statements.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", _capturar)
    try:
        detalhes = get_head_to_head(db_session, a.id, b.id)
    finally:
        event.remove(connection, "before_cursor_execute", _capturar)

    assert len(detalhes) == 3
    for detalhe in detalhes:
        assert {bf.fighter_id for bf in detalhe.fighters} == {a.id, b.id}
        assert {bf.corner for bf in detalhe.fighters} == {Corner.RED, Corner.BLUE}
    assert len(statements) == 2
