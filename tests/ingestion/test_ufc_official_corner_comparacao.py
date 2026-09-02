"""Testes da comparação read-only entre o canto persistido e o da fonte oficial (Sprint 008-04).

Cobrem CA-01, CA-02, CA-03 e CA-08 do Plano 008-04. A disciplina que estes testes guardam é a
do RF-02: a comparação é **estruturalmente** anterior a qualquer escrita -- ela recebe a
``Session``, lê, e devolve o diagnóstico sem tocar em nada. Por isso quase todo teste daqui
relê ``bout_fighters.corner`` **depois** da chamada e exige que continue como estava.

O cenário sai da captura **verbatim** de UFC 282: Blachowicz vs. Ankalaev
(``fixtures/ufc_official/event_live_1124.json``), com dois pares reais do card:

- ``FightId`` 10214 -- Darren Till (2544, **vermelho**) x Dricus Du Plessis (3599, **azul**);
- ``FightId`` 10212 -- Paddy Pimblett (3644, **vermelho**) x Jared Gordon (2890, **azul**).

Nenhum teste toca a rede: a execução é offline, lendo o diretório de fixtures como cache.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.bouts.enums import BoutMethod, Corner
from apps.bouts.models import Bout, BoutFighter
from apps.events.models import Event
from apps.fighters.models import Fighter
from ingestion.normalize import normalize_name
from ingestion.ufc_official import OFFICIAL_WINDOW_START
from ingestion.ufc_official.cache import OfficialEventCache
from ingestion.ufc_official.corner import (
    CornerComparison,
    OfficialCornerError,
    compare_event_corners,
    official_corners,
    run_corner_comparison,
)
from ingestion.ufc_official.dto import (
    UfcOfficialContractError,
    UfcOfficialFight,
    parse_event,
    parse_fight,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official"

# Os dois pares reais do card de UFC 282 usados como cenário: (FightId, vermelho, azul).
_TILL_X_DU_PLESSIS = (10214, "2544", "3599")
_PIMBLETT_X_GORDON = (10212, "3644", "2890")

# O evento persistido que corresponde à captura: está na base real com este nome e esta data.
_UFC_282 = ("UFC 282: Blachowicz vs. Ankalaev", date(2022, 12, 10), "1124")

# Nome de cada lutador dos dois pares, indexado pelo identificador externo da fonte.
_NOMES = {
    "2544": "Darren Till",
    "3599": "Dricus Du Plessis",
    "3644": "Paddy Pimblett",
    "2890": "Jared Gordon",
}


def _payload_do_evento(event_id: int) -> object:
    """Payload cru da captura verbatim do evento."""
    return json.loads((_FIXTURES / f"event_live_{event_id}.json").read_text(encoding="utf-8"))


def _luta_do_card(event_id: int, fight_id: int) -> object:
    """Recorta a luta ``fight_id`` do payload cru do evento, sem passar por DTO.

    Devolve o dicionário **cru** para que os testes de forma inválida possam mutá-lo antes da
    validação -- é assim que se prova que a fonte mudando de forma falha alto (RF-10).
    """
    detalhe = _payload_do_evento(event_id)
    assert isinstance(detalhe, dict)
    for luta in detalhe["LiveEventDetail"]["FightCard"]:
        if luta["FightId"] == fight_id:
            return luta
    raise AssertionError(f"Luta {fight_id} ausente da captura do evento {event_id}.")


# --------------------------------------------------------------------------- #
# CA-01 -- o canto da fonte vira o enum do domínio, e forma inesperada falha alto
# --------------------------------------------------------------------------- #


def test_canto_da_fonte_vira_o_enum_do_dominio_por_identificador() -> None:
    """CA-01: cada ``FighterId`` da luta oficial é projetado no ``Corner`` do domínio.

    O par vem indexado pelo identificador externo, e não pela ordem em que a fonte lista os
    cantos: a chave de casamento da slice é o par **não-ordenado** de ``ufc_fighter_id``.
    """
    fight_id, vermelho, azul = _TILL_X_DU_PLESSIS
    evento = parse_event(_payload_do_evento(1124), event_id=1124)
    (luta,) = [fight for fight in evento.fight_card if fight.fight_id == fight_id]

    assert official_corners(luta) == {vermelho: Corner.RED, azul: Corner.BLUE}


def test_canto_desconhecido_falha_alto_e_nunca_degrada_para_vermelho() -> None:
    """CA-01/RF-10: rótulo fora de ``Red``/``Blue`` levanta -- nunca vira canto default.

    Degradar aqui gravaria dado falso na coluna que é o **alvo** do modelo, que é exatamente o
    defeito do campo ``corner`` da Cito que a SPEC 008 existe para corrigir.
    """
    luta = _luta_do_card(1124, _TILL_X_DU_PLESSIS[0])
    assert isinstance(luta, dict)
    luta["Fighters"][0]["Corner"] = "Yellow"

    with pytest.raises(UfcOfficialContractError):
        parse_fight({"LiveFightDetail": luta}, fight_id=_TILL_X_DU_PLESSIS[0])


def test_par_oficial_com_os_dois_cantos_iguais_falha_alto() -> None:
    """CA-03: dois cantos do mesmo lado é payload malformado -- não se escolhe um deles.

    A luta tem dois lados por definição; se a fonte devolve ``Red`` nos dois, ela não está
    dizendo qual é qual. Adivinhar poria o lutador errado no vermelho.
    """
    luta = _luta_do_card(1124, _TILL_X_DU_PLESSIS[0])
    assert isinstance(luta, dict)
    luta["Fighters"][1]["Corner"] = "Red"
    parseada = parse_fight({"LiveFightDetail": luta}, fight_id=_TILL_X_DU_PLESSIS[0])

    with pytest.raises(OfficialCornerError):
        official_corners(parseada)


def test_luta_sem_exatamente_dois_cantos_falha_alto() -> None:
    """CA-03: sem os dois cantos não há par, e sem par não há chave de casamento."""
    luta = _luta_do_card(1124, _TILL_X_DU_PLESSIS[0])
    assert isinstance(luta, dict)
    luta["Fighters"] = luta["Fighters"][:1]
    parseada = parse_fight({"LiveFightDetail": luta}, fight_id=_TILL_X_DU_PLESSIS[0])

    with pytest.raises(OfficialCornerError):
        official_corners(parseada)


# --------------------------------------------------------------------------- #
# Cenário persistido: o evento de UFC 282 com os dois pares reais do card
# --------------------------------------------------------------------------- #


def _semeia_lutador(session: Session, nome: str, ufc_fighter_id: str | None) -> Fighter:
    lutador = Fighter(
        name=nome,
        name_normalized=normalize_name(nome),
        nickname=None,
        date_of_birth=None,
        height_cm=None,
        reach_cm=None,
        stance=None,
        weight_kg=None,
        wins=0,
        losses=0,
        draws=0,
        source="kaggle",
        ufc_fighter_id=ufc_fighter_id,
    )
    session.add(lutador)
    session.flush()
    return lutador


def _semeia_evento(
    session: Session,
    nome: str = _UFC_282[0],
    quando: date = _UFC_282[1],
    ufc_event_id: str | None = _UFC_282[2],
) -> Event:
    evento = Event(
        name=nome, date=quando, location=None, source="kaggle", ufc_event_id=ufc_event_id
    )
    session.add(evento)
    session.flush()
    return evento


def _semeia_luta(
    session: Session,
    evento: Event,
    vermelho: Fighter,
    azul: Fighter,
    *,
    winner: Fighter | None = None,
    source: str = "kaggle",
) -> Bout:
    """Uma luta com os dois cantos persistidos; ``vermelho``/``azul`` são o canto **na base**."""
    luta = Bout(
        event_id=evento.id,
        winner_id=None if winner is None else winner.id,
        method=BoutMethod.DECISION,
        round=None,
        ending_time_seconds=None,
        weight_class=None,
        source=source,
    )
    session.add(luta)
    session.flush()
    for canto, lutador in ((Corner.RED, vermelho), (Corner.BLUE, azul)):
        session.add(
            BoutFighter(bout_id=luta.id, fighter_id=lutador.id, corner=canto, source=source)
        )
    session.flush()
    return luta


def _cantos_persistidos(session: Session, bout_id: int) -> dict[int, Corner]:
    """Canto de cada lutador da luta, relido do banco -- a prova de que nada foi escrito."""
    linhas = session.scalars(select(BoutFighter).where(BoutFighter.bout_id == bout_id))
    return {linha.fighter_id: linha.corner for linha in linhas}


def _card_oficial(*fight_ids: int) -> list[UfcOfficialFight]:
    """As lutas pedidas do card oficial de UFC 282, já tipadas pelo DTO da Sprint 008-01."""
    evento = parse_event(_payload_do_evento(1124), event_id=1124)
    return [luta for luta in evento.fight_card if luta.fight_id in fight_ids]


# --------------------------------------------------------------------------- #
# CA-02 -- concordância não gera divergência nem escrita
# --------------------------------------------------------------------------- #


def test_canto_persistido_igual_ao_da_fonte_nao_gera_divergencia(db_session: Session) -> None:
    """CA-02: a luta é contada como comparada e concordante, e nada é escrito."""
    fight_id, vermelho, azul = _TILL_X_DU_PLESSIS
    evento = _semeia_evento(db_session)
    till = _semeia_lutador(db_session, _NOMES[vermelho], vermelho)
    du_plessis = _semeia_lutador(db_session, _NOMES[azul], azul)
    luta = _semeia_luta(db_session, evento, till, du_plessis)

    comparacao = compare_event_corners(db_session, evento, _card_oficial(fight_id))

    assert comparacao.compared_bouts == 1
    assert comparacao.agreeing_bouts == 1
    assert comparacao.divergences == ()
    assert comparacao.uncovered_bouts == ()
    assert _cantos_persistidos(db_session, luta.id) == {
        till.id: Corner.RED,
        du_plessis.id: Corner.BLUE,
    }


# --------------------------------------------------------------------------- #
# CA-01 -- divergência nomeada, e o banco intacto
# --------------------------------------------------------------------------- #


def test_divergencia_e_reportada_com_evento_data_lutadores_e_nada_e_escrito(
    db_session: Session,
) -> None:
    """CA-01: a divergência traz o que o humano precisa para julgar sem abrir o banco.

    E o banco continua exatamente como estava: a comparação é leitura pura (RF-02). A
    divergência vem em **par** -- os dois cantos da luta --, porque um lado só divergir seria
    par malformado, não divergência.
    """
    fight_id, vermelho, azul = _TILL_X_DU_PLESSIS
    evento = _semeia_evento(db_session)
    till = _semeia_lutador(db_session, _NOMES[vermelho], vermelho)
    du_plessis = _semeia_lutador(db_session, _NOMES[azul], azul)
    # Cantos INVERTIDOS em relação à fonte: Till está no vermelho na fonte, no azul aqui.
    luta = _semeia_luta(db_session, evento, du_plessis, till)

    comparacao = compare_event_corners(db_session, evento, _card_oficial(fight_id))

    assert comparacao.compared_bouts == 1
    assert comparacao.agreeing_bouts == 0
    assert len(comparacao.divergences) == 2
    do_till = next(d for d in comparacao.divergences if d.fighter_id == till.id)
    assert do_till.bout_id == luta.id
    assert do_till.event_name == _UFC_282[0]
    assert do_till.event_date == _UFC_282[1]
    assert do_till.fighter_name == _NOMES[vermelho]
    assert do_till.opponent_name == _NOMES[azul]
    assert do_till.persisted_corner is Corner.BLUE
    assert do_till.official_corner is Corner.RED
    assert do_till.persisted_source == "kaggle"
    assert _cantos_persistidos(db_session, luta.id) == {
        du_plessis.id: Corner.RED,
        till.id: Corner.BLUE,
    }


# --------------------------------------------------------------------------- #
# CA-03 -- o que a fonte não cobre é contado, nunca adivinhado
# --------------------------------------------------------------------------- #


def test_luta_com_lutador_sem_identificador_externo_e_contada_como_nao_coberta(
    db_session: Session,
) -> None:
    """CA-03: sem ``ufc_fighter_id`` nos dois cantos não há chave de casamento.

    A luta entra em ``uncovered_bouts``, nunca em ``divergences`` -- ausência de chave não é
    discordância, e tratá-la como tal inventaria correção onde não há dado.
    """
    fight_id, vermelho, azul = _TILL_X_DU_PLESSIS
    evento = _semeia_evento(db_session)
    till = _semeia_lutador(db_session, _NOMES[vermelho], None)
    du_plessis = _semeia_lutador(db_session, _NOMES[azul], azul)
    luta = _semeia_luta(db_session, evento, du_plessis, till)

    comparacao = compare_event_corners(db_session, evento, _card_oficial(fight_id))

    assert comparacao.uncovered_bouts == (luta.id,)
    assert comparacao.divergences == ()
    assert comparacao.compared_bouts == 0
    assert _cantos_persistidos(db_session, luta.id) == {
        du_plessis.id: Corner.RED,
        till.id: Corner.BLUE,
    }


def test_luta_ausente_do_card_oficial_e_contada_como_nao_coberta(db_session: Session) -> None:
    """CA-03: par persistido que não existe no card da fonte é pulado e contado."""
    _, vermelho, azul = _PIMBLETT_X_GORDON
    evento = _semeia_evento(db_session)
    pimblett = _semeia_lutador(db_session, _NOMES[vermelho], vermelho)
    gordon = _semeia_lutador(db_session, _NOMES[azul], azul)
    luta = _semeia_luta(db_session, evento, gordon, pimblett)

    # O card entregue tem só a OUTRA luta do evento.
    comparacao = compare_event_corners(db_session, evento, _card_oficial(_TILL_X_DU_PLESSIS[0]))

    assert comparacao.uncovered_bouts == (luta.id,)
    assert comparacao.divergences == ()


def test_par_oficial_malformado_e_contado_como_nao_coberto(db_session: Session) -> None:
    """CA-03: card com os dois cantos do mesmo lado não derruba a varredura -- vira não coberto.

    A luta é ilegível, e uma luta ilegível não pode custar as outras 7.506 da janela.
    """
    fight_id, vermelho, azul = _TILL_X_DU_PLESSIS
    evento = _semeia_evento(db_session)
    till = _semeia_lutador(db_session, _NOMES[vermelho], vermelho)
    du_plessis = _semeia_lutador(db_session, _NOMES[azul], azul)
    luta = _semeia_luta(db_session, evento, du_plessis, till)

    cru = _luta_do_card(1124, fight_id)
    assert isinstance(cru, dict)
    cru["Fighters"][1]["Corner"] = "Red"
    malformada = parse_fight({"LiveFightDetail": cru}, fight_id=fight_id)

    comparacao = compare_event_corners(db_session, evento, [malformada])

    assert comparacao.uncovered_bouts == (luta.id,)
    assert comparacao.divergences == ()


def test_par_persistido_com_os_dois_cantos_iguais_e_contado_como_nao_coberto(
    db_session: Session,
) -> None:
    """CA-03: a base sem um lado de cada também é ilegível -- não há o que comparar.

    Não há constraint em ``(bout_id, corner)`` (ADR 0001: a unicidade é
    ``(bout_id, fighter_id)``), então o caso é possível e precisa de tratamento explícito.
    """
    fight_id, vermelho, azul = _TILL_X_DU_PLESSIS
    evento = _semeia_evento(db_session)
    till = _semeia_lutador(db_session, _NOMES[vermelho], vermelho)
    du_plessis = _semeia_lutador(db_session, _NOMES[azul], azul)
    luta = _semeia_luta(db_session, evento, till, du_plessis)
    for linha in db_session.scalars(select(BoutFighter).where(BoutFighter.bout_id == luta.id)):
        linha.corner = Corner.RED
    db_session.flush()

    comparacao = compare_event_corners(db_session, evento, _card_oficial(fight_id))

    assert comparacao.uncovered_bouts == (luta.id,)
    assert comparacao.divergences == ()


# --------------------------------------------------------------------------- #
# CA-08 -- a varredura da janela não enxerga nada anterior a 2010-03-21
# --------------------------------------------------------------------------- #

# Um par real do card do evento 1002 (UFC Fight Night: Moraes vs. Sandhagen), usado para semear
# um evento **anterior** à fronteira que também divergiria se fosse comparado.
_MORAES_X_SANDHAGEN = (8723, "2887", "3055")
_NOMES_1002 = {"2887": "Marlon Moraes", "3055": "Cory Sandhagen"}


def _varredura(session: Session) -> CornerComparison:
    """Comparação da janela inteira em modo **offline**: lê as fixtures como cache, sem rede."""
    return run_corner_comparison(session, None, OfficialEventCache(_FIXTURES))


def _semeia_evento_divergente(
    session: Session,
    *,
    nome: str,
    quando: date,
    ufc_event_id: str,
    par: tuple[int, str, str],
    nomes: dict[str, str],
) -> Bout:
    """Semeia um evento cujo único par tem os cantos **invertidos** em relação à fonte."""
    _, vermelho, azul = par
    evento = _semeia_evento(session, nome, quando, ufc_event_id)
    da_fonte_vermelho = _semeia_lutador(session, nomes[vermelho], vermelho)
    da_fonte_azul = _semeia_lutador(session, nomes[azul], azul)
    return _semeia_luta(session, evento, da_fonte_azul, da_fonte_vermelho)


def test_varredura_ignora_evento_anterior_a_fronteira_e_reporta_o_do_dia_da_fronteira(
    db_session: Session,
) -> None:
    """CA-08: 2010-03-20 fica de fora da comparação; 2010-03-21 entra.

    A fronteira é limitação de fonte medida (o canto só passou a ser registrado no UFC Live:
    Vera vs Jones), não parâmetro de tuning. A data que decide é a do **evento persistido**; a
    captura entra apenas como card real do par comparado.
    """
    de_vespera = _semeia_evento_divergente(
        db_session,
        nome="Evento anterior à fronteira",
        quando=OFFICIAL_WINDOW_START - timedelta(days=1),
        ufc_event_id="1002",
        par=_MORAES_X_SANDHAGEN,
        nomes=_NOMES_1002,
    )
    do_dia = _semeia_evento_divergente(
        db_session,
        nome="Evento do dia da fronteira",
        quando=OFFICIAL_WINDOW_START,
        ufc_event_id="1124",
        par=_TILL_X_DU_PLESSIS,
        nomes=_NOMES,
    )

    comparacao = _varredura(db_session)

    assert comparacao.window_start == OFFICIAL_WINDOW_START
    assert {divergencia.bout_id for divergencia in comparacao.divergences} == {do_dia.id}
    assert de_vespera.id not in comparacao.uncovered_bouts
    assert _cantos_persistidos(db_session, de_vespera.id) == _cantos_persistidos(
        db_session, de_vespera.id
    )


def test_varredura_conta_evento_da_janela_sem_identificador_externo(db_session: Session) -> None:
    """CA-03: evento que a Slice 02 não mapeou é nomeado como não coberto, nunca resolvido aqui.

    Resolver por nome dentro desta slice reintroduziria exatamente a heurística que a Sprint
    008-02 aposentou; o teto da cobertura é dado, e dado se reporta.
    """
    _semeia_evento(db_session, "UFC 300: Pereira vs Hill", date(2024, 4, 13), None)

    comparacao = _varredura(db_session)

    assert comparacao.uncovered_events == ((1, "UFC 300: Pereira vs Hill", "sem ufc_event_id"),)


def test_varredura_conta_evento_cujo_payload_a_fonte_nao_devolveu(db_session: Session) -> None:
    """CA-03: id que a fonte não resolve nesta execução é contado, e nenhuma luta dele é tocada.

    A fonte responde **200 com envelope vazio** para id inexistente, nunca 404 -- a fixture
    ``event_live_1345.json`` é essa resposta, capturada verbatim.
    """
    luta = _semeia_evento_divergente(
        db_session,
        nome="Evento sem payload na fonte",
        quando=date(2024, 4, 13),
        ufc_event_id="1345",
        par=_TILL_X_DU_PLESSIS,
        nomes=_NOMES,
    )

    comparacao = _varredura(db_session)

    assert comparacao.divergences == ()
    assert comparacao.uncovered_events == (
        (1, "Evento sem payload na fonte", "sem payload na fonte"),
    )
    assert _cantos_persistidos(db_session, luta.id) != {}


def test_varredura_loga_cada_divergencia_com_evento_e_lutadores(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """CA-01: o relatório logado nomeia cada divergência -- é o que o humano inspeciona.

    Sem evento, data e os dois lutadores na linha do log, "21 divergências" seria um número,
    não uma lista inspecionável (RF-02).
    """
    _semeia_evento_divergente(
        db_session,
        nome=_UFC_282[0],
        quando=_UFC_282[1],
        ufc_event_id=_UFC_282[2],
        par=_TILL_X_DU_PLESSIS,
        nomes=_NOMES,
    )

    with caplog.at_level(logging.WARNING, logger="ingestion.ufc_official.corner"):
        _varredura(db_session)

    for esperado in (_UFC_282[0], str(_UFC_282[1]), _NOMES["2544"], _NOMES["3599"], "kaggle"):
        assert esperado in caplog.text
