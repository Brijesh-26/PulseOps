"""Environment-based configuration.

All settings are read from environment variables prefixed with ``PULSEOPS_``
(or a local ``.env`` file). Secrets use ``SecretStr`` so they are never
rendered in logs or reprs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PULSEOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["local", "test", "prod"] = "local"
    log_level: str = "INFO"
    log_json: bool = True

    # Database
    database_url: str = "postgresql+psycopg://pulseops_app:pulseops_app@localhost:5433/pulseops"
    # Owner role used only for migrations; falls back to database_url when unset.
    migration_database_url: str | None = None

    # HTTP
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    max_body_bytes: int = 64 * 1024
    rate_limit_per_minute: int = 600
    auth_rate_limit_per_minute: int = 30

    # Auth
    session_secret: SecretStr = SecretStr("")
    session_ttl_seconds: int = 3600

    # Queue
    queue_backend: Literal["memory", "sqs"] = "sqs"
    sqs_endpoint_url: str | None = None
    aws_region: str = "me-central-1"
    sqs_queue_name: str = "pulseops-work"
    sqs_dlq_name: str = "pulseops-work-dlq"
    sqs_visibility_timeout_seconds: int = 60
    max_receive_count: int = 5
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 300.0

    # Outbox relay
    outbox_batch_size: int = 50
    outbox_poll_interval_seconds: float = 1.0

    # Investigator
    investigator_provider: Literal["mock", "anthropic"] = "mock"
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Server-side refusal fallback ("fallbacks": "default"); Claude API only.
    anthropic_refusal_fallback: bool = True
    investigator_timeout_seconds: float = 120.0
    investigator_max_attempts: int = 3
    investigator_max_tool_calls: int = 6
    investigator_max_evidence_chars: int = 24_000
    action_min_confidence: float = 0.6

    # Remediation
    proposal_ttl_seconds: int = 3600

    # Demo
    demo_mode: bool = False

    @model_validator(mode="after")
    def _validate_prod(self) -> Settings:
        if self.environment == "prod":
            if len(self.session_secret.get_secret_value()) < 32:
                raise ValueError("PULSEOPS_SESSION_SECRET must be at least 32 chars in prod")
            if self.demo_mode:
                raise ValueError("PULSEOPS_DEMO_MODE must be false in prod")
            if "*" in self.cors_origins:
                raise ValueError("Wildcard CORS origin is not allowed in prod")
        return self

    def session_secret_bytes(self) -> bytes:
        secret = self.session_secret.get_secret_value()
        if not secret:
            if self.environment == "prod":
                raise RuntimeError("session secret not configured")
            # Deterministic dev-only secret so local sessions survive restarts.
            secret = "pulseops-local-dev-only-secret-not-for-prod"  # noqa: S105
        return secret.encode()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
