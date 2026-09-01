"""Testes do cache em disco resumível das páginas do catálogo Cito (Slice 03).

O catálogo custa ~17 chamadas (811 eventos, ``limit=50``) e é lido de novo pelas Slices 05 e 06.
O cache grava a **resposta crua** de cada página e, numa reexecução, serve do disco: sem chamar o
cliente e **sem cobrar o ``CallBudget``** -- a mesma regra que o ``EventStatsCache`` do M5 aplica.

A chave inclui ``limit``/``from``/``to``, porque páginas de recortes diferentes não são
intercambiáveis: a página 2 de ``limit=50`` não contém os mesmos eventos que a página 2 de
``limit=200``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ingestion.cito.cache import CatalogPageCache
from ingestion.cito.client import CallBudget, CitoClient

_FIXTURES = Path(__file__).parent / "fixtures"
_CATALOGO_PAGINADO = _FIXTURES / "catalog_paginado_derivado"


def _cliente(budget: CallBudget) -> CitoClient:
    return CitoClient(
        token="",
        base_url="https://api.citoapi.com",
        fixture_dir=_CATALOGO_PAGINADO,
        budget=budget,
    )


def test_miss_chama_o_fetch_e_grava_a_resposta_crua(tmp_path: Path) -> None:
    """Cache miss chama ``fetch``, grava o payload cru em disco e reporta ``hit=False``."""
    cache = CatalogPageCache(tmp_path)
    chamadas: list[str] = []

    def _fetch() -> object:
        chamadas.append("rede")
        return {"success": True, "data": [], "meta": {"page": 1}}

    payload, hit = cache.get_or_fetch("catalog_l50_p1", _fetch)

    assert hit is False
    assert chamadas == ["rede"]
    assert payload == {"success": True, "data": [], "meta": {"page": 1}}
    gravado = json.loads((tmp_path / "catalog_l50_p1.json").read_text(encoding="utf-8"))
    assert gravado == payload


def test_hit_le_do_disco_sem_chamar_o_fetch(tmp_path: Path) -> None:
    """Cache hit devolve o payload do disco sem invocar ``fetch`` (0 quota)."""
    cache = CatalogPageCache(tmp_path)
    cache.get_or_fetch("catalog_l50_p1", lambda: {"success": True, "data": [1]})

    def _explode() -> object:
        raise AssertionError("cache hit não pode chamar a rede")

    payload, hit = cache.get_or_fetch("catalog_l50_p1", _explode)

    assert hit is True
    assert payload == {"success": True, "data": [1]}


def test_fetch_event_catalog_com_cache_quente_nao_cobra_quota(tmp_path: Path) -> None:
    """A segunda execução sobre o cache quente devolve os mesmos itens gastando ZERO quota."""
    cache = CatalogPageCache(tmp_path)

    primeiro_budget = CallBudget(limit=10)
    primeiros = _cliente(primeiro_budget).fetch_event_catalog(cache=cache)

    segundo_budget = CallBudget(limit=10)
    segundos = _cliente(segundo_budget).fetch_event_catalog(cache=cache)

    assert primeiro_budget.used == 2
    assert segundo_budget.used == 0
    assert [item.slug for item in segundos] == [item.slug for item in primeiros]


def test_chave_do_cache_separa_limites_e_janelas_diferentes(tmp_path: Path) -> None:
    """Recortes diferentes não compartilham cache: a chave embute ``limit``/``from``/``to``.

    A página 2 de ``limit=50`` não contém os mesmos eventos que a página 2 de ``limit=200``;
    reusar o arquivo entre recortes serviria dado errado.
    """
    cache = CatalogPageCache(tmp_path)
    budget = CallBudget(limit=20)
    client = _cliente(budget)

    client.fetch_event_catalog(cache=cache)
    gastos_apos_primeiro_recorte = budget.used

    client.fetch_event_catalog(cache=cache, date_from=date(2026, 1, 1))

    assert budget.used > gastos_apos_primeiro_recorte
    nomes = sorted(p.name for p in tmp_path.glob("*.json"))
    assert len(nomes) > 2  # arquivos distintos por recorte
