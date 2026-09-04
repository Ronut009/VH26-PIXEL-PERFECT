# GitHub Phase 1: read-only setup

PulseGraph Phase 1 connects a monitored service to a selected GitHub
repository and records immutable source inventories. It cannot write to GitHub:
it has no branch, commit, pull-request, issue, comment, merge, or Git-over-HTTP
capability.

## Create the GitHub App

An organization owner must create and install the app. This cannot be completed
from PulseGraph code because GitHub requires the organization's approval.

1. Create a GitHub App and set its webhook URL to
   `https://<your-pulsegraph-host>/v1/github/webhooks`.
2. Generate a high-entropy webhook secret and save it as
   `GITHUB_WEBHOOK_SECRET` in the deployment secret manager.
3. Grant only these repository permissions:
   - **Metadata: Read-only**
   - **Contents: Read-only**
4. Do not grant any other repository, organization, or account permission.
5. Configure installation so an administrator must choose **Only select
   repositories**. Do not permit all repositories for this MVP.
6. Generate a private key. Store it only in the deployment secret manager.
7. Copy the GitHub App's **Client ID** into `GITHUB_APP_CLIENT_ID`.

GitHub delivers installation lifecycle events to the configured webhook; Phase
1 accepts only `installation` and `installation_repositories` events. It does
not subscribe to source-change events such as `push`.

## Configure PulseGraph

Set the following deployment secrets and runtime configuration. Do not commit
real values to `.env` or source control.

```text
GITHUB_APP_CLIENT_ID=Iv...             # GitHub App Client ID
GITHUB_APP_PRIVATE_KEY=-----BEGIN...   # PEM; use \n for line breaks in a .env file
GITHUB_WEBHOOK_SECRET=<random-secret>
GITHUB_APP_SLUG=<github-app-slug>
GITHUB_ADMIN_TOKEN=<separate-random-admin-token>
GITHUB_WEBHOOK_MAX_BYTES=1048576
GITHUB_WEBHOOK_MAX_REPOSITORIES=1000
GITHUB_MAX_TREE_ENTRIES=10000
GITHUB_MAX_TREE_REQUESTS=2000
GITHUB_MAX_TREE_DEPTH=64
```

`GITHUB_ADMIN_TOKEN` protects the temporary repository-management endpoints
until dashboard account authentication exists. Use HTTPS and place these
endpoints behind the product's authenticated gateway in production.

## Connect and snapshot a repository

With `Authorization: Bearer $GITHUB_ADMIN_TOKEN`:

1. Open `GET /v1/github/install-url` and install the app on the chosen
   repositories. The verified lifecycle webhook records the installation.
2. Call `POST /v1/github/installations/{installation_id}/sync` to refresh the
   selected repository inventory from GitHub.
3. Call `PUT /v1/github/service-mappings/{service}` with
   `{ "repository_id": 123 }`.
4. Call `POST /v1/github/repositories/123/snapshots`.

The snapshot records the default branch, immutable commit SHA, root tree SHA,
and blob metadata. It deliberately does not persist source content or access
tokens. GitHub's installation token is created in memory for the request, is
restricted to read-only contents access, and expires after one hour.

## Safety checks

- Every webhook uses raw-body HMAC-SHA256 validation via
  `X-Hub-Signature-256`; legacy SHA-1 signatures are rejected.
- `X-GitHub-Delivery` IDs are deduplicated to make redelivery safe.
- Installations using all repositories or any permission beyond read-only
  Metadata/Contents are stored as misconfigured and cannot be mapped or
  snapshotted.
- The current selection mode is rechecked both from lifecycle webhooks and
  every installation-token response; a delayed or broadened installation is
  refused before repository metadata can be persisted.
- Revocation and repository-selection state is rechecked inside the final
  database write transaction, so stale GitHub reads cannot re-enable a
  removed, suspended, or deleted connection.
- GitHub API access is limited to metadata, branch, tree, blob, and required
  installation-token endpoints. No generic mutation operation exists.
- Large recursive trees fall back to a bounded tree walk; source inventories
  above `GITHUB_MAX_TREE_ENTRIES`, `GITHUB_MAX_TREE_REQUESTS`, or
  `GITHUB_MAX_TREE_DEPTH` are refused rather than silently truncated.
- Webhook bodies and lifecycle repository arrays are capped before persistence;
  this protects the alert-ingestion writer from optional integration traffic.
