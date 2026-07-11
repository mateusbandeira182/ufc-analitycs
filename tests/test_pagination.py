"""Testes unitários do envelope de paginação reusável ``Page[T]``.

Puros (sem banco, sem app): validam a montagem do envelope e os defaults da
dependência ``page_params``. O teto de ``limit`` (rejeição 422) é exercitado pela
camada de API no teste do endpoint de fighters, onde a validação do FastAPI atua.
"""

from __future__ import annotations

from mma_analytics.pagination import DEFAULT_LIMIT, MAX_LIMIT, Page, PageParams, page_params


def test_page_monta_envelope() -> None:
    """``Page`` expõe ``items``, ``total``, ``limit`` e ``offset`` conforme informado."""
    page: Page[int] = Page(items=[1, 2, 3], total=42, limit=10, offset=20)

    assert page.items == [1, 2, 3]
    assert page.total == 42
    assert page.limit == 10
    assert page.offset == 20


def test_page_params_usa_defaults() -> None:
    """Sem argumentos, ``page_params`` devolve o ``limit`` padrão e ``offset`` zero."""
    params = page_params()

    assert params == PageParams(limit=DEFAULT_LIMIT, offset=0)
    assert DEFAULT_LIMIT <= MAX_LIMIT
