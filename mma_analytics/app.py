"""App factory FastAPI da MMA Analytics Platform.

``create_app`` monta a aplicação somente-leitura da API v1: inclui o agregador
``/api/v1`` e expõe o contrato OpenAPI em ``/docs`` e ``/openapi.json``. A versão
do OpenAPI é ``1``, casando com o prefixo de versionamento das rotas.
"""

from __future__ import annotations

from fastapi import FastAPI

from mma_analytics.api_v1 import api_v1_router
from mma_analytics.settings import settings
from mma_analytics.spa import mount_spa


def create_app() -> FastAPI:
    """Constrói a instância FastAPI com o router ``/api/v1`` montado.

    Se a SPA já estiver buildada em ``web/dist``, ela é servida por este mesmo
    processo (ver ``mount_spa``); sem o build (CI/uso API-only), a app é só a API.
    O mount da SPA vem **depois** do router para a API ter prioridade de rota, e é
    pulado sob ``APP_ENV=test`` para isolar a suíte da presença de um build local.
    """
    app = FastAPI(title="MMA Analytics API", version="1")
    app.include_router(api_v1_router, prefix="/api/v1")
    if settings.app_env != "test":
        mount_spa(app)
    return app
