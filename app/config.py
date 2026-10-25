"""Application configuration.

Everything that differs between a laptop demo and a Postgres deployment is
resolved here so the rest of the code never branches on environment.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Customer Support Resolution Agent"
    environment: str = "local"

    # sqlite keeps the project runnable with zero setup; docker-compose supplies
    # a Postgres URL for the deployment the schema was actually designed for.
    database_url: str = f"sqlite+pysqlite:///{(REPO_ROOT / 'support.db').as_posix()}"
    checkpoint_database_url: str | None = None
    sql_echo: bool = False

    # --- model configuration -------------------------------------------------
    anthropic_api_key: str | None = None
    model_name: str = "claude-sonnet-5-5"
    model_max_tokens: int = 1500
    model_temperature: float = 0.0
    # When no API key is present the agents fall back to a deterministic
    # reasoner so the whole workflow, dashboard and eval suite still run.
    force_offline_reasoner: bool = False
    # Used only to report an estimated cost per run. Set these to whatever the
    # rate card says for the model you configure; they do not affect behaviour.
    price_input_usd_per_mtok: float = 3.0
    price_output_usd_per_mtok: float = 15.0

    # --- retrieval -----------------------------------------------------------
    embedding_dim: int = 512
    retrieval_top_k: int = 4
    retrieval_min_score: float = 0.08

    # --- money / policy ------------------------------------------------------
    currency: str = "INR"
    return_window_days: int = 30
    auto_approval_enabled: bool = False  # every refund needs a human reviewer
    approval_ttl_minutes: int = 60

    # --- reliability ---------------------------------------------------------
    provider_timeout_seconds: float = 5.0
    max_read_retries: int = 3
    read_retry_base_delay_seconds: float = 0.02
    max_workflow_steps: int = 40
    max_model_calls_per_run: int = 12

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgres")

    @property
    def effective_checkpoint_url(self) -> str:
        return self.checkpoint_database_url or self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
