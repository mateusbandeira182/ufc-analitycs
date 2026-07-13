"""Configuração da aplicação lida do ambiente (pydantic-settings v2).

Monta a URL síncrona do SQLAlchemy a partir das variáveis ``DB_*`` e resolve o
banco efetivo por ambiente: em ``APP_ENV=test`` aponta para ``ufc_bum_test``,
isolando a suíte do banco de desenvolvimento.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

TEST_DB_NAME = "ufc_bum_test"


class Settings(BaseSettings):
    """Configuração efetiva da aplicação, populada por ambiente."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "ufc_databum"
    db_user: str = "devuser"
    # Default de desenvolvimento (compose compartilhado); o segredo real vem do .env.
    db_password: str = "devpass"  # noqa: S105

    # Cito API (ingestão incremental, M1). O token vem do .env; vazio é aceitável no
    # modo fixture, que é o caminho de teste e não consome a quota do free tier.
    cito_api_token: str = ""
    cito_base_url: str = "https://api.citoapi.com"

    @property
    def effective_db_name(self) -> str:
        """Nome do banco efetivo: ``ufc_bum_test`` em teste, ``db_name`` fora dele."""
        return TEST_DB_NAME if self.app_env == "test" else self.db_name

    @property
    def database_url(self) -> str:
        """URL síncrona (psycopg) do SQLAlchemy para o banco efetivo."""
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.effective_db_name}"
        )


settings = Settings()
