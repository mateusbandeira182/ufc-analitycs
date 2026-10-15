"""Testes de Selector de events contra o Postgres de teste transacional.

Cobrem ``list_events`` (ordem por data decrescente, paginação por limit/offset,
janela vazia e -- no M9, Slice 07 -- a partição ``when=past|upcoming`` derivada do
``status`` da luta), ``get_event_by_id`` (encontrado e inexistente),
``list_event_bouts`` (bouts do card, event sem bouts e a luta agendada, que **entra**
no card, com a ordenação por ``bout_order``), ``get_latest_completed_event``,
``summarize_event_bouts`` e ``collect_event_highlights``. Semeiam via factories e
exercitam o selector direto, sem subir a API.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, BoutStatus, Corner
from apps.bouts.models import Bout
from apps.bouts.tests.factories import BoutFactory, BoutFighterFactory, EventFactory
from apps.events.models import Event
from apps.events.selectors import (
    collect_event_highlights,
    get_event_by_id,
    get_latest_completed_event,
    list_event_bouts,
    list_events,
    summarize_event_bouts,
)
from apps.fighters.models import Fighter
from apps.fighters.tests.factories import FighterFactory
from apps.predictions.services import SOURCE_WALK_FORWARD, record_prediction


def _add_event(session: Session, name: str, event_date: date) -> Event:
    """Semeia um event com ``name``/``date`` dados e devolve o model persistido."""
    event = EventFactory.build(name=name, date=event_date)
    session.add(event)
    session.flush()
    return event


def _hoje(session: Session) -> date:
    """Data corrente **do Postgres** -- o mesmo relógio que o selector usa.

    A partição compara ``Event.date`` com ``func.current_date()``; derivar as datas do
    teste de ``datetime.now(UTC).date()`` colocaria dois relógios na mesma asserção e
    tornaria o caso de borda "evento de hoje" intermitente na virada do dia.
    """
    hoje = session.scalar(select(func.current_date()))
    assert isinstance(hoje, date)
    return hoje


def _add_bout(session: Session, event: Event, *, status: BoutStatus, **campos: object) -> Bout:
    """Semeia uma luta do ``event`` com o ``status`` dado e devolve o model persistido."""
    bout = BoutFactory.build(event_id=event.id, status=status, **campos)
    session.add(bout)
    session.flush()
    return bout


def test_list_events_ordena_por_data_desc(db_session: Session) -> None:
    """Devolve os events mais recentes primeiro (data decrescente)."""
    _add_event(db_session, "UFC 300", date(2024, 4, 13))
    _add_event(db_session, "UFC 290", date(2023, 7, 8))
    _add_event(db_session, "UFC 310", date(2024, 12, 7))

    rows, total = list_events(db_session, limit=50, offset=0)

    assert total == 3
    assert [row.name for row in rows] == ["UFC 310", "UFC 300", "UFC 290"]


def test_list_events_pagina_com_limit_e_offset(db_session: Session) -> None:
    """``limit``/``offset`` paginam sobre a ordem recentes-primeiro."""
    _add_event(db_session, "UFC 290", date(2023, 7, 8))
    _add_event(db_session, "UFC 300", date(2024, 4, 13))
    _add_event(db_session, "UFC 310", date(2024, 12, 7))

    primeira, total = list_events(db_session, limit=2, offset=0)
    segunda, _ = list_events(db_session, limit=2, offset=2)

    assert total == 3
    assert [row.name for row in primeira] == ["UFC 310", "UFC 300"]
    assert [row.name for row in segunda] == ["UFC 290"]


def test_list_events_offset_alem_do_total_devolve_vazio(db_session: Session) -> None:
    """``offset`` além do total devolve página vazia, mas ``total`` cheio."""
    _add_event(db_session, "UFC 300", date(2024, 4, 13))
    _add_event(db_session, "UFC 310", date(2024, 12, 7))

    rows, total = list_events(db_session, limit=50, offset=10)

    assert rows == []
    assert total == 2


def test_list_events_lista_vazia(db_session: Session) -> None:
    """Sem events semeados, devolve página vazia e ``total`` zero."""
    rows, total = list_events(db_session, limit=50, offset=0)

    assert rows == []
    assert total == 0


def test_get_event_by_id_encontrado_e_inexistente(db_session: Session) -> None:
    """Devolve o event pelo id e ``None`` quando o id não existe."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))

    assert get_event_by_id(db_session, event.id) is event
    assert get_event_by_id(db_session, 999_999) is None


def test_list_event_bouts_devolve_bouts_do_event(db_session: Session) -> None:
    """Devolve os bouts do event semeado, isolando os de outros events."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    outro = _add_event(db_session, "UFC 290", date(2023, 7, 8))
    primeiro = BoutFactory.build(event_id=event.id, weight_class="Lightweight")
    segundo = BoutFactory.build(event_id=event.id, weight_class="Welterweight")
    de_outro = BoutFactory.build(event_id=outro.id, weight_class="Featherweight")
    db_session.add_all([primeiro, segundo, de_outro])
    db_session.flush()

    bouts = list_event_bouts(db_session, event.id)

    assert {bout.id for bout in bouts} == {primeiro.id, segundo.id}


def test_list_event_bouts_carrega_os_cantos_com_identidade(db_session: Session) -> None:
    """Cada bout do card carrega os dois cantos com a identidade do lutador (enrich SPA)."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    red = FighterFactory.build(name="Alex Pereira")
    blue = FighterFactory.build(name="Jamahal Hill")
    db_session.add_all([red, blue])
    db_session.flush()
    bout = BoutFactory.build(event_id=event.id, weight_class="Light Heavyweight")
    db_session.add(bout)
    db_session.flush()
    db_session.add_all(
        [
            BoutFighterFactory.build(bout_id=bout.id, fighter_id=red.id, corner=Corner.RED),
            BoutFighterFactory.build(bout_id=bout.id, fighter_id=blue.id, corner=Corner.BLUE),
        ]
    )
    db_session.flush()

    bouts = list_event_bouts(db_session, event.id)

    assert len(bouts) == 1
    por_canto = {bf.corner: bf for bf in bouts[0].bout_fighters}
    assert set(por_canto) == {Corner.RED, Corner.BLUE}
    assert por_canto[Corner.RED].fighter_id == red.id
    assert por_canto[Corner.RED].fighter.name == "Alex Pereira"
    assert por_canto[Corner.BLUE].fighter.name == "Jamahal Hill"


def test_list_event_bouts_event_sem_bouts_devolve_vazio(db_session: Session) -> None:
    """Event sem bouts devolve lista vazia."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))

    assert list_event_bouts(db_session, event.id) == []


def test_list_event_bouts_inclui_a_luta_agendada(db_session: Session) -> None:
    """Ponto 2 da varredura (M9, RF-07): o card **não** filtra ``status``.

    Decisão ativa, não omissão: o card de um evento futuro é feito inteiro de lutas
    agendadas, e quem decide o que exibir é a tela. Filtrar aqui esvaziaria a página do
    evento por vir -- exatamente a tela que o M9 existe para entregar.
    """
    event = _add_event(db_session, "UFC Card Futuro", date(2026, 12, 12))
    realizada = BoutFactory.build(event_id=event.id, weight_class="Lightweight")
    agendada = BoutFactory.build(
        event_id=event.id,
        winner_id=None,
        method=None,
        status=BoutStatus.SCHEDULED,
        weight_class="Welterweight",
    )
    db_session.add_all([realizada, agendada])
    db_session.flush()

    rows = list_event_bouts(db_session, event.id)

    assert {bout.id for bout in rows} == {realizada.id, agendada.id}
    assert {bout.status for bout in rows} == {BoutStatus.COMPLETED, BoutStatus.SCHEDULED}


def test_list_event_bouts_ordena_por_bout_order_com_nulos_no_fim(db_session: Session) -> None:
    """Ponto 2 da varredura: a ordem do card é ``bout_order asc nulls_last, id asc``.

    ``bout_order`` é **ordenável, mas não sequencial nem denso** -- o payload real da Cito
    traz 1001 na luta principal --, então o teste afirma a ordem relativa, nunca os valores.
    Os nulos vão para o fim porque luta sem contexto de card não pode encabeçar o card, e o
    desempate por ``id`` mantém a lista reprodutível entre execuções.

    As lutas são inseridas **fora** da ordem de ``id`` de propósito: com elas em ordem, o
    ``order_by(Bout.id)`` antigo passaria por coincidência.
    """
    event = _add_event(db_session, "UFC Card Futuro", date(2026, 12, 12))
    sem_ordem = BoutFactory.build(event_id=event.id, bout_order=None, weight_class="Flyweight")
    db_session.add(sem_ordem)
    db_session.flush()
    principal = BoutFactory.build(
        event_id=event.id,
        winner_id=None,
        method=None,
        status=BoutStatus.SCHEDULED,
        bout_order=1001,
        weight_class="Welterweight",
    )
    db_session.add(principal)
    db_session.flush()
    coprincipal = BoutFactory.build(
        event_id=event.id,
        method=BoutMethod.DECISION,
        bout_order=1002,
        weight_class="Lightweight",
    )
    db_session.add(coprincipal)
    db_session.flush()

    rows = list_event_bouts(db_session, event.id)

    assert [bout.id for bout in rows] == [principal.id, coprincipal.id, sem_ordem.id]


def _semeia_acervo_da_particao(session: Session) -> dict[str, Event]:
    """Semeia os oito cenários da partição ``when=`` e devolve os events por apelido.

    Os seis primeiros espelham casos reais medidos no banco em 2026-10-15 (DT-1): há
    evento de data **passada** cujas lutas seguem todas ``scheduled``, porque o
    ``gap_sync`` é manual -- classificá-lo como realizado faria o resumo contar zero
    KO, zero finalização e zero decisão, zeros que se leem como medição.

    As datas são relativas ao ``current_date`` do Postgres, nunca fixas: data embutida
    em teste apodrece.
    """
    hoje = _hoje(session)
    passado = hoje - timedelta(days=30)
    futuro = hoje + timedelta(days=30)

    passado_so_agendado = _add_event(session, "UFC FN: Hooker vs Parnasse", passado)
    _add_bout(
        session, passado_so_agendado, status=BoutStatus.SCHEDULED, winner_id=None, method=None
    )

    passado_realizado = _add_event(session, "UFC 300", passado)
    _add_bout(session, passado_realizado, status=BoutStatus.COMPLETED)

    futuro_agendado = _add_event(session, "UFC 334", futuro)
    _add_bout(session, futuro_agendado, status=BoutStatus.SCHEDULED, winner_id=None, method=None)

    futuro_sem_lutas = _add_event(session, "UFC 335", futuro)

    passado_sem_lutas = _add_event(session, "UFC 1 (card não ingerido)", passado)

    card_misto = _add_event(session, "UFC FN: card misto", passado)
    _add_bout(session, card_misto, status=BoutStatus.COMPLETED)
    _add_bout(session, card_misto, status=BoutStatus.SCHEDULED, winner_id=None, method=None)

    futuro_realizado = _add_event(session, "UFC 333 (resultado adiantado)", futuro)
    _add_bout(session, futuro_realizado, status=BoutStatus.COMPLETED)

    hoje_sem_lutas = _add_event(session, "Noche UFC (hoje)", hoje)

    return {
        "passado_so_agendado": passado_so_agendado,
        "passado_realizado": passado_realizado,
        "futuro_agendado": futuro_agendado,
        "futuro_sem_lutas": futuro_sem_lutas,
        "passado_sem_lutas": passado_sem_lutas,
        "card_misto": card_misto,
        "futuro_realizado": futuro_realizado,
        "hoje_sem_lutas": hoje_sem_lutas,
    }


def test_list_events_when_upcoming_pega_evento_com_luta_agendada(db_session: Session) -> None:
    """``upcoming`` é evento com ao menos uma luta ``scheduled`` -- inclusive de data passada.

    Os seis eventos reais da DT-1 têm data passada e 100% das lutas ``scheduled``. Pela
    partição por data sozinha eles cairiam em "Realizados" e a página de detalhe deles,
    que decide o modo pelo ``status`` da luta, se declararia "futuro": o usuário clicaria
    em "Realizados" e cairia numa tela de previsão.
    """
    acervo = _semeia_acervo_da_particao(db_session)

    rows, _ = list_events(db_session, when="upcoming", limit=50, offset=0)

    ids = {row.id for row in rows}
    assert acervo["passado_so_agendado"].id in ids
    assert acervo["futuro_agendado"].id in ids
    assert acervo["card_misto"].id in ids


def test_list_events_when_upcoming_pega_evento_sem_lutas_com_data_futura(
    db_session: Session,
) -> None:
    """Segundo ramo de ``upcoming``: evento sem nenhuma luta cuja data não passou.

    O ramo existe pelo ``UFC 335`` (no banco sem nenhuma luta): sem ele, um evento futuro
    de card não publicado cairia em "Realizados". O evento **de hoje** entra pelo mesmo
    ramo -- ``date >= current_date`` --, porque o evento de hoje ainda não aconteceu.
    """
    acervo = _semeia_acervo_da_particao(db_session)

    rows, _ = list_events(db_session, when="upcoming", limit=50, offset=0)

    ids = {row.id for row in rows}
    assert acervo["futuro_sem_lutas"].id in ids
    assert acervo["hoje_sem_lutas"].id in ids


def test_list_events_when_past_pega_o_complemento_exato(db_session: Session) -> None:
    """``past`` é a **negação** de ``upcoming``: realizado, sem-lutas-e-já-passado, e nada mais.

    O evento de data futura com todas as lutas ``completed`` cai em ``past`` de propósito:
    a partição é derivada do ``status`` da luta, não da data -- quem escreve o status é a
    ingestão do resultado, então a classificação se auto-corrige quando o ``gap_sync`` roda.
    """
    acervo = _semeia_acervo_da_particao(db_session)

    rows, _ = list_events(db_session, when="past", limit=50, offset=0)

    assert {row.id for row in rows} == {
        acervo["passado_realizado"].id,
        acervo["passado_sem_lutas"].id,
        acervo["futuro_realizado"].id,
    }


def test_list_events_when_particiona_sem_sobreposicao(db_session: Session) -> None:
    """CA-11: os dois conjuntos são disjuntos, a união é o acervo e os ``total`` somam.

    A prova é sobre **conjuntos e ``total``**, nunca sobre ordem. Com ``past`` escrito como
    segundo predicado independente, bastaria um dos dois esquecer o ramo do evento sem lutas
    para um evento cair nos dois conjuntos (ou em nenhum) -- e nenhum teste de conjunto
    único acusaria.
    """
    _semeia_acervo_da_particao(db_session)

    todos, total = list_events(db_session, limit=50, offset=0)
    passados, total_passado = list_events(db_session, when="past", limit=50, offset=0)
    proximos, total_proximo = list_events(db_session, when="upcoming", limit=50, offset=0)

    ids_passado = {row.id for row in passados}
    ids_proximo = {row.id for row in proximos}
    assert ids_passado & ids_proximo == set()
    assert ids_passado | ids_proximo == {row.id for row in todos}
    assert total_passado + total_proximo == total


def test_list_events_when_total_reflete_o_conjunto_filtrado(db_session: Session) -> None:
    """O ``total`` de cada resposta conta o conjunto filtrado, não o acervo inteiro.

    Contar sem filtro faria a paginação de cada seção mentir enquanto os itens pareceriam
    certos -- defeito que nenhum teste de item acusaria.
    """
    _semeia_acervo_da_particao(db_session)

    rows, total = list_events(db_session, when="past", limit=1, offset=0)

    assert len(rows) == 1
    assert total == 3


def test_list_events_when_upcoming_ordena_crescente(db_session: Session) -> None:
    """``upcoming`` ordena por data **crescente** (o próximo evento primeiro)."""
    hoje = _hoje(db_session)
    mais_longe = _add_event(db_session, "UFC 336", hoje + timedelta(days=60))
    mais_perto = _add_event(db_session, "UFC 335", hoje + timedelta(days=10))

    rows, _ = list_events(db_session, when="upcoming", limit=50, offset=0)

    assert [row.id for row in rows] == [mais_perto.id, mais_longe.id]


def test_list_events_when_past_ordena_decrescente(db_session: Session) -> None:
    """``past`` e a ausência de ``when`` preservam a ordem decrescente (recentes primeiro)."""
    hoje = _hoje(db_session)
    antigo = _add_event(db_session, "UFC 290", hoje - timedelta(days=400))
    _add_bout(db_session, antigo, status=BoutStatus.COMPLETED)
    recente = _add_event(db_session, "UFC 300", hoje - timedelta(days=30))
    _add_bout(db_session, recente, status=BoutStatus.COMPLETED)

    passados, _ = list_events(db_session, when="past", limit=50, offset=0)
    sem_filtro, _ = list_events(db_session, limit=50, offset=0)

    assert [row.id for row in passados] == [recente.id, antigo.id]
    assert [row.id for row in sem_filtro] == [recente.id, antigo.id]


def test_get_latest_completed_event_escolhe_o_mais_recente(db_session: Session) -> None:
    """O último evento realizado é o mais recente com data passada e ao menos uma luta feita."""
    hoje = _hoje(db_session)
    antigo = _add_event(db_session, "UFC 290", hoje - timedelta(days=400))
    _add_bout(db_session, antigo, status=BoutStatus.COMPLETED)
    recente = _add_event(db_session, "UFC 300", hoje - timedelta(days=30))
    _add_bout(db_session, recente, status=BoutStatus.COMPLETED)

    assert get_latest_completed_event(db_session) is recente


def test_get_latest_completed_event_ignora_futuro_e_so_agendado(db_session: Session) -> None:
    """Ignora evento de data futura e evento de data passada cujas lutas são só agendadas.

    O "último evento" da Home nunca é um card que ainda vai acontecer, nem um card cujo
    resultado a ingestão ainda não trouxe -- nos dois casos não há desfecho a resumir.
    """
    hoje = _hoje(db_session)
    futuro = _add_event(db_session, "UFC 335", hoje + timedelta(days=30))
    _add_bout(db_session, futuro, status=BoutStatus.COMPLETED)
    so_agendado = _add_event(db_session, "UFC FN: Hooker vs Parnasse", hoje - timedelta(days=10))
    _add_bout(db_session, so_agendado, status=BoutStatus.SCHEDULED, winner_id=None, method=None)
    realizado = _add_event(db_session, "UFC 300", hoje - timedelta(days=200))
    _add_bout(db_session, realizado, status=BoutStatus.COMPLETED)

    assert get_latest_completed_event(db_session) is realizado


def test_get_latest_completed_event_aceita_card_misto(db_session: Session) -> None:
    """Card misto (uma realizada, uma agendada) conta: há desfecho a resumir."""
    hoje = _hoje(db_session)
    misto = _add_event(db_session, "UFC FN: card misto", hoje - timedelta(days=5))
    _add_bout(db_session, misto, status=BoutStatus.COMPLETED)
    _add_bout(db_session, misto, status=BoutStatus.SCHEDULED, winner_id=None, method=None)

    assert get_latest_completed_event(db_session) is misto


def test_get_latest_completed_event_acervo_vazio_devolve_none(db_session: Session) -> None:
    """Sem nenhum evento realizado no acervo, devolve ``None`` (o router traduz em 404)."""
    assert get_latest_completed_event(db_session) is None


def test_summarize_event_bouts_soma_dos_baldes_igual_a_n_bouts(db_session: Session) -> None:
    """Card com um de **cada** desfecho: a soma dos seis baldes é igual a ``n_bouts``.

    A identidade é o teste que falha **alto** no dia em que ``BoutMethod`` ganhar um membro
    novo. Sem o balde ``dq``, as 23 lutas por desqualificação do banco desapareceriam do
    resumo e a soma ficaria menor que o total sem erro nenhum.

    O empate mora na nulabilidade do vencedor (``winner_id`` nulo), não num
    ``BoutMethod.DRAW`` -- que **não existe**.
    """
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    vencedor = FighterFactory.build(name="Alex Pereira")
    db_session.add(vencedor)
    db_session.flush()
    _add_bout(
        db_session,
        event,
        status=BoutStatus.COMPLETED,
        method=BoutMethod.KO_TKO,
        winner_id=vencedor.id,
    )
    _add_bout(
        db_session,
        event,
        status=BoutStatus.COMPLETED,
        method=BoutMethod.SUBMISSION,
        winner_id=vencedor.id,
    )
    _add_bout(
        db_session,
        event,
        status=BoutStatus.COMPLETED,
        method=BoutMethod.DECISION,
        winner_id=vencedor.id,
    )
    _add_bout(
        db_session,
        event,
        status=BoutStatus.COMPLETED,
        method=BoutMethod.DQ,
        winner_id=vencedor.id,
    )
    _add_bout(
        db_session,
        event,
        status=BoutStatus.COMPLETED,
        method=BoutMethod.DECISION,
        winner_id=None,
    )
    _add_bout(
        db_session,
        event,
        status=BoutStatus.COMPLETED,
        method=BoutMethod.NO_CONTEST,
        winner_id=None,
    )

    resumo = summarize_event_bouts(list_event_bouts(db_session, event.id))

    assert resumo is not None
    assert resumo.n_bouts == 6
    assert resumo.ko_tko == 1
    assert resumo.submissions == 1
    assert resumo.decisions == 1
    assert resumo.dq == 1
    assert resumo.draws == 1
    assert resumo.no_contests == 1
    assert (
        resumo.ko_tko
        + resumo.submissions
        + resumo.decisions
        + resumo.draws
        + resumo.no_contests
        + resumo.dq
    ) == resumo.n_bouts


def test_summarize_event_bouts_empate_nao_cai_em_decisions(db_session: Session) -> None:
    """Empate (``winner_id`` nulo com ``method=decision``) entra em ``draws``, não em decisões."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    _add_bout(
        db_session,
        event,
        status=BoutStatus.COMPLETED,
        method=BoutMethod.DECISION,
        winner_id=None,
    )

    resumo = summarize_event_bouts(list_event_bouts(db_session, event.id))

    assert resumo is not None
    assert resumo.draws == 1
    assert resumo.decisions == 0


def test_summarize_event_bouts_card_so_agendado_devolve_none(db_session: Session) -> None:
    """Card inteiro agendado devolve ``None``: ausência declarada, nunca zeros.

    Zero finalização é uma **afirmação** sobre o evento; ausência de resultado não é zero.
    Contar por método sobre ``method = NULL`` produziria 0 KO, 0 finalização e 0 decisão --
    zeros que se leem como medição.
    """
    event = _add_event(db_session, "UFC 335", date(2026, 12, 12))
    _add_bout(db_session, event, status=BoutStatus.SCHEDULED, winner_id=None, method=None)
    _add_bout(db_session, event, status=BoutStatus.SCHEDULED, winner_id=None, method=None)

    assert summarize_event_bouts(list_event_bouts(db_session, event.id)) is None


def test_summarize_event_bouts_evento_sem_lutas_devolve_none(db_session: Session) -> None:
    """Evento sem nenhuma luta devolve ``None`` (o ``UFC 335``, id 813, está nesse estado)."""
    event = _add_event(db_session, "UFC 335", date(2026, 12, 12))

    assert summarize_event_bouts(list_event_bouts(db_session, event.id)) is None


def test_summarize_event_bouts_card_misto_conta_so_as_realizadas(db_session: Session) -> None:
    """Card misto conta só as lutas ``completed`` e declara ``n_bouts`` como denominador.

    É esse denominador que impede o card misto de se ler como o card inteiro na tela.
    """
    event = _add_event(db_session, "UFC FN: card misto", date(2024, 4, 13))
    vencedor = FighterFactory.build(name="Alex Pereira")
    db_session.add(vencedor)
    db_session.flush()
    _add_bout(
        db_session,
        event,
        status=BoutStatus.COMPLETED,
        method=BoutMethod.KO_TKO,
        winner_id=vencedor.id,
    )
    _add_bout(db_session, event, status=BoutStatus.SCHEDULED, winner_id=None, method=None)
    _add_bout(db_session, event, status=BoutStatus.SCHEDULED, winner_id=None, method=None)

    resumo = summarize_event_bouts(list_event_bouts(db_session, event.id))

    assert resumo is not None
    assert resumo.n_bouts == 1
    assert resumo.ko_tko == 1


def test_summarize_event_bouts_realizada_sem_metodo_fica_fora_e_avisa(
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Luta ``completed`` com ``method`` nulo fica fora de tudo e gera aviso no log.

    É estado impossível por construção -- o ``gap_sync`` grava desfecho e
    ``status='completed'`` juntos --, então silenciar o caso faria a soma dos baldes bater
    com um ``n_bouts`` que esconde a linha. O aviso vai por ``logging``, nunca ``print``.

    O ``disabled = False`` não é adorno: ``alembic/env.py`` chama ``fileConfig`` sem
    ``disable_existing_loggers=False``, e o default dessa função **desliga** todo logger já
    existente. Como ``tests/test_migrations.py`` carrega a config do Alembic, qualquer teste
    que rode depois dele encontra os loggers da aplicação mudos -- e este teste passava
    sozinho e falhava na suíte inteira. O ``monkeypatch`` restaura o estado no teardown; a
    correção na raiz (um argumento em ``alembic/env.py``) está reportada como follow-up,
    porque muda o comportamento de log de ``alembic upgrade`` e não é escopo desta sprint.
    """
    monkeypatch.setattr(logging.getLogger("apps.events.selectors"), "disabled", False)
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    vencedor = FighterFactory.build(name="Alex Pereira")
    db_session.add(vencedor)
    db_session.flush()
    _add_bout(
        db_session,
        event,
        status=BoutStatus.COMPLETED,
        method=BoutMethod.KO_TKO,
        winner_id=vencedor.id,
    )
    orfa = _add_bout(db_session, event, status=BoutStatus.COMPLETED, method=None, winner_id=None)

    with caplog.at_level(logging.WARNING, logger="apps.events.selectors"):
        resumo = summarize_event_bouts(list_event_bouts(db_session, event.id))

    assert resumo is not None
    assert resumo.n_bouts == 1
    assert str(orfa.id) in caplog.text


def _add_fighter(session: Session, name: str) -> Fighter:
    """Semeia um lutador com o nome dado e devolve o model persistido."""
    fighter = FighterFactory.build(name=name)
    session.add(fighter)
    session.flush()
    return fighter


def _add_luta_com_cantos(
    session: Session,
    event: Event,
    *,
    red: Fighter,
    blue: Fighter,
    status: BoutStatus = BoutStatus.COMPLETED,
    sig_red: int | None = None,
    sig_blue: int | None = None,
    td_red: int | None = None,
    td_blue: int | None = None,
    **campos: object,
) -> Bout:
    """Semeia uma luta com os dois cantos e as stats granulares sob prova.

    As stats entram **explícitas** (inclusive como ``None``): a factory sorteia valor para
    coluna numérica, e um sorteio no meio de um teste de destaque tornaria o vencedor do
    "mais golpes" não-determinístico.
    """
    bout = _add_bout(session, event, status=status, **campos)
    session.add_all(
        [
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=red.id,
                corner=Corner.RED,
                sig_strikes_landed=sig_red,
                takedowns_landed=td_red,
            ),
            BoutFighterFactory.build(
                bout_id=bout.id,
                fighter_id=blue.id,
                corner=Corner.BLUE,
                sig_strikes_landed=sig_blue,
                takedowns_landed=td_blue,
            ),
        ]
    )
    session.flush()
    return bout


def test_event_highlights_fastest_finish_ordena_por_round_e_depois_tempo(
    db_session: Session,
) -> None:
    """A finalização mais rápida ordena por ``(round, ending_time_seconds)``, nessa ordem.

    ``ending_time_seconds`` é o tempo **dentro** do round, não o decorrido total: ordenar só
    por segundos escolheria uma finalização de R3 aos 0:30 (30s) em vez de uma de R1 aos
    4:50 (290s) -- e erraria calado. Converter para tempo total exigiria assumir 300s por
    round, e o dado prova que isso é falso: 48 lutas de 1994-1999 têm round 1 passando de
    300s (o ``UFC Ultimate Ultimate '95`` tem 1080s num round só).
    """
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    red = _add_fighter(db_session, "Alex Pereira")
    blue = _add_fighter(db_session, "Jamahal Hill")
    r1 = _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.KO_TKO,
        winner_id=red.id,
        round=1,
        ending_time_seconds=290,
    )
    _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.SUBMISSION,
        winner_id=blue.id,
        round=3,
        ending_time_seconds=30,
    )

    destaques = collect_event_highlights(
        db_session, bouts=list_event_bouts(db_session, event.id), n_bouts=2
    )

    assert destaques is not None
    assert destaques.fastest_finish is not None
    assert destaques.fastest_finish.bout_id == r1.id
    assert destaques.fastest_finish.round == 1
    assert destaques.fastest_finish.ending_time_seconds == 290
    assert destaques.fastest_finish.method is BoutMethod.KO_TKO
    assert destaques.fastest_finish.fighter.id == red.id


def test_event_highlights_fastest_finish_ignora_decisao_e_no_contest(
    db_session: Session,
) -> None:
    """Decisão e no contest não são finalização; sem KO/TKO nem submissão o destaque é nulo."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    red = _add_fighter(db_session, "Alex Pereira")
    blue = _add_fighter(db_session, "Jamahal Hill")
    _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.DECISION,
        winner_id=red.id,
        round=3,
        ending_time_seconds=300,
    )
    _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.NO_CONTEST,
        winner_id=None,
        round=1,
        ending_time_seconds=10,
    )

    destaques = collect_event_highlights(
        db_session, bouts=list_event_bouts(db_session, event.id), n_bouts=2
    )

    assert destaques is not None
    assert destaques.fastest_finish is None


def test_event_highlights_volume_distingue_ausencia_de_zero_medido(db_session: Session) -> None:
    """``value: 0`` medido é destaque; ausência de linha granular é ``None``.

    A distinção é o coração da DT-2. Uma luta que acabou em 11 segundos com zero golpe
    significativo conectado é uma **medição**; ausência de dado é outra coisa, e tratar as
    duas igual é exatamente o defeito que falha calando.
    """
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    red = _add_fighter(db_session, "Alex Pereira")
    blue = _add_fighter(db_session, "Jamahal Hill")
    _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.KO_TKO,
        winner_id=red.id,
        round=1,
        ending_time_seconds=11,
        sig_red=0,
        sig_blue=0,
        td_red=None,
        td_blue=None,
    )

    destaques = collect_event_highlights(
        db_session, bouts=list_event_bouts(db_session, event.id), n_bouts=1
    )

    assert destaques is not None
    assert destaques.most_sig_strikes is not None
    assert destaques.most_sig_strikes.value == 0
    assert destaques.most_takedowns is None


def test_event_highlights_volume_escolhe_o_maior_por_lutador_por_luta(
    db_session: Session,
) -> None:
    """O destaque de volume é o maior valor por **lutador-por-luta**, não a soma da luta."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    red = _add_fighter(db_session, "Alex Pereira")
    blue = _add_fighter(db_session, "Jamahal Hill")
    outro = _add_fighter(db_session, "Max Holloway")
    _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.DECISION,
        winner_id=red.id,
        round=3,
        ending_time_seconds=300,
        sig_red=120,
        sig_blue=98,
        td_red=1,
        td_blue=0,
    )
    volumosa = _add_luta_com_cantos(
        db_session,
        event,
        red=outro,
        blue=blue,
        method=BoutMethod.DECISION,
        winner_id=outro.id,
        round=5,
        ending_time_seconds=300,
        sig_red=200,
        sig_blue=150,
        td_red=0,
        td_blue=7,
    )

    destaques = collect_event_highlights(
        db_session, bouts=list_event_bouts(db_session, event.id), n_bouts=2
    )

    assert destaques is not None
    assert destaques.most_sig_strikes is not None
    assert destaques.most_sig_strikes.bout_id == volumosa.id
    assert destaques.most_sig_strikes.fighter.id == outro.id
    assert destaques.most_sig_strikes.value == 200
    assert destaques.most_takedowns is not None
    assert destaques.most_takedowns.bout_id == volumosa.id
    assert destaques.most_takedowns.fighter.id == blue.id
    assert destaques.most_takedowns.value == 7


def test_event_highlights_luta_agendada_nao_entra_em_nenhum_destaque(
    db_session: Session,
) -> None:
    """Luta ``scheduled`` fica fora de todos os destaques, mesmo com stats semeadas."""
    event = _add_event(db_session, "UFC FN: card misto", date(2024, 4, 13))
    red = _add_fighter(db_session, "Alex Pereira")
    blue = _add_fighter(db_session, "Jamahal Hill")
    realizada = _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.DECISION,
        winner_id=red.id,
        round=3,
        ending_time_seconds=300,
        sig_red=50,
        sig_blue=40,
        td_red=1,
        td_blue=0,
    )
    _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        status=BoutStatus.SCHEDULED,
        winner_id=None,
        method=None,
        round=None,
        ending_time_seconds=None,
        sig_red=999,
        sig_blue=999,
        td_red=99,
        td_blue=99,
    )

    destaques = collect_event_highlights(
        db_session, bouts=list_event_bouts(db_session, event.id), n_bouts=1
    )

    assert destaques is not None
    assert destaques.most_sig_strikes is not None
    assert destaques.most_sig_strikes.bout_id == realizada.id
    assert destaques.most_sig_strikes.value == 50
    assert destaques.most_takedowns is not None
    assert destaques.most_takedowns.value == 1


def test_event_highlights_sem_luta_realizada_devolve_none(db_session: Session) -> None:
    """Card inteiro agendado devolve ``None`` -- o retorno inteiro, não destaques zerados."""
    event = _add_event(db_session, "UFC 335", date(2026, 12, 12))
    red = _add_fighter(db_session, "Alex Pereira")
    blue = _add_fighter(db_session, "Jamahal Hill")
    _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        status=BoutStatus.SCHEDULED,
        winner_id=None,
        method=None,
        round=None,
        ending_time_seconds=None,
    )

    assert (
        collect_event_highlights(
            db_session, bouts=list_event_bouts(db_session, event.id), n_bouts=0
        )
        is None
    )


def test_event_highlights_biggest_upset_le_o_registro_persistido(db_session: Session) -> None:
    """A maior zebra é **lida** de ``bout_predictions``, com o denominador declarado.

    Nunca recalculada com o modelo de hoje (D-01): o artefato atual descreveria um evento
    passado com um modelo que não existia na época.
    """
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    red = _add_fighter(db_session, "Alex Pereira")
    blue = _add_fighter(db_session, "Jamahal Hill")
    zebra = _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.KO_TKO,
        winner_id=blue.id,
        round=1,
        ending_time_seconds=180,
        sig_red=10,
        sig_blue=20,
        td_red=0,
        td_blue=0,
    )
    zebrinha = _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.DECISION,
        winner_id=blue.id,
        round=3,
        ending_time_seconds=300,
        sig_red=80,
        sig_blue=90,
        td_red=0,
        td_blue=0,
    )
    sem_predicao = _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.DECISION,
        winner_id=red.id,
        round=3,
        ending_time_seconds=300,
        sig_red=70,
        sig_blue=60,
        td_red=0,
        td_blue=0,
    )
    record_prediction(
        db_session,
        bout_id=zebra.id,
        predicted_winner_id=red.id,
        prob_predicted_winner=0.83,
        model_version="v1",
        n_features=57,
        source=SOURCE_WALK_FORWARD,
    )
    record_prediction(
        db_session,
        bout_id=zebrinha.id,
        predicted_winner_id=red.id,
        prob_predicted_winner=0.61,
        model_version="v1",
        n_features=57,
        source=SOURCE_WALK_FORWARD,
    )

    destaques = collect_event_highlights(
        db_session, bouts=list_event_bouts(db_session, event.id), n_bouts=3
    )

    assert destaques is not None
    assert destaques.biggest_upset is not None
    assert destaques.biggest_upset.bout_id == zebra.id
    assert destaques.biggest_upset.winner.id == blue.id
    assert destaques.biggest_upset.prob_of_winner == pytest.approx(1 - 0.83)
    assert destaques.biggest_upset.n_bouts_with_prediction == 2
    assert destaques.biggest_upset.n_bouts == 3
    assert sem_predicao.id != destaques.biggest_upset.bout_id


def test_event_highlights_biggest_upset_usa_a_predicao_mais_recente(db_session: Session) -> None:
    """Com duas versões de modelo na mesma luta, vale a predição de ``predicted_at`` mais novo.

    ``bout_predictions`` acumula uma linha por versão de modelo. Ler a antiga faria o
    destaque descrever uma afirmação que o modelo corrente já revisou.
    """
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    red = _add_fighter(db_session, "Alex Pereira")
    blue = _add_fighter(db_session, "Jamahal Hill")
    bout = _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.KO_TKO,
        winner_id=blue.id,
        round=1,
        ending_time_seconds=180,
        sig_red=10,
        sig_blue=20,
        td_red=0,
        td_blue=0,
    )
    record_prediction(
        db_session,
        bout_id=bout.id,
        predicted_winner_id=red.id,
        prob_predicted_winner=0.90,
        model_version="v1",
        n_features=39,
        source=SOURCE_WALK_FORWARD,
    )
    record_prediction(
        db_session,
        bout_id=bout.id,
        predicted_winner_id=red.id,
        prob_predicted_winner=0.55,
        model_version="v2",
        n_features=57,
        source=SOURCE_WALK_FORWARD,
    )

    destaques = collect_event_highlights(
        db_session, bouts=list_event_bouts(db_session, event.id), n_bouts=1
    )

    assert destaques is not None
    assert destaques.biggest_upset is not None
    assert destaques.biggest_upset.prob_of_winner == pytest.approx(1 - 0.55)


def test_event_highlights_biggest_upset_none_quando_o_modelo_acertou_todas(
    db_session: Session,
) -> None:
    """Modelo que acertou todas não produz zebra -- ``None``, nunca valor sobre amostra vazia."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    red = _add_fighter(db_session, "Alex Pereira")
    blue = _add_fighter(db_session, "Jamahal Hill")
    bout = _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.KO_TKO,
        winner_id=red.id,
        round=1,
        ending_time_seconds=180,
        sig_red=20,
        sig_blue=10,
        td_red=0,
        td_blue=0,
    )
    record_prediction(
        db_session,
        bout_id=bout.id,
        predicted_winner_id=red.id,
        prob_predicted_winner=0.72,
        model_version="v1",
        n_features=57,
        source=SOURCE_WALK_FORWARD,
    )

    destaques = collect_event_highlights(
        db_session, bouts=list_event_bouts(db_session, event.id), n_bouts=1
    )

    assert destaques is not None
    assert destaques.biggest_upset is None


def test_event_highlights_biggest_upset_none_sem_nenhuma_predicao(db_session: Session) -> None:
    """Card sem nenhuma predição registrada não tem zebra a declarar."""
    event = _add_event(db_session, "UFC 300", date(2024, 4, 13))
    red = _add_fighter(db_session, "Alex Pereira")
    blue = _add_fighter(db_session, "Jamahal Hill")
    _add_luta_com_cantos(
        db_session,
        event,
        red=red,
        blue=blue,
        method=BoutMethod.KO_TKO,
        winner_id=blue.id,
        round=1,
        ending_time_seconds=180,
        sig_red=10,
        sig_blue=20,
        td_red=0,
        td_blue=0,
    )

    destaques = collect_event_highlights(
        db_session, bouts=list_event_bouts(db_session, event.id), n_bouts=1
    )

    assert destaques is not None
    assert destaques.biggest_upset is None
