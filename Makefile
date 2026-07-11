# Makefile -- gate único de qualidade do backend/ingestão da MMA Analytics Platform.
# `make ci` é o contrato entre o dev local e o CI (mesmos comandos).

.PHONY: ci lint typecheck test format security test-db

ci: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy mma_analytics apps ingestion tests conftest.py alembic/env.py

test: test-db
	APP_ENV=test uv run pytest

format:
	uv run ruff format .
	uv run ruff check --fix .

security:
	uv run bandit -c pyproject.toml -r apps ingestion mma_analytics
	uv run pip-audit

# Cria o banco de teste no Postgres compartilhado, se ainda não existir.
# Idempotente: o `|| true` absorve o erro de banco já existente.
test-db:
	@PGPASSWORD=$${DB_PASSWORD:-devpass} createdb \
		-h $${DB_HOST:-postgres} -U $${DB_USER:-devuser} ufc_bum_test 2>/dev/null || true
