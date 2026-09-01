"""Contrato e extração do granular (``FightStats``/``RoundStats``) da fonte oficial da UFC.

As fixtures lidas aqui são capturas **cruas e verbatim** (``curl -o``, sem ``jq``, sem
pretty-print, sem edição) de 2026-09-01 contra ``https://d29dxerjsp82wz.cloudfront.net``. A
procedência está em ``tests/ingestion/fixtures/ufc_official/README.md``.

Nenhum nome de campo deste módulo foi suposto: todos saíram da captura. A regra empírica do
projeto é dura -- 3 de 3 DTOs escritos sem sondar a API real estavam errados, e sempre em
silêncio (``sigStrikes`` x ``significantStrikes`` degradava para ``(None, None)`` com a suíte
verde). Ver a lição ``licao-dto-sem-sondagem-real-sempre-errado``.

Nenhum teste deste arquivo toca a rede nem o banco.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import BaseModel

from ingestion.ufc_official.dto import (
    UfcOfficialContractError,
    UfcOfficialFightGranular,
    UfcOfficialStatLine,
    parse_fight_granular,
)
from ingestion.ufc_official.granular_measurement import (
    MEASURED_FIELDS,
    OfficialFighterNotInCardError,
    extract_official_lines,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official"

# Luta principal do evento de referência da medição (UFC Fight Night: Imavov vs. Borralho,
# ``eventId`` 1271, 2025-09-06): cinco rounds completos nos dois cantos, o card mais rico do
# evento. É a mesma luta que a medição do portão percorre.
_FIGHT_ID = 12204
_IMAVOV_ID = 3576
_BORRALHO_ID = 3658
# Luta de evento ``Upcoming`` (UFC 331, 2026-09-19): ``FightStats``/``RoundStats`` presentes e
# **vazios**. É a prova de que a chave é estrutural e só o conteúdo é que falta.
_UPCOMING_FIGHT_ID = 13017


def _raw(fight_id: int) -> dict[str, object]:
    """Lê uma captura verbatim do disco, exatamente como o CDN a devolveu."""
    return json.loads((_FIXTURES / f"fight_live_{fight_id}.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _granular(fight_id: int) -> UfcOfficialFightGranular:
    return parse_fight_granular(_raw(fight_id), fight_id=fight_id)


def test_dto_valida_captura_verbatim_do_granular() -> None:
    """CA-03: a captura crua de ``/api/v3/fight/live/12204.json`` valida nos DTOs de estatística.

    Dois cantos no bloco de totais, dois no bloco round-a-round, cinco rounds cada -- exatamente
    como a fonte devolveu.
    """
    fight = _granular(_FIGHT_ID)

    assert fight.fight_id == _FIGHT_ID
    assert [line.fighter_id for line in fight.fight_stats] == [_IMAVOV_ID, _BORRALHO_ID]
    assert [entry.fighter_id for entry in fight.round_stats] == [_IMAVOV_ID, _BORRALHO_ID]
    assert [[line.round_number for line in entry.rounds] for entry in fight.round_stats] == [
        [1, 2, 3, 4, 5],
        [1, 2, 3, 4, 5],
    ]


def test_estatistica_do_total_vem_como_a_fonte_devolveu() -> None:
    """CA-03: os campos base do bloco de totais, sem normalização nem derivação."""
    fight = _granular(_FIGHT_ID)
    imavov = next(line for line in fight.fight_stats if line.fighter_id == _IMAVOV_ID)

    assert imavov.knockdowns == 0
    assert imavov.sig_strikes_landed == 81
    assert imavov.sig_strikes_attempted == 162
    assert imavov.takedowns_landed == 0
    assert imavov.takedowns_attempted == 0
    assert imavov.submission_attempts == 0
    assert imavov.reversals == 0
    assert imavov.total_strikes_landed == 89
    assert imavov.head_landed == 53
    assert imavov.distance_landed == 79


def test_tempo_de_controle_vira_segundos_na_borda() -> None:
    """CA-03: ``ControlTime`` chega como ``"m:ss"`` e é convertido a segundos na fronteira.

    É o campo com maior chance de "divergir" por **forma** e não por conteúdo, e a unidade é o
    defeito que já passou despercebido neste projeto (polegadas onde o DTO esperava centímetros,
    SPEC 007). Converter na borda, com teste próprio, é o que impede a medição de registrar uma
    divergência que nunca existiu.
    """
    fight = _granular(_FIGHT_ID)
    by_id = {line.fighter_id: line for line in fight.fight_stats}

    assert by_id[_IMAVOV_ID].control_time_seconds == 29  # "0:29"
    assert by_id[_BORRALHO_ID].control_time_seconds == 80  # "1:20"


def test_round_a_round_repete_a_forma_do_total_mais_o_numero_do_round() -> None:
    """CA-03: a linha de round tem os mesmos campos do total, acrescida de ``RoundNumber``."""
    fight = _granular(_FIGHT_ID)
    borralho = next(entry for entry in fight.round_stats if entry.fighter_id == _BORRALHO_ID)
    primeiro = borralho.rounds[0]

    assert primeiro.round_number == 1
    assert primeiro.sig_strikes_landed == 6
    assert primeiro.sig_strikes_attempted == 22
    assert primeiro.control_time_seconds == 51  # "0:51"


def test_luta_nao_realizada_tem_blocos_presentes_e_vazios() -> None:
    """CA-03: em luta ``Upcoming`` os dois blocos vêm **presentes e vazios**, nunca ausentes.

    Medido em 14 lutas capturadas em 2026-09-01 (13 ``Final`` do evento de referência e uma
    ``Upcoming`` do UFC 331): as chaves ``FightStats`` e ``RoundStats`` existem em todas. Por
    isso elas são **estruturais** no DTO -- um rename estoura -- e a lista vazia é a forma
    legítima de dizer "esta luta ainda não aconteceu".
    """
    fight = _granular(_UPCOMING_FIGHT_ID)

    assert fight.fight_stats == []
    assert fight.round_stats == []


@pytest.mark.parametrize(
    "caminho",
    [
        "LiveFightDetail.FightStats",
        "LiveFightDetail.RoundStats",
        "LiveFightDetail.FightStats.0.FighterId",
        "LiveFightDetail.FightStats.0.SigStrikesLanded",
        "LiveFightDetail.FightStats.0.ControlTime",
        "LiveFightDetail.RoundStats.0.Rounds",
        "LiveFightDetail.RoundStats.0.Rounds.0.RoundNumber",
        "LiveFightDetail.RoundStats.0.Rounds.0.TakedownsLanded",
    ],
)
def test_campo_estrutural_de_estatistica_ausente_falha_alto(caminho: str) -> None:
    """CA-03 / RF-10: campo de estatística que some vira exceção tipada, nunca ``None``.

    Se ``SigStrikesLanded`` fosse opcional, um rename na fonte degradaria para nulo, sairia do
    denominador da concordância pela regra "ausência não é divergência" e a medição informaria
    100% sobre um campo que deixou de existir. Falhar alto é o que impede esse resultado.
    """
    payload = _sem_chave(_raw(_FIGHT_ID), caminho)

    with pytest.raises(UfcOfficialContractError):
        parse_fight_granular(payload, fight_id=_FIGHT_ID)


@pytest.mark.parametrize(
    ("caminho", "valor"),
    [
        ("LiveFightDetail.FightStats.0.SigStrikesLanded", "81"),
        ("LiveFightDetail.FightStats.0.Knockdowns", 0.5),
        ("LiveFightDetail.FightStats.0.ControlTime", 29),
        ("LiveFightDetail.RoundStats.0.Rounds.0.RoundNumber", "1"),
        ("LiveFightDetail.RoundStats", {"0": "nao e uma lista"}),
    ],
)
def test_tipo_trocado_em_estatistica_falha_alto(caminho: str, valor: object) -> None:
    """CA-03 / RF-10: trocar o tipo de um campo de estatística estoura, sem coerção silenciosa."""
    payload = _com_valor(_raw(_FIGHT_ID), caminho, valor)

    with pytest.raises(UfcOfficialContractError):
        parse_fight_granular(payload, fight_id=_FIGHT_ID)


def test_tempo_de_controle_em_formato_desconhecido_falha_alto() -> None:
    """CA-03: ``ControlTime`` fora de ``"m:ss"`` levanta -- nunca vira zero nem nulo.

    Um tempo de controle zerado por engano é pior que ausente: entra como valor plausível numa
    comparação numérica e inventa divergência (ou concordância) onde não há dado.
    """
    payload = _com_valor(_raw(_FIGHT_ID), "LiveFightDetail.FightStats.0.ControlTime", "29 s")

    with pytest.raises(UfcOfficialContractError):
        parse_fight_granular(payload, fight_id=_FIGHT_ID)


def test_mensagem_de_erro_nomeia_o_endpoint_com_o_identificador() -> None:
    """A medição percorre 13 lutas por evento -- erro sem o id vira caça ao tesouro no log."""
    payload = _sem_chave(_raw(_FIGHT_ID), "LiveFightDetail.FightStats")

    with pytest.raises(UfcOfficialContractError, match=r"/api/v3/fight/live/12204\.json"):
        parse_fight_granular(payload, fight_id=_FIGHT_ID)


def test_extracao_achata_total_e_rounds_em_linhas_comparaveis() -> None:
    """CA-03: a extração achata ``FightStats`` + ``RoundStats`` em ``GranularLine``.

    ``round=None`` identifica o total da luta; ``round=N`` identifica o round N. A chave é o
    **nome normalizado**, nunca o canto -- o canto é exatamente o campo sob suspeita nas duas
    fontes e é o que a Sprint 008-04 corrige.
    """
    lines = extract_official_lines(_granular(_FIGHT_ID))

    chaves = {(line.fighter_name_normalized, line.round) for line in lines}
    assert ("nassourdine imavov", None) in chaves
    assert ("caio borralho", None) in chaves
    assert {(nome, rodada) for nome, rodada in chaves if rodada is not None} == {
        (nome, rodada) for nome in ("nassourdine imavov", "caio borralho") for rodada in range(1, 6)
    }
    assert len(lines) == 12  # 2 totais + 2 cantos x 5 rounds


def test_extracao_preenche_os_22_campos_medidos() -> None:
    """CA-03: cada linha extraída traz os 22 campos que a medição compara, sem buraco."""
    lines = extract_official_lines(_granular(_FIGHT_ID))
    total_imavov = next(
        line
        for line in lines
        if line.fighter_name_normalized == "nassourdine imavov" and line.round is None
    )

    assert set(total_imavov.values) == set(MEASURED_FIELDS)
    assert total_imavov.values["sig_strikes_landed"] == 81
    assert total_imavov.values["control_time_seconds"] == 29
    assert total_imavov.values["head_landed"] == 53
    assert all(valor is not None for valor in total_imavov.values.values())


def test_extracao_de_luta_nao_realizada_devolve_nenhuma_linha() -> None:
    """Luta sem estatística não produz linha -- ausência nunca vira linha zerada."""
    assert extract_official_lines(_granular(_UPCOMING_FIGHT_ID)) == []


def test_estatistica_de_lutador_fora_do_card_falha_alto() -> None:
    """Nunca casar no escuro: estatística de um ``FighterId`` que não está no card levanta.

    Sem o lutador em ``Fighters[]`` não há nome, e sem nome não há chave de casamento. Deixar a
    linha passar silenciosamente removeria dado da medição sem que ninguém soubesse.
    """
    payload = _com_valor(_raw(_FIGHT_ID), "LiveFightDetail.FightStats.0.FighterId", 999999)

    with pytest.raises(OfficialFighterNotInCardError, match="999999"):
        extract_official_lines(parse_fight_granular(payload, fight_id=_FIGHT_ID))


def test_fixture_do_granular_e_captura_crua_e_nao_forma_reconstruida() -> None:
    """CA-03: sobra payload que o DTO não declara -- prova de que não veio de um dump.

    Uma fixture "limpa", com exatamente os campos que o DTO conhece, é indistinguível de uma
    fixture inventada e passaria neste arquivo inteiro sem nunca ter tocado a API real.
    """
    detalhe = _no_caminho(_raw(_FIGHT_ID), "LiveFightDetail")
    linha_de_total = _no_caminho(_raw(_FIGHT_ID), "LiveFightDetail.FightStats.0")

    assert set(detalhe) - _aliases(UfcOfficialFightGranular), "captura suspeita de reconstrução"
    # A fonte devolve ~65 campos por linha de estatística (tempo por posição, acurácias,
    # controle por posição); a medição compara 22. O resto precisa continuar no arquivo.
    assert set(linha_de_total) - _aliases(UfcOfficialStatLine) - {"FighterId"}


def _sem_chave(payload: dict[str, object], caminho: str) -> dict[str, object]:
    """Copia a captura removendo uma chave indicada por caminho pontilhado."""
    mutado = deepcopy(payload)
    alvo, _, chave = caminho.rpartition(".")
    _no_caminho(mutado, alvo).pop(chave)
    return mutado


def _com_valor(payload: dict[str, object], caminho: str, valor: object) -> dict[str, object]:
    """Copia a captura trocando o valor em um caminho pontilhado."""
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


def _aliases(modelo: type[BaseModel]) -> set[str]:
    """Chaves do payload que o DTO declara -- alias quando há, nome do campo quando não."""
    return {field.alias or nome for nome, field in modelo.model_fields.items()}
