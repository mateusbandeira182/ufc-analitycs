"""App factory FastAPI da MMA Analytics Platform.

``create_app`` monta a aplicação somente-leitura da API v1: inclui o agregador
``/api/v1`` e expõe o contrato OpenAPI em ``/docs`` e ``/openapi.json``. A versão
do OpenAPI é ``1``, casando com o prefixo de versionamento das rotas.
"""

from __future__ import annotations

from fastapi import FastAPI

from mma_analytics.api_v1 import api_v1_router


def create_app() -> FastAPI:
    """Constrói a instância FastAPI com o router ``/api/v1`` montado."""
    app = FastAPI(title="MMA Analytics API", version="1")
    app.include_router(api_v1_router, prefix="/api/v1")
    return app
