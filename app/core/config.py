from typing import Optional
from urllib.parse import quote_plus
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_REGION: str = "ap-southeast-2"
    EMAIL_SENDER: Optional[str] = None  # Verified SES identity email
    ENV: Optional[str] = None

    # Database configuration (DATABASE_URL overrides host/user/password settings)
    DATABASE_URL: Optional[str] = None
    IRIS_DB_USER: str = "iris"
    IRIS_DB_HOST: str = "iris-db"
    IRIS_DB_PORT: int = 5432
    IRIS_DB_NAME: str = "iris"
    IRIS_DB_PASSWORD: Optional[str] = None

    # Auth and inbound gateway secrets
    IRIS_WEBHOOK_SECRET: Optional[str] = None

    # Discord notifications
    DISCORD_ALERTS_WEBHOOK_URL: Optional[str] = None
    DISCORD_DECISIONS_WEBHOOK_URL: Optional[str] = None
    DISCORD_USER_ID: Optional[str] = None
    DISCORD_DEFAULT_COOLDOWN_SECONDS: int = 1800

    # TypeSafe Jev Decision Triage & Email Router configuration
    TYPESAFE_API_KEY: Optional[str] = None
    TYPESAFE_ENDPOINT: str = "https://api.typesafe.ai/v1/systemone"
    MUNINN_API_BASE: Optional[str] = "http://muninn:4000/v1"
    MUNINN_API_KEY: Optional[str] = None
    IRIS_ALERT_TRIAGE_ENABLED: bool = False
    IRIS_ALERT_SCORE_THRESHOLD: int = 7
    IRIS_ALERT_CONFIDENCE_THRESHOLD: float = 0.80

    # Plutus internal overlay consumer dispatch
    PLUTUS_API_URL: str = "http://plutus-api:8000"
    PLUTUS_WEBHOOK_SECRET: Optional[str] = None
    IRIS_PLUTUS_DISPATCH_ENABLED: bool = False

    # Autonomous Kanban Swarm Remediation Pipeline
    KANBAN_GATEWAY_URL: Optional[str] = "http://huginn-gateway:8643"
    IRIS_KANBAN_AUTO_TRIAGE: bool = True
    IRIS_KANBAN_MIN_SCORE: int = 8

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir="/run/secrets",
    )

    @model_validator(mode="after")
    def _strip_secret_whitespace(self) -> "Settings":
        for field_name in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "EMAIL_SENDER",
            "DATABASE_URL",
            "IRIS_DB_PASSWORD",
            "IRIS_WEBHOOK_SECRET",
            "DISCORD_ALERTS_WEBHOOK_URL",
            "DISCORD_DECISIONS_WEBHOOK_URL",
            "TYPESAFE_API_KEY",
            "MUNINN_API_KEY",
            "PLUTUS_WEBHOOK_SECRET",
        ):
            val = getattr(self, field_name)
            if isinstance(val, str):
                cleaned = val.strip()
                setattr(self, field_name, cleaned or None)
        return self

    def resolved_database_url(self) -> str:
        """Resolve SQLAlchemy database URL from DATABASE_URL or IRIS_DB_* fields."""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        if not self.IRIS_DB_PASSWORD:
            raise ValueError(
                "Database configuration missing: set DATABASE_URL or IRIS_DB_PASSWORD."
            )
        user = quote_plus(self.IRIS_DB_USER)
        password = quote_plus(self.IRIS_DB_PASSWORD)
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.IRIS_DB_HOST}:{self.IRIS_DB_PORT}/{self.IRIS_DB_NAME}"
        )


settings = Settings()
