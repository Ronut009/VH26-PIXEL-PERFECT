"""Credentials must not travel in a traceback.

Pydantic renders the whole settings object in a repr, and a repr is exactly
what ends up in a pytest traceback, an unhandled-exception log line, or a
debugger frame. One unrelated test failure was enough to print the live Slack
bot token, PagerDuty routing key and GitHub admin token in full - and from
there they travel wherever that output goes: CI logs, a pasted stack trace, a
screenshot in a chat.
"""

from src.config import Settings, _is_sensitive


def _settings() -> Settings:
    return Settings(
        SLACK_BOT_TOKEN="xoxb-real-looking-token",
        PAGERDUTY_INTEGRATION_KEY="pd-routing-key",
        GITHUB_ADMIN_TOKEN="gh-admin-token",
        GITHUB_APP_PRIVATE_KEY="fake-pem-body-not-a-real-key",
        GITHUB_WEBHOOK_SECRET="gh-webhook-secret",
        SLACK_SIGNING_SECRET="slack-signing-secret",
        PAGERDUTY_WEBHOOK_SECRET="pd-webhook-secret",
        SMTP_PASSWORD="smtp-app-password",
        ANTHROPIC_API_KEY="sk-ant-not-a-real-key",
        INGEST_TOKENS="src:ingest-token:*",
        HEARTBEAT_URL="https://hc-ping.com/secret-uuid",
        OLLAMA_MODEL="qwen2.5-coder:7b",
    )


def test_no_secret_survives_a_repr():
    rendered = repr(_settings())

    for secret in (
        "xoxb-real-looking-token",
        "pd-routing-key",
        "gh-admin-token",
        "fake-pem-body-not-a-real-key",
        "gh-webhook-secret",
        "slack-signing-secret",
        "pd-webhook-secret",
        "smtp-app-password",
        "sk-ant-not-a-real-key",
        "ingest-token",
    ):
        assert secret not in rendered, f"{secret!r} leaked into the settings repr"


def test_a_heartbeat_url_is_treated_as_a_credential():
    """Holding it means being able to fake the dead man's switch all-clear."""

    assert "secret-uuid" not in repr(_settings())


def test_non_secrets_stay_readable():
    """Redaction has to leave the object useful for debugging."""

    rendered = repr(_settings())

    assert "qwen2.5-coder:7b" in rendered
    assert "OLLAMA_MODEL=" in rendered


def test_values_themselves_are_untouched():
    """Only the presentation changes; the app still needs to authenticate."""

    settings = _settings()

    assert settings.SLACK_BOT_TOKEN == "xoxb-real-looking-token"
    assert settings.INGEST_TOKENS == "src:ingest-token:*"


def test_an_unset_secret_is_visibly_unset():
    """Seeing that a credential is missing is the point of reading a repr."""

    rendered = repr(Settings(SLACK_BOT_TOKEN=""))

    assert "SLACK_BOT_TOKEN=''" in rendered


def test_a_newly_added_credential_is_redacted_by_default():
    """Matching on the field name means the next secret is covered without
    anyone remembering to add it to a list."""

    assert _is_sensitive("SOME_NEW_API_KEY")
    assert _is_sensitive("VENDOR_WEBHOOK_SECRET")
    assert _is_sensitive("THING_PASSWORD")
    assert _is_sensitive("SERVICE_TOKEN")
    assert not _is_sensitive("OLLAMA_MODEL")
    assert not _is_sensitive("CORRELATION_WINDOW_MS")
