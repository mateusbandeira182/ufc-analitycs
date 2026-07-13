"""Testes dos DTOs do endpoint real ``/events/{id}/stats`` da Cito -- CA-03.

Cobrem o desembrulho do envelope ``{success, data, meta}``, a leitura de camelCase por alias,
a tolerância a campo desconhecido (``extra="ignore"``) e a aplicação dos parsers na borda: as
strings ``"L of A"`` chegam ao domínio como ``(landed, attempted)`` e ``controlTime`` como
segundos -- nada de ``Any`` propagando para os tipos de ``boutStats``/``roundStats``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from apps.bouts.enums import Corner
from ingestion.cito.dto import (
    CitoBoutStatLine,
    CitoEventStats,
    CitoRoundStatLine,
    CitoStatsEnvelope,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _payload() -> dict[str, object]:
    raw = (_FIXTURES / "event_stats_ufc-319.json").read_text(encoding="utf-8")
    return json.loads(raw)  # type: ignore[no-any-return]


def test_envelope_desembrulha_data_tipada() -> None:
    """CA-03: o envelope valida ``success`` e expõe ``data`` como ``CitoEventStats`` tipado."""
    envelope = CitoStatsEnvelope.model_validate(_payload())

    assert envelope.success is True
    assert isinstance(envelope.data, CitoEventStats)
    assert len(envelope.data.bout_stats) == 2
    assert len(envelope.data.round_stats) == 2


def test_bout_stats_le_camelcase_por_alias() -> None:
    """CA-03: campos camelCase (``boutId``, ``fighterSlug``) são lidos pelos aliases."""
    data = CitoStatsEnvelope.model_validate(_payload()).data
    by_corner = {line.corner: line for line in data.bout_stats}

    red = by_corner[Corner.RED]
    assert isinstance(red, CitoBoutStatLine)
    assert red.bout_id == "ufc-319-bout-1"
    assert red.fighter_slug == "dricus-du-plessis"
    assert red.knockdowns == 0


def test_split_string_vira_tupla_landed_attempted() -> None:
    """CA-03: ``"41 of 120"`` chega ao domínio como ``(41, 120)`` via parser na borda."""
    data = CitoStatsEnvelope.model_validate(_payload()).data
    by_corner = {line.corner: line for line in data.bout_stats}

    red = by_corner[Corner.RED]
    assert red.sig_strikes == (41, 120)
    assert red.head == (20, 80)
    assert red.takedowns == (0, 2)


def test_control_time_string_vira_segundos() -> None:
    """CA-03: ``controlTime`` ``"21:40"`` chega como ``1300`` segundos (21*60 + 40)."""
    data = CitoStatsEnvelope.model_validate(_payload()).data
    by_corner = {line.corner: line for line in data.bout_stats}

    assert by_corner[Corner.RED].control_time_seconds == 30
    assert by_corner[Corner.BLUE].control_time_seconds == 1300


def test_round_stats_tipadas_com_numero_do_round() -> None:
    """CA-03: cada ``roundStats`` vira ``CitoRoundStatLine`` com ``round`` e splits parseados."""
    data = CitoStatsEnvelope.model_validate(_payload()).data

    first = data.round_stats[0]
    assert isinstance(first, CitoRoundStatLine)
    assert first.round == 1
    assert first.corner == Corner.RED
    assert first.sig_strikes == (12, 30)
    assert first.control_time_seconds == 10


def test_campo_desconhecido_e_ignorado() -> None:
    """CA-03: ``extra="ignore"`` descarta ``unknownField`` sem estourar a validação."""
    data = CitoStatsEnvelope.model_validate(_payload()).data
    red = next(line for line in data.bout_stats if line.corner == Corner.RED)

    assert not hasattr(red, "unknownField")
    assert not hasattr(red, "unknown_field")


def test_split_ausente_degrada_para_tupla_de_none() -> None:
    """CA-03: um split ausente no payload degrada para ``(None, None)`` -- sem inventar zero."""
    payload = _payload()
    event_data = cast("dict[str, object]", payload["data"])
    bout_stats = cast("list[dict[str, object]]", event_data["boutStats"])
    del bout_stats[0]["head"]

    data = CitoStatsEnvelope.model_validate(payload).data
    red = next(line for line in data.bout_stats if line.corner == Corner.RED)

    assert red.head == (None, None)
    assert red.sig_strikes == (41, 120)
