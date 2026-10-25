from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_REGION: str = "ap-southeast-2"
    EMAIL_SENDER: str  # Verified SES identity email
    ENV: Optional[str] = None
    DISCORD_ALERTS_WEBHOOK_URL: Optional[str] = None
    DISCORD_DECISIONS_WEBHOOK_URL: Optional[str] = None
    DISCORD_USER_ID: Optional[str] = None
    DISCORD_DEFAULT_COOLDOWN_SECONDS: int = 1800

    # TypeSafe Jev Decision Triage Gate configuration
    TYPESAFE_API_KEY: Optional[str] = None
    TYPESAFE_ENDPOINT: str = "https://api.typesafe.ai/v1/systemone"
    MUNINN_API_BASE: Optional[str] = "http://muninn:4000/v1"
    IRIS_ALERT_TRIAGE_ENABLED: bool = False
    IRIS_ALERT_SCORE_THRESHOLD: int = 7
    IRIS_ALERT_CONFIDENCE_THRESHOLD: float = 0.80

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir="/run/secrets"
    )

settings = Settings()
