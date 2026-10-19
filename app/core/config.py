from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_REGION: str = "ap-southeast-2"
    EMAIL_SENDER: str  # Verified SES identity email
    ENV: Optional[str] = None
    DISCORD_ALERTS_WEBHOOK_URL: Optional[str] = None
    DISCORD_USER_ID: Optional[str] = None
    DISCORD_DEFAULT_COOLDOWN_SECONDS: int = 1800

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir="/run/secrets"
    )

settings = Settings()
