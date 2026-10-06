"""Control settings. Domain workers must not load .env or DB settings."""

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
    env: Literal["dev", "test", "prod"] = "prod"
    database_url: str = (
        "postgresql+psycopg://copenhagen_app:copenhagen_app@localhost:5432/copenhagen"
    )
    database_owner_url: str | None = None
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    tenant_id: str = "default"
    copenhagen_dev_login: bool = False
    policy_directory: str | None = None
    session_hours: int = Field(default=8, ge=1, le=24)
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_metadata_url: str = "https://accounts.google.com/.well-known/openid-configuration"
    oidc_allowed_domain: str | None = None
    public_url: str = "http://localhost:8000"
    hook_secret: str | None = None
    callback_secret: str | None = None
    run_timeout_hours: int = Field(default=72, ge=1, le=720)
    notify_webhook_url: str | None = None
    notify_webhook_secret: str | None = None
    reconcile_minutes: int = Field(default=5, ge=1, le=1440)

    @model_validator(mode="after")
    def production_guards(self) -> Self:
        if self.env == "prod":
            if self.copenhagen_dev_login:
                raise ValueError(
                    "development identity/backend overrides are forbidden in production"
                )
            if not self.public_url.startswith("https://"):
                raise ValueError("production public_url must use HTTPS")
            if (
                not self.oidc_client_id
                or not self.oidc_client_secret
                or not self.oidc_allowed_domain
            ):
                raise ValueError("production requires OIDC credentials and allowed domain")
            if not self.database_url.startswith("postgresql"):
                raise ValueError("production requires PostgreSQL")
            if self.notify_webhook_url and not self.notify_webhook_url.startswith("https://"):
                raise ValueError("production notification webhook must use HTTPS")
            if "copenhagen_owner" in self.database_url:
                raise ValueError("runtime must use the restricted app role")
        return self

    @property
    def db_url(self) -> str:
        return self.database_url.replace("postgresql+asyncpg:", "postgresql+psycopg:")
