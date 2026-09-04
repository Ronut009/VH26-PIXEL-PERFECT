# GitHub incident-to-patch MVP

PulseGraph's GitHub integration turns a correlated alert storm into a
reviewable engineering handoff. It is deliberately **read-only**: PulseGraph
may inspect a selected repository at a pinned commit, but it never changes
GitHub or an engineer's checkout.

## Non-negotiable safety boundary

- The GitHub App receives only repository **Metadata: read** and **Contents:
  read** permissions, and is installed on **only selected repositories**.
- GitHub access is limited to installation-token creation and read operations
  for repository metadata, branches, commits, trees, and blobs.
- PulseGraph has no GitHub write endpoint or permission. It cannot push,
  create commits, branches, pull requests, issues, or comments; merge code;
  change repository settings; or run `git clone`, `git pull`, or `git push`.
- App private keys, webhook secrets, and short-lived installation tokens stay
  out of incident records, snapshots, logs, and generated patches.
- A proposed fix always requires human review. A PulseGraph patch preview is
  not a commit, does not alter the connected repository, and does not alter
  the engineer's local checkout.

## The four phases

### Phase 1 — Connect a repository safely

An administrator installs the read-only GitHub App on selected repositories.
PulseGraph verifies signed installation lifecycle webhooks, records the
selected repository inventory, and maps one monitored service to one selected
repository for the MVP. It then records an immutable source inventory pinned
to the default branch's exact commit and tree SHA.

The commit SHA is part of the investigation evidence: a later branch update
does not silently change the code that an engineer is asked to review. Phase 1
stores repository and Git-object metadata, not installation tokens or a full
persisted copy of source content.

### Phase 2 — Collapse the alert storm into an investigation

PulseGraph retains the original alerts for auditability but surfaces one
incident/investigation when alerts share a service, scope, time window, and
stable identity. The handoff states the alert count, affected environment and
cluster, timeline, related signals, and the exact repository snapshot chosen
for analysis.

This phase is correlation, not a claim that one alert is certainly the root
cause. A diagnosis must distinguish evidence from hypothesis.

### Phase 3 — Produce a bounded, grounded diagnosis

Only relevant, text-like files from the pinned snapshot are considered. The
source-context policy limits file count, individual-file bytes, total bytes,
path length, and Git tree traversal. Binary files, unsafe paths, oversized
files, detectable credential-like paths, and unneeded generated assets are
excluded.

A diagnosis provider receives the incident evidence plus those bounded source
excerpts—not an unbounded repository dump. A valid result must include:

- a root-cause hypothesis rather than an unsupported certainty;
- citations to the supplied snapshot paths, blob IDs, and line ranges;
- a confidence value and a plain-language explanation; and
- a proposed fix limited to paths that were actually supplied as evidence.

If source context is missing, the provider is unavailable, or a result is
ungrounded, PulseGraph returns a transparent fallback instead of inventing a
diagnosis.

### Phase 4 — Generate a local patch preview for review

PulseGraph may apply a structured proposed patch only inside an ephemeral,
local workspace created from the pinned source inputs. The workspace validates
paths, file counts, byte budgets, expected source hashes, and conflicts before
producing a unified diff and a change summary.

The current MVP model route permits only updates or deletions to source files
already supplied as grounded evidence. It intentionally does not create new
files yet, so an unreviewed model cannot introduce a surprise test, script, or
configuration path.

It does not execute the patch, run untrusted code, access the network, create
a Git repository, or persist source contents after the workspace is removed.
The engineer reviews the diff, applies it in their own checkout, runs their
own tests, and independently creates any commit or pull request.

## End-to-end evidence flow

```text
many monitoring alerts
  -> one correlated incident
  -> service-to-repository mapping
  -> immutable GitHub commit/tree snapshot
  -> bounded source excerpts + incident evidence
  -> grounded diagnosis or safe fallback
  -> local unified-diff preview
  -> engineer reviews, tests, commits, and opens a PR
```

The final step is intentionally outside PulseGraph. The engineer remains the
authority to approve, commit, push, and merge code.

## Required configuration

These values belong in a deployment secret manager or a local uncommitted
`.env` file:

| Variable | Why it is needed |
| --- | --- |
| `GITHUB_APP_CLIENT_ID` | Identifies the read-only GitHub App when creating an App JWT. |
| `GITHUB_APP_PRIVATE_KEY` | Signs short-lived App JWTs; never store it in SQLite or source control. |
| `GITHUB_WEBHOOK_SECRET` | Verifies `X-Hub-Signature-256` on GitHub lifecycle deliveries. |
| `GITHUB_APP_SLUG` | Builds the administrator's GitHub App installation URL. |
| `GITHUB_ADMIN_TOKEN` | Temporary protection for connection and snapshot management APIs until dashboard account authentication exists. |
| `GITHUB_DIAGNOSIS_MAX_*` | Bounded text-file context sent for a diagnosis (defaults: 6 files, 8 KiB/file, 48 KiB total). |
| `GITHUB_PATCH_MAX_*` | Bounded evidence-file and local-diff limits for a patch preview. |
| `OLLAMA_ENABLED` | Enables the optional local model only when explicitly set to `true`. |
| `OLLAMA_MODEL` | Local Ollama model name, for example `qwen2.5-coder:7b`. |
| `OLLAMA_BASE_URL` | Must remain a loopback URL; defaults to `http://127.0.0.1:11434`. |
| `OLLAMA_TIMEOUT_SECONDS` / `OLLAMA_MAX_OUTPUT_TOKENS` | Bound one local structured model request. |

Operational bounds such as `GITHUB_WEBHOOK_MAX_BYTES`,
`GITHUB_WEBHOOK_MAX_REPOSITORIES`, `GITHUB_MAX_TREE_ENTRIES`,
`GITHUB_MAX_TREE_REQUESTS`, and `GITHUB_MAX_TREE_DEPTH` should stay finite.
They prevent optional source-inventory work from crowding out alert ingestion.

For the concrete GitHub App registration and API sequence, see
[GitHub Phase 1: read-only setup](github-phase1-setup.md).

With the temporary `Authorization: Bearer $GITHUB_ADMIN_TOKEN` guard, the
backend workflow is:

1. `POST /v1/github/incidents/{incident_id}/diagnoses` creates a sanitized,
   cited diagnosis record from the newest active mapped snapshot. Without a
   healthy local provider it creates a transparent safe fallback instead.
2. `GET /v1/github/incidents/{incident_id}/diagnoses` and
   `GET /v1/github/analyses/{analysis_id}` expose only the sanitized record;
   source excerpts are never returned or persisted.
3. `POST /v1/github/analyses/{analysis_id}/patch-preview` asks the enabled
   loopback Ollama provider for a constrained proposal, applies it only in a
   disposable local workspace, and returns a unified diff. It does not write
   a GitHub branch, commit, pull request, or the engineer's checkout.

## What deployment still requires

Code alone cannot create a live organization connection. A GitHub organization
owner must register the App, configure the public HTTPS webhook URL, keep the
private key and webhook secret in the deployment secret manager, and choose
the repositories during installation. Until that happens, PulseGraph should
report the GitHub integration as unconfigured rather than imply a live
connection.
