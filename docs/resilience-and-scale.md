# Delivery resilience, grouping, and scale

Answers to the questions raised in jury review, and what changed in the code
because of them. Each section states the mechanism, then the honest limit.

Run `python scripts/outage_drill.py` to watch the whole outage lifecycle
offline — no Slack workspace or PagerDuty key needed.

---

## 1. If Slack goes down, how does the system know?

**It does not infer it. It classifies the failure, then confirms it.**

The old worker could not tell "this message is malformed" from "slack.com is
unreachable". Both took the same path: increment `attempts`, back off, and mark
the row `dead` at `OUTBOX_MAX_ATTEMPTS`. With backoff `min(2**attempts, 300)`
and a budget of 5, **every queued message was permanently dead-lettered about
62 seconds into an outage.** An outage shorter than most real ones silently
destroyed the entire backlog. That was the single most serious flaw the jury's
question pointed at, and it is fixed.

Failures are now classified into three kinds ([failure_policy.py](../src/outbox/failure_policy.py)):

| Kind | Examples | Charged an attempt? | Trips breaker? |
|---|---|---|---|
| `MESSAGE_FATAL` | `invalid_blocks`, `channel_not_found`, 4xx | Dead-lettered at once | No |
| `CHANNEL_DOWN` | connection refused, DNS failure, 5xx, `token_revoked` | **No** | Yes |
| `TRANSIENT` | 429 with `Retry-After`, unknown errors | Yes | No |

The asymmetry is the whole point. A bad payload is *that row's* fault and can
never be retried into success. An outage is *the channel's* fault, and the row
did nothing wrong — so it must not be charged for it. A malformed message can
no longer convince the system that Slack is down, and an outage can no longer
consume the retry budget of messages that were never even sent.

Three consecutive `CHANNEL_DOWN` failures (`OUTBOX_BREAKER_FAILURE_THRESHOLD`)
flip a per-channel circuit breaker to `OPEN` in `channel_health`. The threshold
is above one so a single unlucky request never halts a healthy channel.

## 2. And how does it know when Slack came back?

**A scheduled, side-effect-free probe asks it directly.**

While a channel is `OPEN` the worker stops dispatching to it entirely — no row
burns an attempt against a dead endpoint. Instead it calls Slack's `auth.test`
on an exponential backoff (5s, 10s, 20s … capped at 120s). `auth.test` posts
nothing and pages nobody, so a dead channel can be polled cheaply for hours
with zero risk of spamming a real channel.

Recovery is deliberately **two-stage**:

```
OPEN  --probe succeeds-->  HALF_OPEN  --real delivery succeeds-->  CLOSED
  ^                             |
  +------ real delivery fails --+
```

A successful probe does not close the breaker; it only re-opens the door for a
small trial batch (`OUTBOX_HALF_OPEN_ALLOWANCE`, default 3). A provider that
answers `auth.test` but still rejects posts is caught by the trial instead of
being flooded with the entire backlog. Only a real successful delivery closes
the breaker and stamps `recovered_at` on the outage record.

Every outage is a row in `channel_outages` with its detection time, recovery
time, probe count, and how many alerts were failed over — so the blind window
is a fact on the record, not a guess.

## 3. Critical alerts pile up during the outage. What are the alternate routes?

**Severity decides. Criticals leave immediately; the rest wait.**

The moment an outage is declared, [routing.py](../src/outbox/routing.py) walks
the failover chain and re-routes every queued `critical` and `high` row onto
the first fallback whose own breaker is closed:

```
slack      ->  pagerduty  ->  email  ->  (dashboard, always available)
pagerduty  ->  slack      ->  email
```

PagerDuty is chosen first because it shares almost no failure domain with
Slack: different company, different infrastructure, different DNS. A Slack
outage tells you nothing about PagerDuty's health.

`medium` and `low` are deliberately **not** failed over. Paging a human at 3am
because Slack was briefly unreachable trades one kind of alert fatigue for a
worse one. Those rows simply wait.

Three details make the failover safe rather than noisy:

- **No double-paging.** PagerDuty's `dedup_key` is the incident id, so a
  failover trigger for an incident that critical-bypass already paged collapses
  into the existing PagerDuty incident instead of paging twice.
- **PagerDuty cannot edit a card**, so a queued `update` becomes a `trigger` on
  the way over; `dedup_key` keeps that idempotent too.
- **The Slack row is not deleted.** It stays queued and gets tagged
  `delivered_via_fallback`, so when Slack returns the card explicitly says
  *"Paged via PagerDuty while Slack was unreachable — this is the same
  incident, not a new one."* Without this, a responder sees a PagerDuty alert
  and a Slack card and reasonably assumes two separate problems.

There is also a route with no third party in it at all: the dashboard's SSE
stream (`GET /v1/stream`) is served by this system, so it keeps working when
every external provider is down. It is the honest floor of availability.

## 4. How does alert grouping actually work?

**Two independent layers.**

**Layer 1 — identity (collapses duplicates).** Each alert gets a
`stable_fingerprint`: `sha256(service | alertname | severity | stable labels)`.
Volatile labels — `pod`, `instance`, `container_id`, timestamps, UIDs — are
stripped before hashing ([dedupe.py](../src/engine/dedupe.py)), so the same
failure across forty restarting pods is one identity, not forty. The
fingerprint is scoped by `scope_key = environment/cluster`, which is what stops
an identical alert in staging from deduplicating a production incident. Match
an active incident, and the alert increments `alert_count` instead of creating
anything. This is the "500 alerts in 10 seconds, one message" number.

**Layer 2 — correlation (relates distinct incidents).** Concurrent incidents in
one scope form directed, time-decayed co-occurrence edges. `rank_root_cause`
ranks them so a DB → API → Pod cascade surfaces the database as the likely
cause, attached to the card as `root_cause_hint`.

**Layer 3 — storm grouping (collapses a cascade).** Layers 1 and 2 left a real
gap: correlation *annotated* incidents but never *merged* them, so a
three-service cascade still posted three cards that each separately named the
same root cause, and the responder had to work out they were one outage.
[storm_grouping.py](../src/graph/storm_grouping.py) turns strong edges into
membership. Above `GROUP_EDGE_THRESHOLD` (1.5 — deliberately above the 1.0 a
single coincidental co-occurrence produces, so grouping needs corroboration),
incidents union into an `incident_group`, and only one card is posted.

Four decisions carry the design:

- **Anchor and root are different ids, on purpose.** The *anchor* owns the
  Slack message for the group's whole life, because the card's `external_ref`
  belongs to it — if the anchor moved, the card would jump to a new message
  mid-incident. The *root* is the currently ranked cause and is free to change
  as evidence accumulates, because it is only rendered, never used as identity.
- **Incremental, never a global recompute.** Grouping walks only the current
  incident's strong neighbours and unions into whatever groups they already
  belong to, capped at `MAX_NEIGHBOURS`. Recomputing connected components per
  alert would compound the graph's existing quadratic cost (see
  [system-design-gaps §2](system-design-gaps.md)).
- **Two half-storms merge when evidence connects them.** Cascades are
  discovered incrementally, so a group can form from each end; when a later
  edge ties them together the *older* group survives, because its anchor
  already owns a card people are looking at.
- **Members stop competing with their own cause.** A member's queued intents
  are retargeted at the anchor, where per-poll coalescing collapses them into
  one edit. A member that already posted a card before it was correlated gets
  one final edit — *"merged into a correlated storm"* — so no orphaned card is
  left claiming to be a separate problem.

A storm is as urgent as its most urgent member, and closes only when every
member has closed.

One bug worth recording, because the test found it and review would not have:
group facts are derived from membership, but formation is driven by *new
alerts* while resolution is driven by their *absence*. Without an explicit
`refresh_group_for_member` hook on every member state change, a storm whose
members all quietly resolved kept an `OPEN` card forever.

## 5. When Slack comes back, how do the grouped alerts get delivered?

**Replaying the backlog in order would be its own incident.** A 30-minute
outage over a noisy service leaves hundreds of rows, most of them successive
`update` intents for a handful of incidents whose state has long moved on.
Posting them in sequence would create exactly the alert fatigue this system
exists to eliminate — caused by the system itself.

Three rules ([recovery.py](../src/outbox/recovery.py)), applied the moment the
probe reports the channel back:

**Coalesce.** For each `(incident_id, channel)`, only the newest pending intent
survives; the rest become `superseded` and are never sent. In the drill, 41
queued writes for one incident become **1 message**. Nothing is deleted —
superseded rows stay in the table as an auditable record of what was collapsed
and why.

**Render late.** A queued row stores the *intent* to notify, not message text.
Title, severity, `alert_count`, and root-cause hint are read from live incident
state at send time. A card delivered after a 30-minute outage therefore says
*"517 alerts"*, not the *"1 alert"* that was true when it was queued. An
incident that opened *and resolved* entirely inside the blind window is posted
as resolved rather than announced as newly firing.

**Explain the gap.** One digest is queued at priority `-1` so it lands ahead of
everything else: how long the channel was unreachable, how many incidents
changed, how many were critical, how many resolved on their own, how many
updates were collapsed, and how many were already paged out via fallback. The
channel history is never silently missing half an hour.

One more fix in this area: previously each outbox row carried its own
`external_ref`, so an `update` row had no Slack `ts` and silently fell back to
posting a *new* message — one incident quietly became N messages, undoing the
grouping upstream. The Slack `ts` belongs to the incident, not to a queue row,
and is now looked up per incident at dispatch.

## 6. How does the queueing work?

**A transactional outbox.** The decision to notify is written in the *same
SQLite transaction* as the incident state change that caused it. There is no
window where an incident exists but its notification does not, and no window
where a notification is sent for a change that then rolls back. That is what
makes a Slack outage a delivery problem rather than a data-loss problem.

The worker polls every `OUTBOX_POLL_INTERVAL_MS` (500ms), drains up to 10 rows
per pass, and orders by `(priority, outbox_id)`. Priority is stamped from
severity at enqueue time — `critical=0` through `low=3` — so a backlog released
after an outage delivers the critical page first rather than in the insertion
order of a queue nobody was watching. Row states are `pending → sent | dead |
superseded`.

**Honest limits, and the path past them:**

- **One writer.** SQLite allows a single writer, guarded by one asyncio lock.
  Correct today, and the ceiling is real. The fix is not a rewrite: the outbox
  *is* the interface. Moving to Postgres and claiming rows with
  `SELECT … FOR UPDATE SKIP LOCKED` makes the worker horizontally scalable
  without any change to how intents are produced.
- **No lease.** Rows are not claimed with `locked_by` / `locked_until`, so two
  workers would double-send. That is the schema change that has to land
  *before* a second worker, not after.
- **Polling, not notification.** 500ms of latency is invisible against a human
  incident-response loop and costs one indexed query per tick. `LISTEN/NOTIFY`
  is available once the store is Postgres, if it ever matters.

## 7. How does batching work?

**No fixed window.** Batching is driven by the alert signal itself. On each
alert, the inter-arrival gap feeds an EWMA whose gain adapts to the number of
observations (`α = 2/(n+1)`), and the silence window is the predicted mean gap
plus its observed uncertainty (`mean + √variance`). A storm arriving every
200ms gets a short window; a slow trickle gets a long one. A timer wheel fires
the deadline and moves the incident `ACKNOWLEDGED → QUIESCENT`; deadlines are
persisted, so a restart mid-storm recovers them rather than dropping them.

**The weakness the jury would have found next:** every new alert recomputed the
deadline as `now + window`, so a continuously flapping service kept pushing its
own notification into the future and could — in principle — never be announced
at all. The adaptive window optimised *when* to send while quietly allowing
*never*. Two bounds now close that:

- `QUIET_WINDOW_MAX_MS` (default 5 min) caps any single predicted window.
- `INCIDENT_MAX_BATCH_SPAN_MS` (default 10 min) caps how long one incident may
  defer delivery, measured from its **first** alert. Once reached, the incident
  ships with whatever it has and later alerts keep updating the same card.

Critical alerts skip all of this. `classify_protected_critical` routes
payment, auth-outage, data-loss, `severity=critical`, and `priority=P0` events
straight to PagerDuty and Slack, bypassing dedupe, EWMA, and batching entirely.
Batching is an optimisation for noise; it is never applied to an emergency.

## 7b. If the problem gets fixed during the outage, how do we find out?

This was the follow-up question, and answering it exposed that the system was
**write-only**: it told Slack and PagerDuty what happened and never learned
anything back. There are exactly three ways a fix can become known, and all
three were broken or missing.

### The monitor tells us — and we ignored it

A live bug, not a design gap. Incidents are created directly in `ACKNOWLEDGED`,
but the lifecycle only allowed `RESOLVE` from `QUIESCENT`:

```
("QUIESCENT", "RESOLVE"): "RESOLVED"      # the only resolve edge that existed
```

So `transition_state("ACKNOWLEDGED", "RESOLVE")` returned `None`, and
`process_event` fell back to leaving the state unchanged. **A `resolved`
webhook from Prometheus silently did nothing**, and the incident stayed open
forever. The forward-only lifecycle modelled the happy path — incident goes
quiet, then closes — and could not express the most common real ending, which
is that somebody fixed it while it was still firing. `RESOLVE` is now valid
from every active state.

### A human tells us — but only inside a provider

An engineer paged via PagerDuty during a Slack outage acknowledges and fixes it
*there*. Nothing flowed back, so when Slack recovered the system posted a fresh
actionable card, with an Acknowledge button, for work finished twenty minutes
earlier. Two responders then start on the same problem.

`src/inbound/` closes that loop with two signed endpoints:

| Endpoint | Verifies | Applies |
|---|---|---|
| `POST /v1/slack/interactions` | Slack v0 HMAC + 5-min replay window | Acknowledge / Resolve buttons |
| `POST /v1/pagerduty/webhooks` | PagerDuty v3 HMAC, multi-signature for rotation | `incident.acknowledged`, `incident.resolved` |

Four properties matter:

- **External actions take the same path as alerts.** A human click builds a
  synthetic `NormalizedEvent` and a real `EngineDecision`, persisted by the
  same `persist_decision` ingest uses. Human actions land in the hash-chained
  ledger exactly like machine ones.
- **Acting in one channel updates the other.** The decision emits its own
  outbox intent for every channel *except* the one acted in — so acknowledging
  in PagerDuty moves the Slack card, and the two surfaces cannot disagree.
- **Callbacks are idempotent.** The provider's delivery id (`trigger_id`,
  PagerDuty `event.id`) is the primary key of `inbound_events`. Providers retry
  and users double-click; a replay is recorded and discarded rather than
  re-transitioning the incident or appending a second ledger entry.
- **These endpoints fail closed.** They are public. An unset signing secret
  makes the route reject everything, because an unverified callback endpoint
  would let anyone on the internet resolve any incident by guessing a UUID —
  silencing a real emergency, which is worse than any outage.

PagerDuty does not always echo `dedup_key`. When it does not, the outbox row
that created the PagerDuty incident is the identity map back, so the outbox
doubles as the outbound identity table.

### Nobody tells us — the common case

Plenty of monitoring setups never send a resolve: an alert rule stops matching
and just goes quiet; a recording rule is deleted mid-incident; the `resolved`
webhook is itself delivered over a network and is lost. A permanently-open
incident is worse than noise — it teaches responders the dashboard is wrong.

`SilenceSweeper` treats absence of signal as evidence, with two safeguards.

**The threshold is derived, not fixed.** The engine already models each
incident's own arrival rhythm as an EWMA over inter-arrival gaps, so the
silence threshold is a multiple of *that incident's* predicted gap plus its
uncertainty. An alert firing every 5 seconds is overdue after a minute; an
hourly one is not. A single global timeout would be wrong for both.

**The claim is labelled.** An inferred close is written as
`resolution_source='inferred_silence'`, never as though a human confirmed it,
and the Slack card says *"Presumed resolved — alerts stopped arriving. Not
confirmed by a human."* Criticals are stretched by
`CRITICAL_MULTIPLIER / MULTIPLIER` on top of the floor: closing a payment
outage because it went quiet for fifteen minutes is exactly the mistake that
would destroy trust in the system.

A subtle bug worth noting, because it is the kind that survives review: the
severity stretch is applied *after* the floor. Multiplying first and clamping
afterwards collapsed both severities onto the same 15-minute floor for any
fast-cycling alert — which is most of them — so a critical would have been
presumed resolved on exactly the same evidence as a `low`.

### And on recovery, the card knows

`hydrate_payload` carries `acknowledged_by`, `acknowledged_via` and
`resolution_source` through, so a card delivered after an outage renders as
*"Resolved by dana in pagerduty"* with no Acknowledge button, rather than
inviting a second responder to restart finished work.

## 8. Scaling the model to the cloud on a free tier

The constraint is real: no free tier will host a backend *and* a local LLM. The
answer is that it never needed to.

**The alert path contains no model at all.** Ingest, dedupe, EWMA, correlation,
routing, and delivery are deterministic code over SQLite. A Slack message never
waits on inference, and the whole backend is a small FastAPI process that fits
comfortably in a free tier. This also means model latency, cost, or downtime
can never delay an alert — the property that matters most in incident response.

The model appears in exactly one place: **GitHub incident-to-patch diagnosis**,
which is on-demand, per-incident, and asynchronous. So it is a separate plane
with its own scaling story:

| Tier | What runs it | Covers | Cost |
|---|---|---|---|
| 0 — deterministic | The backend itself | 100% of alerts | free |
| 1 — hosted model | Claude API behind the existing provider interface | on-demand diagnosis | per token |
| 2 — local model | Ollama on a workstation | offline demo, private source | free, no cloud |

The seam already exists. `DiagnosisService` takes a provider, and
`OllamaLocalProvider` is one implementation of it — a hosted provider is
another class behind the same interface, selected by config. Nothing else in
the system changes.

Three properties keep tier 1 cheap enough to be real:

- **Bounded context.** `GITHUB_DIAGNOSIS_MAX_FILES=6` and a 48KB total ceiling
  are enforced before a request is built, so cost per diagnosis has a hard
  upper bound rather than scaling with repository size.
- **Cached by content.** `github_incident_analyses.source_context_digest`
  already keys a diagnosis to the exact snapshot it was derived from, so the
  same incident against the same commit never pays twice.
- **Triggered, not automatic.** Diagnosis runs when an engineer asks for it on
  an incident, not on every alert.

Deployment shape: dashboard on a static/edge free tier, backend on a small
free container instance, model as an API call. Nothing in the system requires a
GPU or a machine large enough to hold model weights.

---

## What is not built yet

Stated plainly, because these are the next questions:

Everything previously listed here is now built: storm grouping (§4), row
leasing, the email channel, and the hosted model provider. What each turned
into:

- **Storm grouping** — `incident_groups`, anchor/root separation, incremental
  union with merge, one card per cascade (§4).
- **Multi-worker delivery** — outbox rows are claimed under an expiring lease
  (`locked_by` / `locked_until`), so two workers split the queue instead of
  double-sending, and a worker that dies mid-dispatch has its rows reclaimed
  rather than stranded. The claim and the read are one statement.
- **Email** — a real SMTP channel replaces the stub that marked every row
  delivered while sending nothing, which had made the third failover hop look
  healthy when it was inert. Standard library only, blocking I/O pushed to a
  thread so an unreachable relay cannot stall the event loop, and `NOOP` as the
  side-effect-free probe.
- **Hosted model provider** — tier 1 for cloud deployments (§8). It sends
  bounded source to a third party, so it is gated on its own flag rather than
  on an API key being present, and Ollama wins when both are configured.

Further gaps found in the system-design review — ingest authentication, webhook
retry idempotency, per-service routing, self-monitoring, and the graph's
quadratic cost during a storm — are tracked in
[System design gaps](system-design-gaps.md).

## Where the code lives

| Concern | File |
|---|---|
| Failure classification | [src/outbox/failure_policy.py](../src/outbox/failure_policy.py) |
| Circuit breaker, probes, outage records | [src/outbox/channel_health.py](../src/outbox/channel_health.py) |
| Priority and failover chain | [src/outbox/routing.py](../src/outbox/routing.py) |
| Coalescing, late rendering, digest | [src/outbox/recovery.py](../src/outbox/recovery.py) |
| Drain loop tying it together | [src/outbox/worker.py](../src/outbox/worker.py) |
| Adaptive batching + its ceilings | [src/engine/adaptive_ewma.py](../src/engine/adaptive_ewma.py), [src/engine/process_event.py](../src/engine/process_event.py) |
| Tests for all of the above | [tests/test_outbox_resilience.py](../tests/test_outbox_resilience.py) |
| Live drill | [scripts/outage_drill.py](../scripts/outage_drill.py) |
| Callback signature verification | [src/inbound/signatures.py](../src/inbound/signatures.py) |
| Applying an external action | [src/inbound/reconcile.py](../src/inbound/reconcile.py) |
| Slack / PagerDuty callback routes | [src/inbound/router.py](../src/inbound/router.py) |
| Presumed resolution from silence | [src/engine/silence_sweeper.py](../src/engine/silence_sweeper.py) |
| Tests for the inbound plane | [tests/test_inbound_reconcile.py](../tests/test_inbound_reconcile.py) |
| Storm grouping | [src/graph/storm_grouping.py](../src/graph/storm_grouping.py) |
| SMTP failover channel | [src/outbox/email.py](../src/outbox/email.py) |
| Hosted diagnosis provider | [src/github_integration/anthropic_provider.py](../src/github_integration/anthropic_provider.py) |
| Lease-based row claiming | `OutboxWorker._claim` in [src/outbox/worker.py](../src/outbox/worker.py) |
