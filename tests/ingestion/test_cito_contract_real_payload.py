"""Teste de contrato: o DTO da Cito acompanha o **payload real** do endpoint de stats.

Separado de ``test_cito_dto.py`` de propósito. Aquele guarda "o parser funciona" sobre uma
fixture sintética; este guarda "o DTO ainda descreve a API real", e a fixture que ele lê é a
resposta **integral e não editada** da sondagem autorizada de 2026-08-31 sobre
``GET /api/v1/ufc/events/ufc-fight-night-august-22-2026/stats`` (HTTP 200, ~118 KB, quatro
blocos ``{event, bouts, boutStats, roundStats}``).

A distinção não é cosmética: durante todo o M5 a suíte ficou verde sobre fixtures inventadas
que usavam ``sigStrikes`` e ``corner`` -- campos que a Cito nunca devolveu. Um alias que não
casa degrada em silêncio para ``(None, None)``, então só um payload real pega o erro.

Nenhum teste deste arquivo toca a rede: a fixture é lida do disco.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from apps.bouts.enums import Corner
from ingestion.cito.dto import CitoEventStats, CitoStatsEnvelope

_FIXTURES = Path(__file__).parent / "fixtures"
_REAL_SLUG = "ufc-fight-night-august-22-2026"


def _real_payload() -> dict[str, object]:
    """Payload integral da sondagem de 2026-08-31, como a Cito devolveu (sem poda)."""
    raw = (_FIXTURES / f"event_stats_{_REAL_SLUG}.json").read_text(encoding="utf-8")
    return json.loads(raw)  # type: ignore[no-any-return]


def _real_stats() -> CitoEventStats:
    return CitoStatsEnvelope.model_validate(_real_payload()).data


def test_dto_valida_payload_real_integral() -> None:
    """CA-01: o DTO valida o payload real inteiro -- 13 lutas, 26 totais, 52 rounds."""
    data = _real_stats()

    assert len(data.bouts) == 13
    assert len(data.bout_stats) == 26
    assert len(data.round_stats) == 52


def test_bloco_event_expoe_data_local_do_evento() -> None:
    """CA-01: ``data.event`` traz a data **local** do evento -- a premissa que a ADR 0004 negava.

    O ``startsAt`` é o instante UTC (2026-08-23T00:00:00Z) e difere em um dia da ``eventDate``
    local (2026-08-22), que é a data usada no slug. É exatamente essa divergência que torna
    insegura qualquer derivação de slug por regra sobre o nome persistido.
    """
    event = _real_stats().event

    assert event.slug == _REAL_SLUG
    assert event.title == "UFC Fight Night: Hernandez vs Rodrigues"
    assert event.status == "completed"
    assert event.event_date == date(2026, 8, 22)
    assert event.starts_at is not None
    assert event.starts_at.date() == date(2026, 8, 23)


def test_bloco_bouts_expoe_contexto_e_resultado_tipados() -> None:
    """CA-01: cada luta do card traz peso, seção, método, vencedor e tempo já tipados."""
    main_event = next(bout for bout in _real_stats().bouts if bout.bout_order == 1001)

    assert main_event.card_section == "Main Card"
    assert main_event.weight_class == "Middleweight"
    assert main_event.method == "Decision - Unanimous"
    assert main_event.winner_fighter_slug == "gregory-rodrigues"
    assert main_event.result_round == 5
    assert main_event.result_time_seconds == 300  # "5:00" parseado na borda
    assert main_event.title_bout is False
    assert main_event.is_cancelled is False


def test_corner_vem_do_bloco_bouts_e_cobre_os_dois_cantos() -> None:
    """CA-03: o canto vive em ``bouts[].fighters[]``, não na linha de stat, e é tipado.

    As linhas de ``boutStats``/``roundStats`` da API real **não** trazem ``corner``; o rótulo
    só existe no card. Por isso o matching passa a chavear por ``fighter_slug``.
    """
    main_event = next(bout for bout in _real_stats().bouts if bout.bout_order == 1001)
    by_corner = {fighter.corner: fighter for fighter in main_event.fighters}

    assert set(by_corner) == {Corner.RED, Corner.BLUE}
    assert by_corner[Corner.BLUE].fighter_slug == "anthony-hernandez"
    assert by_corner[Corner.RED].fighter_slug == "gregory-rodrigues"


def test_significant_strikes_e_total_strikes_chegam_parseados() -> None:
    """CA-02: ``significantStrikes``/``totalStrikes`` do payload real viram tuplas.

    O alias do M5 era ``sigStrikes``, que nunca casou com a API real -- todos os splits
    degradariam para ``(None, None)`` sem erro nenhum.
    """
    line = next(
        line
        for line in _real_stats().bout_stats
        if line.fighter_slug == "anthony-hernandez" and line.bout_id == "fb77b08a90b92d5b"
    )

    assert line.sig_strikes == (90, 159)
    assert line.total_strikes == (111, 184)
    assert line.takedowns == (3, 29)
    assert line.control_time_seconds == 640  # "10:40"


def test_total_strikes_existe_tambem_por_round() -> None:
    """CA-02: ``totalStrikes`` vem por round -- falsifica "a Cito não expõe total por round"."""
    line = next(
        line
        for line in _real_stats().round_stats
        if line.fighter_slug == "anthony-hernandez"
        and line.bout_id == "fb77b08a90b92d5b"
        and line.round == 1
    )

    assert line.sig_strikes == (10, 19)
    assert line.total_strikes == (10, 19)
    assert line.control_time_seconds == 168  # "2:48"


def test_nenhum_split_do_payload_real_degrada_para_none() -> None:
    """CA-02: sobre o payload real, nenhuma das 78 linhas cai para ``(None, None)``.

    É a guarda contra a regressão silenciosa: um alias que deixa de casar não quebra a
    validação, só zera o dado. Asserir a ausência de nulos é o que torna a falha visível.
    """
    data = _real_stats()

    for line in [*data.bout_stats, *data.round_stats]:
        assert line.sig_strikes != (None, None)
        assert line.total_strikes != (None, None)
        assert line.takedowns != (None, None)
        assert line.control_time_seconds is not None
