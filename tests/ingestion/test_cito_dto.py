"""Testes dos DTOs do endpoint real ``/api/v1/ufc/events/{slug}/stats`` da Cito -- CA-03.

Cobrem o desembrulho do envelope ``{success, data, meta}``, a leitura de camelCase por alias,
a tolerância a campo desconhecido (``extra="ignore"``) e a aplicação dos parsers na borda: as
strings ``"L of A"`` chegam ao domínio como ``(landed, attempted)`` e ``controlTime`` como
segundos -- nada de ``Any`` propagando para os tipos de ``boutStats``/``roundStats``.

A fixture deste arquivo é um **recorte verbatim** da captura real de UFC 319 (uma luta do
card, com os totais e o round-a-round daquela luta): os valores saem intactos da resposta da
API, só a forma de campo volta ao camelCase. Quem guarda a fidelidade ao payload **integral**
de produção é ``test_cito_contract_real_payload.py``; aqui o alvo é o parser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from ingestion.cito.dto import (
    CitoBoutStatLine,
    CitoEventStats,
    CitoRoundStatLine,
    CitoStatsEnvelope,
)

_FIXTURES = Path(__file__).parent / "fixtures"

# As linhas de stat da API real não trazem ``corner``; o lutador é identificado pelo slug.
# Os cantos são os que a Cito devolveu para UFC 319: Chimaev (o vencedor) é o vermelho.
_RED_SLUG = "khamzat-chimaev"
_BLUE_SLUG = "dricus-du-plessis"
_BOUT_ID = "12cedec11b37ddc0"


def _payload() -> dict[str, object]:
    raw = (_FIXTURES / "event_stats_ufc-319.json").read_text(encoding="utf-8")
    return json.loads(raw)  # type: ignore[no-any-return]


def test_envelope_desembrulha_data_tipada() -> None:
    """CA-03: o envelope valida ``success`` e expõe ``data`` como ``CitoEventStats`` tipado."""
    envelope = CitoStatsEnvelope.model_validate(_payload())

    assert envelope.success is True
    assert isinstance(envelope.data, CitoEventStats)
    assert len(envelope.data.bout_stats) == 2
    assert len(envelope.data.round_stats) == 10  # 5 rounds x 2 cantos, como a Cito devolveu


def test_bout_stats_le_camelcase_por_alias() -> None:
    """CA-03: campos camelCase (``boutId``, ``fighterSlug``) são lidos pelos aliases."""
    data = CitoStatsEnvelope.model_validate(_payload()).data
    by_slug = {line.fighter_slug: line for line in data.bout_stats}

    red = by_slug[_RED_SLUG]
    assert isinstance(red, CitoBoutStatLine)
    assert red.bout_id == _BOUT_ID
    assert red.fighter_slug == "khamzat-chimaev"
    assert red.knockdowns == 0


def test_split_string_vira_tupla_landed_attempted() -> None:
    """CA-03: ``"37 of 47"`` chega ao domínio como ``(37, 47)`` via parser na borda."""
    data = CitoStatsEnvelope.model_validate(_payload()).data
    by_slug = {line.fighter_slug: line for line in data.bout_stats}

    red = by_slug[_RED_SLUG]
    assert red.sig_strikes == (37, 47)
    assert red.total_strikes == (529, 567)
    assert red.head == (28, 36)
    assert red.takedowns == (12, 17)


def test_control_time_string_vira_segundos() -> None:
    """CA-03: ``controlTime`` ``"21:40"`` chega como ``1300`` segundos (21*60 + 40)."""
    data = CitoStatsEnvelope.model_validate(_payload()).data
    by_slug = {line.fighter_slug: line for line in data.bout_stats}

    assert by_slug[_RED_SLUG].control_time_seconds == 1300  # "21:40"
    assert by_slug[_BLUE_SLUG].control_time_seconds == 53  # "0:53"


def test_round_stats_tipadas_com_numero_do_round() -> None:
    """CA-03: cada ``roundStats`` vira ``CitoRoundStatLine`` com ``round`` e splits parseados."""
    data = CitoStatsEnvelope.model_validate(_payload()).data

    first = data.round_stats[0]
    assert isinstance(first, CitoRoundStatLine)
    assert first.round == 1
    # A ordem das linhas é a da resposta da Cito -- o canto azul vem primeiro neste card.
    assert first.fighter_slug == _BLUE_SLUG
    assert first.sig_strikes == (0, 0)
    assert first.control_time_seconds == 0


def test_campo_desconhecido_e_ignorado() -> None:
    """CA-03: ``extra="ignore"`` descarta campo não modelado sem estourar a validação.

    O campo exercitado (``lastSyncedAt``) é **real**: existe em toda linha de ``boutStats`` da
    captura íntegra de rede e não tem coluna correspondente. Um campo inventado só provaria que
    o Pydantic ignora o que ninguém manda.
    """
    raw = (_FIXTURES / "event_stats_ufc-fight-night-august-22-2026.json").read_text(
        encoding="utf-8"
    )
    payload = cast("dict[str, object]", json.loads(raw))
    event_data = cast("dict[str, object]", payload["data"])
    bout_stats = cast("list[dict[str, object]]", event_data["boutStats"])
    assert "lastSyncedAt" in bout_stats[0]

    linha = CitoStatsEnvelope.model_validate(payload).data.bout_stats[0]

    assert not hasattr(linha, "lastSyncedAt")
    assert not hasattr(linha, "last_synced_at")


def test_split_ausente_degrada_para_tupla_de_none() -> None:
    """CA-03: um split ausente no payload degrada para ``(None, None)`` -- sem inventar zero."""
    payload = _payload()
    event_data = cast("dict[str, object]", payload["data"])
    bout_stats = cast("list[dict[str, object]]", event_data["boutStats"])
    del bout_stats[0]["head"]

    data = CitoStatsEnvelope.model_validate(payload).data
    linha = next(line for line in data.bout_stats if line.fighter_slug == _BLUE_SLUG)

    assert linha.head == (None, None)
    assert linha.sig_strikes == (13, 29)  # os demais splits da linha seguem intactos
