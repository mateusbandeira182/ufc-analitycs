"""Testes do parse de base (guarda), da data de nascimento e da projeção do DTO -- CA-03, CA-05.

O cenário sai das capturas **verbatim** da Sprint 008-01, nunca de um dicionário inventado: é a
aplicação direta da lição ``licao-dto-sem-sondagem-real-sempre-errado`` (3 de 3 DTOs escritos sem
conferir a captura real estavam errados, sempre em silêncio).

Nenhum teste deste arquivo toca banco ou rede.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from apps.fighters.enums import Stance
from ingestion.ufc_official.anthropometry import (
    OfficialAnthropometry,
    UnknownStanceError,
    build_anthropometry,
    parse_stance,
)
from ingestion.ufc_official.dto import UfcOfficialFighter, parse_event, parse_fight

_FIXTURES = Path(__file__).parent / "fixtures" / "ufc_official"


def _canto(arquivo: str, fighter_id: int) -> UfcOfficialFighter:
    """Um canto do DTO, lido da captura verbatim -- nunca de uma forma reconstruída."""
    payload = json.loads((_FIXTURES / arquivo).read_text(encoding="utf-8"))
    identificador = int(arquivo.split("_")[-1].removesuffix(".json"))
    if arquivo.startswith("fight_live_"):
        fighters = list(parse_fight(payload, fight_id=identificador).fighters)
    else:
        evento = parse_event(payload, event_id=identificador)
        fighters = [canto for fight in evento.fight_card for canto in fight.fighters]
    return next(canto for canto in fighters if canto.fighter_id == fighter_id)


# --------------------------------------------------------------------------- #
# CA-03 -- base (guarda)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("rotulo", "stance"),
    [
        ("Orthodox", Stance.ORTHODOX),
        ("Southpaw", Stance.SOUTHPAW),
        ("Switch", Stance.SWITCH),
        # Espaço e caixa não são rótulo novo -- normalizar antes de decidir evita falha alta
        # por diferença cosmética.
        ("  southpaw ", Stance.SOUTHPAW),
    ],
)
def test_parse_stance_mapeia_os_rotulos_da_fonte(rotulo: str, stance: Stance) -> None:
    """CA-03: os três rótulos que a fonte publica viram o enum ``Stance``."""
    assert parse_stance(rotulo) is stance


def test_parse_stance_ausente_permanece_nulo() -> None:
    """CA-05: base ausente permanece nula -- nunca sentinela, nunca um default 'orthodox'."""
    assert parse_stance(None) is None


@pytest.mark.parametrize("rotulo", ["Sideways", "Open Stance"])
def test_parse_stance_rotulo_desconhecido_falha_alto(rotulo: str) -> None:
    """CA-03/RF-10: rótulo fora do enum levanta -- nunca degrada para ``None`` em silêncio.

    Os dois rótulos deste teste **existem de verdade** na fonte (medidos em 2026-09-01 sobre os
    1.354 payloads do cache local: ``Open Stance`` em 30 cantos de 7 lutadores, ``Sideways`` em
    6 cantos de 3). Não são valores inventados para exercitar o caminho de erro -- são a razão de
    o caminho de erro precisar existir, e de o backfill precisar contá-los em vez de estourar.

    É a diferença deliberada em relação a ``ingestion.entity_resolution._parse_stance``, que
    degrada para ``None``: ali o CSV é um snapshot congelado, aqui a fonte é viva.
    """
    with pytest.raises(UnknownStanceError, match=rotulo):
        parse_stance(rotulo)


# --------------------------------------------------------------------------- #
# CA-03 -- data de nascimento (parseada na borda, pelo DTO da Sprint 008-01)
# --------------------------------------------------------------------------- #


def test_dob_da_captura_verbatim_chega_como_data_de_calendario() -> None:
    """CA-03: ``DOB`` (string ISO na fonte) chega ao domínio como ``date``.

    O parse acontece no DTO da Sprint 008-01 (``date_of_birth``, ``strict=False``), não aqui --
    por isso esta slice **não** acrescentou um ``parse_date_of_birth``: seria uma função
    identidade sobre um valor que já chega tipado, e código que não faz nada é código que
    engana quem lê.
    """
    till = _canto("fight_live_10214.json", 2544)
    assert till.date_of_birth == date(1992, 12, 24)


# --------------------------------------------------------------------------- #
# CA-01, CA-03, CA-05 -- a projeção do canto no domínio
# --------------------------------------------------------------------------- #


def test_build_anthropometry_converte_o_canto_real_para_o_dominio() -> None:
    """CA-01/CA-03: o canto verbatim de Darren Till vira cm, kg, ``date`` e ``Stance``.

    Valores de origem na captura: ``Height`` 72.0 in, ``Reach`` 74.5 in, ``Weight`` 185.0 lb,
    ``DOB`` "1992-12-24", ``Stance`` "Southpaw".
    """
    till = _canto("fight_live_10214.json", 2544)

    assert build_anthropometry(till) == OfficialAnthropometry(
        ufc_fighter_id="2544",
        name="Darren Till",
        height_cm=183,
        reach_cm=189,
        weight_kg=83.91,
        date_of_birth=date(1992, 12, 24),
        stance=Stance.SOUTHPAW,
    )


def test_build_anthropometry_mantem_nulo_o_alcance_ausente_na_fonte() -> None:
    """CA-05: ``Reach`` nulo na fonte permanece nulo -- nunca zero, nunca sentinela.

    Roman Salazar, no card verbatim de UFC 184, é um dos lutadores reais sem alcance publicado:
    é ausência **na fonte**, não falha de casamento. A fonte só tem ``Reach`` para ~59,7% dos
    lutadores, e é daí que vem a cobertura de 36,4% que a SPEC previu para este campo.
    """
    salazar = _canto("event_live_700.json", 2331)

    projetado = build_anthropometry(salazar)

    assert projetado.reach_cm is None
    assert projetado.height_cm == 170  # 67 in x 2,54 = 170,18
    assert projetado.weight_kg == 61.23  # 135 lb x 0,45359237 = 61,2350...
