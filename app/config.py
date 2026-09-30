"""Runtime configuration, read from environment variables (or a local .env file)."""
from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database -------------------------------------------------------
    # Either give a full DATABASE_URL (local / docker-compose) or the parts
    # (on AWS, ECS injects DB_USER / DB_PASSWORD from Secrets Manager).
    database_url: str | None = None
    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "aml"
    db_user: str | None = None
    db_password: str | None = None
    db_sslmode: str = "prefer"

    # --- Models ---------------------------------------------------------
    # "bedrock" uses Amazon Bedrock; "mock" is a deterministic offline analyst
    # used for local development and tests (no AWS credentials needed).
    llm_provider: str = "bedrock"
    embedding_provider: str = "bedrock"  # "bedrock" | "local"
    aws_region: str = "us-east-1"
    # Check the Bedrock console for the model / inference-profile IDs enabled
    # in your account and region, and override via BEDROCK_MODEL_ID.
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    bedrock_embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    embedding_dim: int = 1024

    # --- Triage policy --------------------------------------------------
    history_days: int = 90
    # Rule score at or above which the case is escalated even if the LLM
    # recommends closing (a deterministic guardrail over the model).
    force_escalate_score: int = 70
    # Closures with a final score at or below this skip human review.
    # -1 (default) means every disposition goes to an analyst (four-eyes).
    auto_close_max_score: int = -1
    structuring_threshold: float = 10_000.0
    high_risk_countries: str = "KP,IR,MM"  # comma-separated ISO-2; keep in sync with your policy

    # --- API ------------------------------------------------------------
    api_key: str | None = None  # when set, requests need header X-API-Key

    @property
    def dsn(self) -> str:
        if self.database_url:
            return self.database_url
        if not (self.db_host and self.db_user and self.db_password):
            raise RuntimeError("Set DATABASE_URL, or DB_HOST + DB_USER + DB_PASSWORD")
        return (
            f"postgresql://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?sslmode={self.db_sslmode}"
        )

    @property
    def high_risk_country_set(self) -> set[str]:
        return {c.strip().upper() for c in self.high_risk_countries.split(",") if c.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
