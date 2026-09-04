from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_PATH: str = "data/alerts.db"
    SLACK_BOT_TOKEN: str = ""
    SLACK_CHANNEL_ID: str = ""
    PAGERDUTY_INTEGRATION_KEY: str = ""
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "dev"
    OUTBOX_POLL_INTERVAL_MS: int = 500
    OUTBOX_MAX_ATTEMPTS: int = 5


settings = Settings()
