"""Teste de contrato: os DTOs da fonte oficial acompanham o **payload real** da UFC.

As fixtures que este arquivo lê são capturas **cruas e verbatim** (``curl -o``, sem ``jq``, sem
pretty-print, sem edição) de 2026-09-01T19:19:47Z contra
``https://d29dxerjsp82wz.cloudfront.net``. A procedência completa está em
``tests/ingestion/fixtures/ufc_official/README.md``.

A distinção entre este arquivo e um teste de parser não é cosmética. Durante todo o M5 a suíte
ficou verde sobre fixtures inventadas que usavam ``sigStrikes`` e ``corner`` -- campos que a
Cito nunca devolveu. Um alias que não casa degrada em silêncio para ``None``, não levanta; só um
payload real, versionado sem edição, pega esse tipo de erro. A regra empírica registrada é dura:
3 de 3 DTOs escritos sem sondar a API real estavam errados (ver a lição
``licao-dto-sem-sondagem-real-sempre-errado``).

Nenhum teste deste arquivo toca a rede: as capturas são lidas do disco.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
from pydantic import BaseModel

from apps.bouts.enums import Corner
from ingestion.ufc_official.dto import (
    UfcOfficialContractError,
    UfcOfficialEvent,
    UfcOfficialFight,
    UfcOfficialFighter,
    parse_event,
    parse_fight,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official"

# UFC 282: Blachowicz vs. Ankalaev (2022-12-10). Dentro da janela da RF-03 e presente na nossa
# base (``events.id = 117``). Escolhido por ter DUAS lutas com vencedor no canto azul e um
# empate -- sem isso, o CA-03 ficaria afirmado e não demonstrado.
_EVENT_ID = 1124
# Till (vermelho, derrota) x Du Plessis (azul, VITÓRIA).
_BLUE_WINNER_FIGHT_ID = 10214
# UFC 184: Rousey vs Zingano -- a captura que prova data local != data UTC.
_LOCAL_DATE_EVENT_ID = 700
# UFC 331 (2026-09-19), ainda por acontecer: canto preenchido, desfecho nulo.
_UPCOMING_EVENT_ID = 1335


def _raw(filename: str) -> dict[str, object]:
    """Lê uma captura verbatim do disco, exatamente como o CDN a devolveu."""
    return json.loads((_FIXTURES / filename).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _raw_event(event_id: int) -> dict[str, object]:
    return _raw(f"event_live_{event_id}.json")


def _fight_da_captura() -> UfcOfficialFight:
    """A luta Till x Du Plessis vinda do **endpoint de luta**, não do card do evento."""
    return parse_fight(
        _raw(f"fight_live_{_BLUE_WINNER_FIGHT_ID}.json"), fight_id=_BLUE_WINNER_FIGHT_ID
    )


def test_dto_valida_captura_verbatim_do_endpoint_de_evento() -> None:
    """CA-01: a captura crua de ``/api/v3/event/live/1124.json`` valida nos DTOs.

    Doze lutas, identificador e nome do evento como a fonte os devolveu -- nada normalizado.
    """
    event = parse_event(_raw_event(_EVENT_ID), event_id=_EVENT_ID)

    assert event.event_id == _EVENT_ID
    assert event.name == "UFC 282: Blachowicz vs. Ankalaev"
    assert event.status == "Final"
    assert len(event.fight_card) == 12


def test_data_de_calendario_do_evento_e_a_local_e_nao_a_utc() -> None:
    """CA-01: ``StartTime`` é um **instante UTC**; a data do evento sai dele com o fuso.

    UFC 184 começou em ``2015-03-01T00:00Z`` com ``TimeZone`` ``GMT-08:00``: a data UTC é
    2015-03-01, mas a data real do evento -- a que a nossa base registra em
    ``events.id = 438`` -- é **2015-02-28**. Usar a data UTC erraria o dia em todo evento
    noturno das Américas. É a mesma armadilha já medida na Cito (``startsAt`` UTC divergindo
    de ``eventDate`` local, ADR 0005), e é a razão de o DTO não declarar a data como um
    ``date`` cru vindo do payload.
    """
    event = parse_event(_raw_event(_LOCAL_DATE_EVENT_ID), event_id=_LOCAL_DATE_EVENT_ID)

    assert event.start_time.date() == date(2015, 3, 1)
    assert event.time_zone == "GMT-08:00"
    assert event.local_date == date(2015, 2, 28)


def test_corner_e_outcome_sao_campos_distintos() -> None:
    """CA-03: o canto não é o desfecho renomeado -- há vencedor no canto azul na captura.

    É a razão de ser da SPEC 008: o ``corner`` da Cito mede 99,7% de vitórias do vermelho (ver
    ``licao-corner-cito-e-desfecho-nao-canto``) e, contra o canto verdadeiro, acerta 56,3% --
    vazava o rótulo que o modelo tenta prever. Se esta asserção passar a falhar, a fonte oficial
    regrediu para o mesmo defeito e a slice inteira perde o sentido.
    """
    event = parse_event(_raw_event(_EVENT_ID), event_id=_EVENT_ID)
    bout = next(fight for fight in event.fight_card if fight.fight_id == _BLUE_WINNER_FIGHT_ID)
    by_corner = {fighter.corner: fighter for fighter in bout.fighters}

    assert set(by_corner) == {Corner.RED, Corner.BLUE}
    assert by_corner[Corner.BLUE].name.last_name == "Du Plessis"
    assert by_corner[Corner.BLUE].outcome.label == "Win"
    assert by_corner[Corner.RED].name.last_name == "Till"
    assert by_corner[Corner.RED].outcome.label == "Loss"


def test_outcome_admite_empate_e_nao_e_booleano_do_canto() -> None:
    """CA-03: a luta principal da captura terminou em ``Draw`` nos DOIS cantos.

    Um terceiro valor de desfecho, idêntico nos dois lados, falsifica por construção qualquer
    leitura de ``Outcome`` como "vencedor" derivado do canto.
    """
    event = parse_event(_raw_event(_EVENT_ID), event_id=_EVENT_ID)
    main_event = next(fight for fight in event.fight_card if fight.fight_id == 10211)

    assert {fighter.outcome.label for fighter in main_event.fighters} == {"Draw"}
    assert {fighter.corner for fighter in main_event.fighters} == {Corner.RED, Corner.BLUE}


def test_canto_existe_antes_do_desfecho_em_evento_futuro() -> None:
    """CA-03: em evento ``Upcoming`` o canto está preenchido e o desfecho está **nulo**.

    A prova mais forte de que um campo não é derivado do outro: a UFC publica o canto de cada
    lutador antes de a luta acontecer, e o objeto ``Outcome`` vem presente com ``OutcomeId`` e
    ``Outcome`` nulos. Um DTO que declarasse o desfecho como obrigatório -- a leitura ingênua,
    e a que o esboço do plano trazia -- quebraria em todo evento futuro.
    """
    event = parse_event(_raw_event(_UPCOMING_EVENT_ID), event_id=_UPCOMING_EVENT_ID)

    assert event.status == "Upcoming"
    for fight in event.fight_card:
        assert {fighter.corner for fighter in fight.fighters} == {Corner.RED, Corner.BLUE}
        for fighter in fight.fighters:
            assert fighter.outcome.label is None
            assert fighter.outcome.outcome_id is None


def test_dto_valida_captura_verbatim_do_endpoint_de_luta() -> None:
    """CA-01: a captura crua de ``/api/v3/fight/live/10214.json`` valida nos DTOs.

    O endpoint de luta repete a mesma forma de ``Fighters[]`` do card do evento (conjunto de
    chaves idêntico, medido), então os dois caminhos compartilham ``UfcOfficialFighter``. O
    objeto de luta, esse, diverge: acrescenta ``Event``, ``OfficialStats``, ``FightStats`` e
    ``RoundStats``, e não tem ``FightNightTracking``.
    """
    fight = parse_fight(
        _raw(f"fight_live_{_BLUE_WINNER_FIGHT_ID}.json"), fight_id=_BLUE_WINNER_FIGHT_ID
    )

    assert fight.fight_id == _BLUE_WINNER_FIGHT_ID
    assert len(fight.fighters) == 2


def test_endpoint_de_luta_tambem_separa_canto_de_desfecho() -> None:
    """CA-03: a mesma distinção vale no endpoint de luta -- vencedor no canto azul."""
    fight = parse_fight(
        _raw(f"fight_live_{_BLUE_WINNER_FIGHT_ID}.json"), fight_id=_BLUE_WINNER_FIGHT_ID
    )
    by_corner = {fighter.corner: fighter for fighter in fight.fighters}

    assert set(by_corner) == {Corner.RED, Corner.BLUE}
    assert by_corner[Corner.BLUE].outcome.label == "Win"
    assert by_corner[Corner.RED].outcome.label == "Loss"


def test_antropometria_chega_na_unidade_da_borda_sem_conversao() -> None:
    """CA-04: a fonte devolve polegadas e libras; o DTO preserva a unidade no nome.

    A conversão para cm/kg é da Sprint 008-05. Converter aqui esconderia a unidade da borda --
    foi assim que a Cito devolveu polegadas onde o DTO esperava centímetros e passou
    despercebido até a SPEC 007.

    O alcance de Darren Till é ``74.5``: **meia polegada**. Além de provar que o valor não foi
    convertido (74,5 pol = 189,2 cm), prova que arredondar para inteiro na borda perderia
    informação real da fonte.
    """
    fight = parse_fight(
        _raw(f"fight_live_{_BLUE_WINNER_FIGHT_ID}.json"), fight_id=_BLUE_WINNER_FIGHT_ID
    )
    by_corner = {fighter.corner: fighter for fighter in fight.fighters}
    till = by_corner[Corner.RED]

    assert till.reach_inches == 74.5
    assert till.height_inches == 72.0
    assert till.weight_lbs == 185.0
    assert till.date_of_birth == date(1992, 12, 24)
    assert till.stance == "Southpaw"


def test_alcance_ausente_permanece_nulo() -> None:
    """CA-04: ``Reach`` falta em 50 de 450 lutadores sondados; ausência permanece **nula**.

    Nunca zero, nunca sentinela -- um alcance zero entraria como valor plausível numa feature
    numérica e envenenaria o modelo em silêncio.
    """
    event = parse_event(_raw_event(_LOCAL_DATE_EVENT_ID), event_id=_LOCAL_DATE_EVENT_ID)
    sem_alcance = [
        fighter
        for fight in event.fight_card
        for fighter in fight.fighters
        if fighter.reach_inches is None
    ]

    assert {fighter.name.last_name for fighter in sem_alcance} == {"Salazar", "Torres"}
    # A ausência é do alcance especificamente: altura e peso dos mesmos lutadores vieram.
    assert all(fighter.height_inches is not None for fighter in sem_alcance)
    assert all(fighter.weight_lbs is not None for fighter in sem_alcance)


def _sem_chave(payload: dict[str, object], caminho: str) -> dict[str, object]:
    """Copia a captura removendo uma chave estrutural indicada por caminho pontilhado."""
    mutado = deepcopy(payload)
    alvo, _, chave = caminho.rpartition(".")
    _no_caminho(mutado, alvo).pop(chave)
    return mutado


def _com_tipo_trocado(payload: dict[str, object], caminho: str, valor: object) -> dict[str, object]:
    """Copia a captura trocando o **tipo** do valor em um caminho pontilhado."""
    mutado = deepcopy(payload)
    alvo, _, chave = caminho.rpartition(".")
    _no_caminho(mutado, alvo)[chave] = valor
    return mutado


def _no_caminho(payload: object, caminho: str) -> dict[str, object]:
    """Navega um caminho pontilhado (índice numérico entra em lista) até um objeto."""
    atual = payload
    for parte in filter(None, caminho.split(".")):
        atual = atual[int(parte)] if isinstance(atual, list) else atual[parte]  # type: ignore[index]
    assert isinstance(atual, dict)
    return atual


_PRIMEIRO_LUTADOR = "LiveEventDetail.FightCard.0.Fighters.0"


@pytest.mark.parametrize(
    "caminho",
    [
        "LiveEventDetail.FightCard",
        "LiveEventDetail.EventId",
        "LiveEventDetail.StartTime",
        "LiveEventDetail.FightCard.0.Fighters",
        f"{_PRIMEIRO_LUTADOR}.Corner",
        f"{_PRIMEIRO_LUTADOR}.Outcome",
    ],
)
def test_campo_estrutural_ausente_falha_alto(caminho: str) -> None:
    """CA-02 / RF-10: ausência estrutural vira exceção tipada, nunca ``None`` silencioso.

    Um campo que some da fonte precisa **parar a ingestão**, não degradar para nulo: a coluna
    afetada aqui é o canto, que é o alvo do modelo.
    """
    payload = _sem_chave(_raw_event(_EVENT_ID), caminho)

    with pytest.raises(UfcOfficialContractError):
        parse_event(payload, event_id=_EVENT_ID)


@pytest.mark.parametrize(
    ("caminho", "valor"),
    [
        ("LiveEventDetail.EventId", "1124"),
        (f"{_PRIMEIRO_LUTADOR}.Height", "74.0"),
        (f"{_PRIMEIRO_LUTADOR}.Reach", "78"),
        (f"{_PRIMEIRO_LUTADOR}.FighterId", "2300"),
        ("LiveEventDetail.FightCard", {"0": "nao e uma lista"}),
        ("LiveEventDetail.Name", 1124),
    ],
)
def test_tipo_trocado_falha_alto_sem_coercao_silenciosa(caminho: str, valor: object) -> None:
    """CA-02 / RF-10: trocar o tipo de um campo **estoura**, não é coagido em silêncio.

    A validação lax do Pydantic aceitaria ``"5"`` onde se espera número e a mudança de contrato
    passaria despercebida -- exatamente a classe de erro que ficou meses invisível no M5. É este
    teste que decide a configuração estrita dos DTOs.
    """
    payload = _com_tipo_trocado(_raw_event(_EVENT_ID), caminho, valor)

    with pytest.raises(UfcOfficialContractError):
        parse_event(payload, event_id=_EVENT_ID)


def test_canto_com_rotulo_desconhecido_falha_alto() -> None:
    """CA-03: rótulo de canto fora de ``Red``/``Blue`` levanta -- nunca vira canto default.

    Um canto inventado grava dado falso na coluna que o modelo tenta prever; dado falso é pior
    que dado ausente.
    """
    payload = _com_tipo_trocado(_raw_event(_EVENT_ID), f"{_PRIMEIRO_LUTADOR}.Corner", "Purple")

    with pytest.raises(UfcOfficialContractError):
        parse_event(payload, event_id=_EVENT_ID)


def test_fuso_em_formato_desconhecido_falha_alto() -> None:
    """CA-02: ``TimeZone`` fora de ``GMT±HH:MM`` levanta em vez de degradar para UTC.

    Degradar para UTC deslocaria o evento em um dia (ver o caso do UFC 184) e a janela da RF-03
    passaria a ser decidida sobre a data errada.
    """
    payload = _com_tipo_trocado(_raw_event(_EVENT_ID), "LiveEventDetail.TimeZone", "PST")

    with pytest.raises(UfcOfficialContractError):
        parse_event(payload, event_id=_EVENT_ID)


def test_mensagem_de_erro_nomeia_o_identificador_e_o_endpoint() -> None:
    """CA-02: sem o id e o endpoint na mensagem, um erro de contrato vira caça ao tesouro.

    A Slice 02 varre centenas de ids; o log precisa dizer qual deles quebrou.
    """
    payload = _sem_chave(_raw_event(_EVENT_ID), "LiveEventDetail.FightCard")

    with pytest.raises(UfcOfficialContractError) as excinfo:
        parse_event(payload, event_id=_EVENT_ID)

    mensagem = str(excinfo.value)
    assert str(_EVENT_ID) in mensagem
    assert "event/live" in mensagem


def test_fixture_e_captura_crua_e_nao_forma_reconstruida() -> None:
    """CA-01: sobra payload que o DTO não declara -- prova de que não veio de um dump.

    Lição da SPEC 007: o ``.cache/`` da Cito guardava a forma DESSERIALIZADA, e por isso uma
    quebra de contrato ficou meses invisível. Uma fixture "limpa", com exatamente os campos que
    o DTO conhece, é indistinguível de uma fixture inventada -- e passaria neste arquivo inteiro
    sem nunca ter tocado a API real.
    """
    raw_event = _no_caminho(_raw_event(_EVENT_ID), "LiveEventDetail")
    raw_fight = _no_caminho(_raw_event(_EVENT_ID), "LiveEventDetail.FightCard.0")
    raw_fighter = _no_caminho(_raw_event(_EVENT_ID), _PRIMEIRO_LUTADOR)
    raw_do_endpoint_de_luta = _no_caminho(
        _raw(f"fight_live_{_BLUE_WINNER_FIGHT_ID}.json"), "LiveFightDetail"
    )

    assert set(raw_event) - _aliases(UfcOfficialEvent), "captura de evento suspeita de reconstrução"
    assert set(raw_fight) - _aliases(UfcOfficialFight), "captura de luta suspeita de reconstrução"
    assert set(raw_fighter) - _aliases(UfcOfficialFighter), (
        "captura de canto suspeita de reconstrução"
    )
    # ``FightStats``/``RoundStats`` são o portão condicional da Slice 07: existem na captura e o
    # DTO desta slice deliberadamente não os declara.
    assert {"FightStats", "RoundStats"} <= set(raw_do_endpoint_de_luta)


def _aliases(modelo: type[BaseModel]) -> set[str]:
    """Chaves do payload que o DTO declara -- alias quando há, nome do campo quando não."""
    return {field.alias or nome for nome, field in modelo.model_fields.items()}
