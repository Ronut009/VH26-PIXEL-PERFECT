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

    # GitHub Phase 1 uses a GitHub App with Metadata: read and Contents: read
    # only. The private key and webhook secret belong in a secret manager in
    # production; escaped newlines (\\n) are supported for local .env files.
    GITHUB_APP_CLIENT_ID: str = ""
    GITHUB_APP_PRIVATE_KEY: str = ""
    GITHUB_WEBHOOK_SECRET: str = ""
    GITHUB_APP_SLUG: str = ""
    GITHUB_ADMIN_TOKEN: str = ""
    GITHUB_API_VERSION: str = "2022-11-28"
    GITHUB_REQUEST_TIMEOUT_SECONDS: float = 10.0
    # Tight MVP budgets keep optional source inventory work from delaying the
    # alert-ingestion writer. Operators can raise these only after capacity
    # testing their deployment.
    GITHUB_WEBHOOK_MAX_BYTES: int = 1_048_576
    GITHUB_WEBHOOK_MAX_REPOSITORIES: int = 1_000
    GITHUB_MAX_TREE_ENTRIES: int = 10_000
    GITHUB_MAX_TREE_REQUESTS: int = 2_000
    GITHUB_MAX_TREE_DEPTH: int = 64

    # Phase 2/3 source context stays deliberately smaller than the repository
    # inventory. It is read only on demand and never persisted to SQLite.
    GITHUB_DIAGNOSIS_MAX_FILES: int = 6
    GITHUB_DIAGNOSIS_MAX_FILE_BYTES: int = 8 * 1024
    GITHUB_DIAGNOSIS_MAX_TOTAL_BYTES: int = 48 * 1024

    # Phase 4 fetches only diagnosis-approved source paths into an ephemeral
    # local workspace. These budgets apply before a model can generate a diff.
    GITHUB_PATCH_MAX_FILES: int = 8
    GITHUB_PATCH_MAX_FILE_BYTES: int = 32 * 1024
    GITHUB_PATCH_MAX_TOTAL_BYTES: int = 96 * 1024
    GITHUB_PATCH_MAX_CHANGES: int = 8
    GITHUB_PATCH_MAX_BYTES: int = 96 * 1024
    GITHUB_PATCH_MAX_DIFF_BYTES: int = 192 * 1024

    # The optional open-model provider is deliberately local-only. It remains
    # disabled by default so an unavailable Ollama process never affects alert
    # ingestion or turns a configured GitHub connection into remote model I/O.
    OLLAMA_ENABLED: bool = False
    OLLAMA_MODEL: str = ""
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    OLLAMA_TIMEOUT_SECONDS: float = 30.0
    OLLAMA_MAX_OUTPUT_TOKENS: int = 2_048

    @property
    def github_app_is_configured(self) -> bool:
        return bool(self.GITHUB_APP_CLIENT_ID and self.GITHUB_APP_PRIVATE_KEY)


settings = Settings()
