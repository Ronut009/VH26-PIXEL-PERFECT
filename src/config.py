from pydantic_settings import BaseSettings, SettingsConfigDict

# Substrings that mark a setting as secret. Matched on the field name so a
# newly added credential is redacted by default rather than by remembering to
# add it here.
_SENSITIVE_NAME_HINTS = ("TOKEN", "SECRET", "PASSWORD", "KEY", "CREDENTIAL")

# Names that carry a secret without saying so. A heartbeat URL is a capability:
# anyone holding it can fake the all-clear for the dead man's switch.
_SENSITIVE_NAMES = frozenset({"HEARTBEAT_URL"})


def _is_sensitive(field_name: str) -> bool:
    upper = field_name.upper()
    return upper in _SENSITIVE_NAMES or any(
        hint in upper for hint in _SENSITIVE_NAME_HINTS
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_PATH: str = "data/alerts.db"
    SLACK_BOT_TOKEN: str = ""
    SLACK_CHANNEL_ID: str = ""
    PAGERDUTY_INTEGRATION_KEY: str = ""
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "dev"
    OUTBOX_POLL_INTERVAL_MS: int = 500
    # Attempts a row may burn on its *own* faults before dead-lettering. A
    # channel outage no longer consumes this budget, so an outage of any
    # length can no longer silently destroy the backlog.
    OUTBOX_MAX_ATTEMPTS: int = 5

    # Delivery circuit breaker. Consecutive channel-level failures before a
    # channel is declared down, then the probe cadence used to notice it is
    # back, and how many real rows are trialled before the breaker fully closes.
    OUTBOX_BREAKER_FAILURE_THRESHOLD: int = 3
    OUTBOX_PROBE_BASE_SECONDS: float = 5.0
    OUTBOX_PROBE_MAX_SECONDS: float = 120.0
    OUTBOX_HALF_OPEN_ALLOWANCE: int = 3

    # How long a worker's claim on a row survives. Long enough that a slow
    # provider call cannot lose its lease mid-send, short enough that a worker
    # killed mid-dispatch releases its rows promptly.
    OUTBOX_LEASE_SECONDS: float = 60.0
    OUTBOX_BATCH_SIZE: int = 10

    # Adaptive batching bounds. QUIET_WINDOW_MAX_MS caps any single predicted
    # silence window; INCIDENT_MAX_BATCH_SPAN_MS caps how long one incident may
    # keep deferring its own delivery, measured from its first alert.
    QUIET_WINDOW_MAX_MS: int = 300_000
    INCIDENT_MAX_BATCH_SPAN_MS: int = 600_000

    # Email — the last hop of the failover chain, via any SMTP relay. Gmail
    # needs an app password (not the account password) with 2FA enabled.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_USE_SSL: bool = False
    SMTP_TIMEOUT_SECONDS: float = 15.0
    EMAIL_FROM: str = ""
    EMAIL_TO: str = ""          # comma-separated

    # Correlation bounds. The co-occurrence graph ran unbounded on every alert:
    # every active incident in the scope became a neighbour, and root cause was
    # re-ranked over every edge, inside the write transaction. Both grew with
    # the square of the active set, so the graph was slowest during a storm.
    # A recent window plus a neighbour cap makes the per-alert cost constant.
    CORRELATION_WINDOW_MS: int = 900_000        # 15 min
    CORRELATION_MAX_NEIGHBOURS: int = 25

    # Root cause is an enrichment, not a transactional invariant, so it is
    # computed off the write path. The interval is also the debounce: a storm
    # of alerts against one scope earns one ranking pass per tick rather than
    # one per alert.
    ROOT_CAUSE_SWEEP_INTERVAL_SECONDS: float = 2.0

    # A source clock this far from ours is an operational problem worth naming:
    # it distorts every elapsed-time judgement made about that source's alerts.
    # Warnings are throttled per source so a drifted clock reports itself
    # without burying the log.
    # Dead man's switch. The heartbeat is sent to an EXTERNAL watchdog
    # (PagerDuty heartbeat, healthchecks.io) that pages when pings stop
    # arriving. Unset means nothing anywhere notices if this process dies.
    # It is gated on delivery actually working, not on the process being
    # alive, so a stalled outbox goes silent instead of reporting all-clear.
    HEARTBEAT_URL: str = ""
    HEARTBEAT_INTERVAL_SECONDS: float = 60.0
    HEARTBEAT_TIMEOUT_SECONDS: float = 10.0

    # Self-check thresholds. Undelivered-age is generous because an open
    # breaker legitimately parks rows while a provider is down; this is meant
    # to catch "nothing is draining at all".
    SELFCHECK_STUCK_OUTBOX_SECONDS: float = 900.0
    SELFCHECK_DEAD_LETTER_LIMIT: int = 10
    SELFCHECK_QUIET_INGEST_SECONDS: float = 3600.0

    CLOCK_SKEW_WARN_MS: int = 120_000
    CLOCK_SKEW_WARN_INTERVAL_SECONDS: float = 300.0

    # Alert ingestion is a trust boundary: it is the endpoint that creates
    # incidents, and a forged `resolved` alert closes a real one. Enabled by
    # default, and with no tokens configured it refuses ingest rather than
    # falling open - an unauthenticated alerting system is worse than one that
    # is loudly misconfigured.
    INGEST_AUTH_ENABLED: bool = True
    # Comma-separated `name:token[:scope]`. Scope is a prefix over
    # `environment/cluster`; "*" means any. Bind staging tokens to staging so a
    # leaked one cannot silence production.
    INGEST_TOKENS: str = ""

    # Inbound callbacks. These endpoints are public, so an unset secret means
    # the corresponding route rejects everything rather than trusting anyone.
    SLACK_SIGNING_SECRET: str = ""
    PAGERDUTY_WEBHOOK_SECRET: str = ""

    # Presumed-resolution from absence of alerts. The threshold is a multiple
    # of each incident's own EWMA gap, clamped between the floor and ceiling,
    # so chatty and quiet services are judged on their own rhythm. Criticals
    # get a much larger multiplier: wrongly closing a payment outage is far
    # more costly than leaving it open a while longer.
    SILENCE_RESOLVE_ENABLED: bool = True
    SILENCE_RESOLVE_MULTIPLIER: float = 6.0
    SILENCE_RESOLVE_CRITICAL_MULTIPLIER: float = 20.0
    SILENCE_RESOLVE_MIN_MS: int = 900_000       # 15 min floor
    SILENCE_RESOLVE_MAX_MS: int = 21_600_000    # 6 h ceiling
    SILENCE_SWEEP_INTERVAL_SECONDS: float = 30.0

    # Flap damping. Closing a quiet incident and reopening it on the next alert
    # are both correct; composed, they turn a badly thresholded alert into a
    # stream of card updates from the system built to stop exactly that.
    # Damping collapses the repeats without hiding the first transitions, which
    # are genuinely new information.
    FLAP_DAMPING_ENABLED: bool = True
    # Reopens before an incident is treated as flapping rather than as one that
    # legitimately came back. Two is a coincidence; this is the pattern.
    FLAP_REOPEN_THRESHOLD: int = 3
    # While flapping, at most one card update per this interval.
    FLAP_DIGEST_INTERVAL_SECONDS: float = 1_800.0
    # Each reopen multiplies the silence threshold, so closing gets harder every
    # time rather than staying equally easy and guaranteeing the next reopen.
    FLAP_HYSTERESIS_FACTOR: float = 1.5
    FLAP_HYSTERESIS_MAX_REOPENS: int = 6

    # GitHub Phase 1 uses a GitHub App with Metadata: read and Contents: read
    # only. The private key and webhook secret belong in a secret manager in
    # production; escaped newlines (\\n) are supported for local .env files.
    # The JWT issuer. GitHub documents both the numeric App ID and the Client
    # ID, but only the App ID is accepted everywhere - a Client ID issuer can
    # come back as "A JSON web token could not be decoded", which reads like a
    # broken key rather than an unrecognised issuer. Set GITHUB_APP_ID (the
    # number on the App's General tab) and it is used in preference.
    GITHUB_APP_ID: str = ""
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

    # Hosted diagnosis, for deployments with no local GPU. Deliberately gated on
    # its own flag rather than on the presence of an API key: sending repository
    # source to a third party is a decision an operator makes explicitly, never
    # a side effect of an environment variable being set somewhere.
    ANTHROPIC_DIAGNOSIS_ENABLED: bool = False
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-opus-5"
    ANTHROPIC_MAX_OUTPUT_TOKENS: int = 16_000
    ANTHROPIC_TIMEOUT_SECONDS: float = 60.0

    def __repr_args__(self):
        """Redact credentials from every repr of this object.

        Pydantic renders the whole settings object in a repr, and a repr is
        exactly what ends up in a pytest traceback, an unhandled-exception log
        line, or a debugger frame. A single unrelated test failure was enough
        to print the live Slack bot token, PagerDuty routing key and GitHub
        admin token in full - which then travels wherever that output goes: CI
        logs, a pasted stack trace, a screenshot in a chat.

        Values stay usable; only their presentation changes.
        """

        for name, value in super().__repr_args__():
            if name and value and _is_sensitive(name):
                yield name, "***redacted***"
            else:
                yield name, value

    @property
    def github_app_issuer(self) -> str:
        """What to put in the App JWT's ``iss`` claim."""

        return self.GITHUB_APP_ID or self.GITHUB_APP_CLIENT_ID

    @property
    def github_app_is_configured(self) -> bool:
        return bool(self.github_app_issuer and self.GITHUB_APP_PRIVATE_KEY)


settings = Settings()
