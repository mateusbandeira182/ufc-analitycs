# Makefile -- gate único de qualidade do backend/ingestão da MMA Analytics Platform.
# `make ci` é o contrato entre o dev local e o CI (mesmos comandos).

.PHONY: ci lint typecheck test format security test-db run build-web

ci: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy mma_analytics apps ingestion analysis tests conftest.py alembic/env.py

test: test-db
	APP_ENV=test uv run pytest

format:
	uv run ruff format .
	uv run ruff check --fix .

security:
	uv run bandit -c pyproject.toml -r apps ingestion mma_analytics analysis
	uv run pip-audit

# Cria o banco de teste no Postgres compartilhado, se ainda não existir.
# Idempotente: o `|| true` absorve o erro de banco já existente.
test-db:
	@PGPASSWORD=$${DB_PASSWORD:-devpass} createdb \
		-h $${DB_HOST:-postgres} -U $${DB_USER:-devuser} ufc_bum_test 2>/dev/null || true

# Builda a SPA (web/dist) e sobe a API em foreground, servindo /api/v1 + a SPA.
# No devcontainer isso já roda em background via post-start.sh; este alvo é o
# atalho manual (ex.: após parar a API com pkill -f uvicorn).
run: build-web
	uv run uvicorn mma_analytics.app:create_app --factory --host 0.0.0.0 --port 8000 --reload

build-web:
	cd web && npm run build
