"""Testes do orçamento de chamadas à Cito -- CA-07.

Cobrem o contador de quota (``CallBudget``): ``charge`` incrementa o consumo a cada chamada
e, ao atingir o teto, levanta ``QuotaExceededError`` **sem** incrementar além do limite; e a
integração com o ``CitoClient`` em modo fixture, que cobra o orçamento antes de **cada** fetch
(o contador modela o custo real do free tier -- o modo fixture apenas evita a rede, não o
custo), interrompendo a chamada que estouraria o teto antes de servir o payload.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingestion.cito.client import CallBudget, CitoClient, QuotaExceededError

_FIXTURES = Path(__file__).parent / "fixtures"
_EVENT_ID = "ufc-319"


def test_call_budget_charge_incrementa_e_estoura_no_teto() -> None:
    """CA-07: ``charge`` conta cada chamada e estoura no teto sem passar do limite."""
    budget = CallBudget(limit=2)

    budget.charge()
    budget.charge()

    assert budget.used == 2
    with pytest.raises(QuotaExceededError):
        budget.charge()
    # A tentativa que estoura não é contabilizada -- o consumo permanece no teto.
    assert budget.used == 2


def test_client_budget_contabiliza_cada_fetch_e_interrompe_no_teto() -> None:
    """CA-07: o cliente em modo fixture cobra o orçamento por fetch e para ao estourar o teto."""
    budget = CallBudget(limit=1)
    client = CitoClient(
        token="", base_url="https://mmaapi.dev", fixture_dir=_FIXTURES, budget=budget
    )

    # A 1a chamada serve o payload e contabiliza uma unidade do orçamento.
    event = client.fetch_event(_EVENT_ID)
    assert event.event_id == _EVENT_ID
    assert budget.used == 1

    # A 2a chamada estouraria o teto: interrompe antes de servir, sem passar do limite.
    with pytest.raises(QuotaExceededError):
        client.get_fighter("dricus-du-plessis")
    assert budget.used == 1
