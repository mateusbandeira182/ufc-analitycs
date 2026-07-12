"""Serviço opcional da SPA buildada (``web/dist``) pelo próprio FastAPI.

No devcontainer só o backend sobe como servidor; a SPA é um build estático
(``npm run build`` -> ``web/dist``) servido por este mesmo processo -- um único
container entrega API e SPA. O mount é **condicional**: se ``web/dist/index.html``
não existe (CI, uso API-only), a app continua sendo só a API v1 e os testes não
são afetados.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

SPA_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


def mount_spa(app: FastAPI, dist: Path = SPA_DIST) -> bool:
    """Monta a SPA buildada em ``/`` se ela existir. Retorna se montou.

    A API ``/api/v1`` tem prioridade (incluída antes deste mount). Os assets
    versionados do Vite saem de ``/assets``; o restante cai no fallback que
    devolve ``index.html`` (client-side routing do React Router), exceto caminhos
    ``/api`` -- que mantêm o 404 JSON da API em vez de receber o HTML da SPA.
    """
    index = dist / "index.html"
    if not index.is_file():
        return False

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="spa-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        # Não sequestrar a API: um /api que chegou até aqui é 404 de verdade.
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        # Servir um arquivo real da raiz do build (ex.: octagon.svg), com proteção
        # contra path traversal; senão devolver index.html (rota client-side).
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)

    return True
