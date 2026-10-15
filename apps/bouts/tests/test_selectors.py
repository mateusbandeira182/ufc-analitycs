"""Testes de Selector de bouts contra o Postgres de teste transacional.

Cobrem ``get_bout_by_id``: composição com o evento correto, os dois cantos e as
stats granulares **como foram gravadas** em ``bout_fighters`` (nunca médias), e o
caso de id inexistente (``None``). Também cobrem ``get_head_to_head`` (Slice 05 e, no M9,
o ponto 1 da varredura de ``bouts.status``):
os confrontos diretos entre dois lutadores em ordem cronológica, sem vazar lutas
contra terceiros. Semeiam via factories e exercitam o selector direto, sem subir
a API.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import event
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, BoutStatus, Corner
from apps.bouts.models import Bout
from apps.bouts.selectors import get_bout_by_id, get_common_opponents, get_head_to_head
from apps.bouts.tests.factories import (
    BoutFactory,
    BoutFighterFactory,
    BoutFighterRoundFactory,
    EventFactory,
)
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


def test_get_bout_by_id_carrega_rounds_por_canto(db_session: Session) -> None:
    """Devolve o breakdown round-a-round com o canto e o lutador de cada round."""
    bout = _persistir_bout_completo(db_session)
    detail = get_bout_by_id(db_session, bout.id)
    assert detail is not None
    por_canto = {bf.corner: bf for bf in detail.fighters}
    red_bf = por_canto[Corner.RED]
    db_session.add_all(
        [
            BoutFighterRoundFactory.build(
                bout_fighter_id=red_bf.id, round=1, sig_strikes_landed=18, head_landed=10
            ),
            BoutFighterRoundFactory.build(
                bout_fighter_id=red_bf.id, round=2, sig_strikes_landed=22, head_landed=15
            ),
        ]
    )
    db_session.flush()

    detail = get_bout_by_id(db_session, bout.id)

    assert detail is not None
    red_rounds = [r for r in detail.rounds if r.corner == Corner.RED]
    assert [r.round.round for r in red_rounds] == [1, 2]
    assert [r.round.sig_strikes_landed for r in red_rounds] == [18, 22]
    assert [r.round.head_landed for r in red_rounds] == [10, 15]
    assert {r.fighter_id for r in red_rounds} == {red_bf.fighter_id}
    assert red_rounds[0].round.source == "cito"


def test_get_bout_by_id_sem_rounds_devolve_rounds_vazio(db_session: Session) -> None:
    """Luta sem round-a-round (backfill parcial) devolve ``rounds`` vazio, sem quebrar."""
    bout = _persistir_bout_completo(db_session)

    detail = get_bout_by_id(db_session, bout.id)

    assert detail is not None
    assert detail.rounds == []


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
    status: BoutStatus = BoutStatus.COMPLETED,
) -> Bout:
    """Semeia 1 evento e 1 luta com os dois cantos entre ``red`` e ``blue``.

    ``status`` é explícito para os testes da luta agendada (M9, RF-07): a luta por vir tem
    os dois cantos em ``bout_fighters`` como qualquer outra, e é por eles que ela vaza.
    """
    event = EventFactory.build(name=event_name, date=event_date)
    session.add(event)
    session.flush()

    bout = BoutFactory.build(
        event_id=event.id,
        winner_id=winner.id if winner is not None else None,
        method=BoutMethod.DECISION if status is BoutStatus.COMPLETED else None,
        status=status,
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


def test_head_to_head_carrega_rounds_de_cada_confronto(db_session: Session) -> None:
    """Cada bout do head-to-head carrega o breakdown round-a-round dos seus cantos."""
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b])
    db_session.flush()
    bout = _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=b, winner=a
    )
    a_bf = next(bf for bf in get_bout_by_id(db_session, bout.id).fighters if bf.fighter_id == a.id)  # type: ignore[union-attr]
    db_session.add(
        BoutFighterRoundFactory.build(bout_fighter_id=a_bf.id, round=1, sig_strikes_landed=12)
    )
    db_session.flush()

    detalhes = get_head_to_head(db_session, a.id, b.id)

    assert len(detalhes) == 1
    rounds = detalhes[0].rounds
    assert [r.round.round for r in rounds] == [1]
    assert rounds[0].fighter_id == a.id
    assert rounds[0].round.sig_strikes_landed == 12


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


def test_head_to_head_ignora_a_luta_agendada_entre_a_dupla(db_session: Session) -> None:
    """Ponto 1 da varredura (M9, RF-07): confronto direto é **histórico**.

    A revanche já marcada entre os dois aparece na tela de confronto pelo card do evento
    futuro, não pelo head-to-head -- misturá-la ao histórico faria a dupla parecer ter se
    enfrentado uma vez a mais do que se enfrentou, e o placar do confronto mentiria.
    """
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b])
    db_session.flush()

    realizada = _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=b, winner=a
    )
    agendada = _seed_confronto(
        db_session,
        event_name="UFC Card Futuro",
        event_date=date(2026, 12, 12),
        red=a,
        blue=b,
        status=BoutStatus.SCHEDULED,
    )

    detalhes = get_head_to_head(db_session, a.id, b.id)

    assert [d.bout.id for d in detalhes] == [realizada.id]
    # Guarda de não-vacuidade: a agendada está persistida, com os dois cantos.
    persistida = db_session.get(Bout, agendada.id)
    assert persistida is not None
    assert persistida.status is BoutStatus.SCHEDULED
    assert len(persistida.bout_fighters) == 2


# ---------------------------------------------------------------------------
# Adversários em comum (SPEC 011, Slice 05 -- RF-16; CA-18/CA-P2)
# ---------------------------------------------------------------------------


def test_common_opponents_de_dupla_que_nunca_se_enfrentou(db_session: Session) -> None:
    """CA-18: A e B nunca se enfrentaram, mas os dois enfrentaram X -> uma entrada para X.

    É a queixa de origem da tela: hoje o head-to-head devolve lista vazia para qualquer dupla
    que nunca se enfrentou -- o caso comum --, e a página "não faz nada". Adversário em comum é
    o cruzamento que existe mesmo sem confronto direto.
    """
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Islam Makhachev")
    x = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b, x])
    db_session.flush()
    ax = _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=x, winner=a
    )
    bx = _seed_confronto(
        db_session, event_name="UFC 276", event_date=date(2022, 7, 2), red=x, blue=b, winner=b
    )

    comuns = get_common_opponents(db_session, a.id, b.id)

    (comum,) = comuns
    assert comum.opponent_id == x.id
    assert comum.opponent_name == "Max Holloway"
    assert [luta.bout_id for luta in comum.bouts_a] == [ax.id]
    assert [luta.bout_id for luta in comum.bouts_b] == [bx.id]
    # O desfecho de cada lado vem junto: é o que permite comparar os dois contra o MESMO
    # terceiro, que é a razão de a seção existir.
    assert comum.bouts_a[0].winner_id == a.id
    assert comum.bouts_b[0].winner_id == b.id
    assert comum.bouts_a[0].method is BoutMethod.DECISION
    assert comum.bouts_a[0].event_name == "UFC 245"
    assert comum.bouts_a[0].event_date == date(2019, 12, 14)


def test_common_opponents_mesmo_adversario_duas_vezes_nao_duplica_a_entrada(
    db_session: Session,
) -> None:
    """CA-18: adversário enfrentado duas vezes pelo mesmo lado -> uma entrada, duas lutas.

    O adversário não duplica (ele é a chave da entrada), as lutas somam -- e em ordem
    cronológica ascendente, para a tela ler a sequência como ela aconteceu.
    """
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Islam Makhachev")
    x = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b, x])
    db_session.flush()
    # Semeadas fora de ordem cronológica de propósito: a ordenação é do selector.
    segunda = _seed_confronto(
        db_session, event_name="UFC 276", event_date=date(2022, 7, 2), red=a, blue=x, winner=a
    )
    primeira = _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=x, blue=a, winner=a
    )
    _seed_confronto(
        db_session, event_name="UFC 300", event_date=date(2024, 4, 13), red=b, blue=x, winner=b
    )

    (comum,) = get_common_opponents(db_session, a.id, b.id)

    assert comum.opponent_id == x.id
    assert [luta.bout_id for luta in comum.bouts_a] == [primeira.id, segunda.id]
    assert [luta.event_date for luta in comum.bouts_a] == [
        date(2019, 12, 14),
        date(2022, 7, 2),
    ]


def test_common_opponents_exclui_adversario_de_um_lado_so(db_session: Session) -> None:
    """Adversário que só um dos dois enfrentou fica fora: a seção é a **interseção**."""
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Islam Makhachev")
    comum = FighterFactory.build(name="Max Holloway")
    so_de_a = FighterFactory.build(name="Brian Ortega")
    db_session.add_all([a, b, comum, so_de_a])
    db_session.flush()
    _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=comum, winner=a
    )
    _seed_confronto(
        db_session, event_name="UFC 300", event_date=date(2024, 4, 13), red=b, blue=comum, winner=b
    )
    _seed_confronto(
        db_session,
        event_name="UFC 266",
        event_date=date(2021, 9, 25),
        red=a,
        blue=so_de_a,
        winner=a,
    )

    comuns = get_common_opponents(db_session, a.id, b.id)

    assert [entrada.opponent_id for entrada in comuns] == [comum.id]


def test_common_opponents_nao_inclui_o_proprio_par(db_session: Session) -> None:
    """O confronto DIRETO entre A e B não vira entrada de adversário em comum.

    Sem excluir o próprio par, B apareceria como "adversário em comum de A e B" (A enfrentou B,
    e B enfrentou B? não -- mas A enfrentou B e o join trivial de B contra si mesmo ou a
    simples presença de B no conjunto de adversários de A o traria), duplicando na tela a
    seção de confronto direto que fica ao lado.
    """
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b])
    db_session.flush()
    _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=b, winner=a
    )

    comuns = get_common_opponents(db_session, a.id, b.id)

    assert comuns == []
    assert {entrada.opponent_id for entrada in comuns}.isdisjoint({a.id, b.id})


def test_common_opponents_ignora_luta_agendada(db_session: Session) -> None:
    """CA-P2: luta ``scheduled`` **não** produz adversário em comum -- o filtro é ``completed``.

    ``get_common_opponents`` é consumidor **novo** de ``bout_fighters``, criado depois da
    varredura de ``bouts.status`` desta SPEC: ``rg '\\bBout\\b'`` não casa ``BoutFighter``, e é
    por ``bout_fighters`` que a luta agendada vaza. Sem o filtro, um adversário que A **vai**
    enfrentar entraria no retrospecto como se já tivesse enfrentado -- e sem desfecho, o que a
    tela leria como derrota (``winner_id`` nulo).

    Luta agendada chega à tela pelo card do evento, não por aqui.
    """
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Islam Makhachev")
    futuro = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b, futuro])
    db_session.flush()
    agendada = _seed_confronto(
        db_session,
        event_name="UFC Card Futuro",
        event_date=date(2026, 12, 12),
        red=a,
        blue=futuro,
        status=BoutStatus.SCHEDULED,
    )
    _seed_confronto(
        db_session,
        event_name="UFC 300",
        event_date=date(2024, 4, 13),
        red=b,
        blue=futuro,
        winner=b,
    )

    comuns = get_common_opponents(db_session, a.id, b.id)

    assert comuns == []
    # Guarda de não-vacuidade: a agendada está persistida, com os dois cantos. Sem isto, o
    # teste passaria se o seed simplesmente não tivesse criado a luta.
    persistida = db_session.get(Bout, agendada.id)
    assert persistida is not None
    assert persistida.status is BoutStatus.SCHEDULED
    assert len(persistida.bout_fighters) == 2


def test_common_opponents_tem_ordem_deterministica_por_nome(db_session: Session) -> None:
    """A ordem das entradas é por nome do adversário, ascendente.

    Ordem determinística é contrato: sem ``ORDER BY``, o Postgres pode devolver em ordem de
    varredura e a tela reordenaria sozinha entre dois carregamentos do mesmo confronto.
    """
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Islam Makhachev")
    # Semeados em ordem inversa à alfabética, para a ordenação ter o que provar.
    zulu = FighterFactory.build(name="Zulu Fighter")
    alfa = FighterFactory.build(name="Alfa Fighter")
    db_session.add_all([a, b, zulu, alfa])
    db_session.flush()
    for adversario in (zulu, alfa):
        _seed_confronto(
            db_session,
            event_name=f"UFC A vs {adversario.name}",
            event_date=date(2019, 12, 14),
            red=a,
            blue=adversario,
            winner=a,
        )
        _seed_confronto(
            db_session,
            event_name=f"UFC B vs {adversario.name}",
            event_date=date(2024, 4, 13),
            red=b,
            blue=adversario,
            winner=b,
        )

    comuns = get_common_opponents(db_session, a.id, b.id)

    assert [entrada.opponent_name for entrada in comuns] == ["Alfa Fighter", "Zulu Fighter"]


def test_common_opponents_empate_preserva_winner_id_nulo(db_session: Session) -> None:
    """Empate contra o adversário em comum mantém ``winner_id`` nulo -- não colapsa em derrota.

    Deliberadamente **não** reusa o ``won: bool`` de ``FighterBoutOut``: ali empate colapsa em
    ``False``, e nesta seção -- onde os dois lutadores são comparados contra o MESMO terceiro --
    "perdeu para X" e "empatou com X" não podem virar a mesma afirmação.
    """
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Islam Makhachev")
    x = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b, x])
    db_session.flush()
    _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=x, winner=None
    )
    _seed_confronto(
        db_session, event_name="UFC 300", event_date=date(2024, 4, 13), red=b, blue=x, winner=b
    )

    (comum,) = get_common_opponents(db_session, a.id, b.id)

    assert comum.bouts_a[0].winner_id is None
    assert comum.bouts_b[0].winner_id == b.id


def test_common_opponents_ordem_dos_argumentos_espelha_os_lados(db_session: Session) -> None:
    """Trocar ``a_id`` e ``b_id`` espelha ``bouts_a``/``bouts_b``, sem mudar o conjunto.

    A função não é simétrica por acidente: ``bouts_a`` é sempre do primeiro argumento. O que
    não pode mudar é **quem** são os adversários em comum.
    """
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Islam Makhachev")
    x = FighterFactory.build(name="Max Holloway")
    db_session.add_all([a, b, x])
    db_session.flush()
    ax = _seed_confronto(
        db_session, event_name="UFC 245", event_date=date(2019, 12, 14), red=a, blue=x, winner=a
    )
    bx = _seed_confronto(
        db_session, event_name="UFC 300", event_date=date(2024, 4, 13), red=b, blue=x, winner=b
    )

    (direto,) = get_common_opponents(db_session, a.id, b.id)
    (invertido,) = get_common_opponents(db_session, b.id, a.id)

    assert direto.opponent_id == invertido.opponent_id == x.id
    assert [luta.bout_id for luta in direto.bouts_a] == [ax.id]
    assert [luta.bout_id for luta in invertido.bouts_a] == [bx.id]
    assert [luta.bout_id for luta in invertido.bouts_b] == [ax.id]


def test_common_opponents_usa_duas_queries_sem_n_mais_1(db_session: Session) -> None:
    """Duas queries no total (uma por lado), constantes -- não crescem com os adversários.

    Semeia quatro adversários em comum e conta os statements que tocam ``bout_fighters``. A
    interseção é feita em memória e o nome do adversário vem resolvido no join, então nem a
    quantidade de adversários nem a de lutas por adversário acrescenta consulta. Sem esta
    guarda, um N+1 aqui só apareceria na produção, onde um lutador tem dezenas de adversários.
    """
    a = FighterFactory.build(name="Alexander Volkanovski")
    b = FighterFactory.build(name="Islam Makhachev")
    adversarios = [FighterFactory.build(name=f"Common Opponent {indice}") for indice in range(4)]
    db_session.add_all([a, b, *adversarios])
    db_session.flush()
    for indice, adversario in enumerate(adversarios):
        _seed_confronto(
            db_session,
            event_name=f"UFC A{indice}",
            event_date=date(2019, 1, 1),
            red=a,
            blue=adversario,
            winner=a,
        )
        _seed_confronto(
            db_session,
            event_name=f"UFC B{indice}",
            event_date=date(2024, 1, 1),
            red=b,
            blue=adversario,
            winner=b,
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
        comuns = get_common_opponents(db_session, a.id, b.id)
    finally:
        event.remove(connection, "before_cursor_execute", _capturar)

    assert len(comuns) == 4
    assert len(statements) == 2
