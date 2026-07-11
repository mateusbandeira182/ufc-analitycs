"""Paginação limit/offset reusável por todas as listas da API v1.

Define o envelope genérico ``Page[T]`` (``items``, ``total``, ``limit``,
``offset``) e a dependência ``page_params``, que valida ``limit``/``offset`` com
teto (o FastAPI rejeita ``limit`` fora da faixa com 422). Padrão estabelecido na
Slice 01 e reusado nas slices seguintes -- não reinventar por endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel

T = TypeVar("T")

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


class Page(BaseModel, Generic[T]):
    """Envelope de uma página de resultados de leitura."""

    items: list[T]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class PageParams:
    """Parâmetros de paginação já validados (limit dentro do teto, offset >= 0)."""

    limit: int
    offset: int


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    """Dependência FastAPI que valida e agrupa os parâmetros de paginação."""
    return PageParams(limit=limit, offset=offset)
