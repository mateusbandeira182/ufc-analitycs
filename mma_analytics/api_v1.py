"""Router agregador da API v1.

Ponto único onde os routers de cada app de domínio são montados sob ``/api/v1``.
As slices da SPEC acrescentam aqui seus routers (fighters, events, bouts, os
cruzamentos e o serving de predições), reusando este agregador.
"""

from __future__ import annotations

from fastapi import APIRouter

from apps.bouts.api import head_to_head_router
from apps.bouts.api import router as bouts_router
from apps.events.api import router as events_router
from apps.fighters.api import router as fighters_router
from apps.predictions.api import router as predictions_router

api_v1_router = APIRouter()
api_v1_router.include_router(fighters_router)
api_v1_router.include_router(events_router)
api_v1_router.include_router(bouts_router)
api_v1_router.include_router(head_to_head_router)
api_v1_router.include_router(predictions_router)
