# System design gaps

A pass over PulseGraph looking for what an experienced reviewer would attack
next, now that the delivery and reconciliation planes are closed. Each entry
states the gap, the concrete failure it produces, and the design that fixes it.

Ordered by what gets asked first, not by effort. Gaps 1-4, 6 and 11 are
now fixed; the rest stand.

---

## 1. The front door has no lock — FIXED

`POST /v1/ingest/prometheus` accepts anything from anyone. Meanwhile GitHub
webhooks are HMAC-verified, and the new Slack and PagerDuty callbacks verify
signatures and fail closed. The asymmetry is glaring: **the one endpoint that
creates incidents is the one endpoint nobody has to prove anything to.**

The interesting attack is not a flood, it is a *forgery*. Anyone who can reach
the ingest URL can POST a `resolved` alert whose labels match a real, firing
incident. Because dedupe keys on `service | alertname | severity | stable
labels`, a forged resolve lands on the real incident and closes it. An attacker
silences a production emergency with one HTTP request and no credentials — the
exact inverse of what an alerting system is for.

**Fixed** in [src/ingest/auth.py](../src/ingest/auth.py), with two checks,
because authentication alone is not enough:

- **Who are you.** A bearer token compared in constant time, presented as
  `Authorization: Bearer …` or `X-PulseGraph-Token`. Bearer rather than a body
  HMAC because Alertmanager can set an `Authorization` header natively but
  cannot sign a request body — a scheme the sender cannot implement is a scheme
  that ends up switched off.
- **What may you say.** Every credential is bound to a scope prefix over
  `environment/cluster`. A staging token cannot write *or resolve* anything in
  `production/eu-west`, so a leaked staging credential cannot silence
  production. Scope is already the boundary the engine dedupes within, so this
  is authorisation rather than new modelling.

Two details worth keeping: the whole batch is authorised before any of it is
written, so a payload containing one out-of-scope alert cannot half-apply; and
with `INGEST_AUTH_ENABLED` true and no tokens configured, ingest returns 503
rather than falling open — an unauthenticated alerting system is worse than a
loudly misconfigured one.

Enabling this **breaks any sender that does not present a token**, which is the
correct outcome and was the point. `scripts/` read the token from
`INGEST_TOKENS` in `.env` (or `INGEST_TOKEN`), so the demo keeps working once
one is set.

## 2. The graph is quadratic, inside the global write lock — FIXED

This is the one I would push hardest on, because it degrades exactly when the
product is supposed to shine.

On every non-bypassed alert, `persist_and_observe`:

1. selects **all** active incidents in the scope,
2. calls `observe_incident`, which does an edge lookup + upsert **per related
   incident**,
3. then calls `rank_root_cause`, which scans **every edge** joined to active
   incidents and re-ranks from scratch.

With `A` active incidents in a scope, edges are `O(A²)` and step 3 is `O(A²)`
per alert. All of it runs inside the `BEGIN IMMEDIATE` transaction, holding the
single SQLite writer lock, while ingest requests queue behind it.

The demo — ten duplicates and a three-service cascade — never reveals this,
because `A` is about 4. A genuine incident with 200 concurrent distinct
incidents in one cluster makes every subsequent alert do ~40,000 edge rows of
work while holding the lock that all ingest depends on. **The system gets
slowest precisely during the storm it exists to absorb.**

**Fixed** by bounding both halves:

- **The neighbourhood.** Correlation now considers only incidents active
  within `CORRELATION_WINDOW_MS` (15 min), ordered by recency and capped at
  `CORRELATION_MAX_NEIGHBOURS` (25). `O(A)` per alert becomes `O(K)` with K
  fixed, so total edges grow with alerts rather than alerts squared.
- **The ranking.** `rank_root_cause` now takes a `candidate_ids` set and ranks
  within the same bounded neighbourhood. That is cheaper *and* more correct: a
  global rank names the loudest thing anywhere in the scope, so a large
  unrelated incident could outweigh the actual leader of the cascade being
  explained, and the hint on the card would describe a different event. The
  unrestricted path keeps a `DEFAULT_MAX_EDGES` backstop so a pathological
  graph degrades the hint rather than the write transaction.

Measured on 75 active incidents in one scope, ten alerts: **795 edges before,
240 after** — and the gap widens quadratically with the size of the storm.
[tests/test_graph_bounds.py](../tests/test_graph_bounds.py) pins the bound, the
window, and the scoped ranking.

**The remaining half is now done too.** Ranking has left the write
transaction entirely
([root_cause_worker.py](../src/graph/root_cause_worker.py)). A root cause is an
enrichment, not a transactional invariant: nothing about durably recording an
alert or delivering its notification depends on knowing what caused it.

The larger win is **debouncing**. Five hundred alerts in a storm used to trigger
five hundred rankings of a neighbourhood that barely changed between them. The
observation round marks its scope dirty and the worker sweeps at most once per
interval, so the same storm costs a handful of passes — the work per alert stops
scaling with the alert rate at all. A test pins this: forty alerts collapse to
**one** ranking pass, and a scope with no new evidence is not re-ranked.

Nothing is lost to the delay, because delivery payloads render from live
incident state at send time — the same property that lets a card recovered
after an outage show the current alert count. A hint that lands after a card
was queued still reaches it.

Writing this surfaced a bug worth recording, because it is **gap 4 biting in a
new place**. Dirtiness was first written as `ranked_at < last_observed_at` —
but `last_observed_at` carries the *monitor's* clock while `ranked_at` is wall
clock. Comparing the two means a source whose clock runs a few minutes behind
leaves its scope permanently clean, and root cause silently stops updating
forever. Dirtiness is now a **revision counter**, which has no clock to
disagree with.

## 3. Correlation is temporal only, and will produce confident nonsense — FIXED

Two services that break at the same time get an edge, whether or not they have
anything to do with each other. During a broad event — a bad deploy, an AZ
blip — *everything* co-occurs with everything, so edge weights spike uniformly
and `rank_root_cause` confidently names whichever incident happened to fire
first. A responder who is told the wrong root cause once will stop reading the
field entirely, which is worse than not having it.

**First, a correction to this entry as originally written.** It claimed the
GitHub integration gives dependency topology "for free" via the
`service → repository` mapping. It does not. That table is only
`service → repository_id`; there is no manifest parsing anywhere in
`src/github_integration/`. A real dependency graph means fetching and parsing
package.json / requirements.txt / go.mod across ecosystems and resolving
dependencies to repositories to services — a subsystem, and one that is dead
weight whenever the GitHub App is not configured. It is still worth building,
but it was not the cheapest or the largest win available.

**The larger win was already latent in the code.** `edge_decay.DecayedWeights`
carries three fields — `joint`, `source`, `target` — and `increment_weights`
maintains all three, but `observe_incident` passed the joint count for all
three, so the marginals carried no information. `edges.weight` was therefore a
raw decayed co-occurrence count with **no normalisation by how often each
incident fires**. That is precisely why a broad event produced confident
nonsense: two services that each fire constantly co-occur constantly *by
chance*, and a raw count cannot tell that apart from a causal pair.

**Fixed** with two changes in
[root_cause_ranker.py](../src/graph/root_cause_ranker.py):

- **Lead versus follow.** The old ranker summed outbound weight, so a
  chronically unhealthy service — which co-occurs with everything, and follows
  as often as it leads — won outright. Ranking now uses *net* lead
  (outbound − inbound), so a node that does both in equal measure scores zero
  however loud it is. `graph_node_stats` and `graph_scope_stats` supply the
  marginals for a **lift** term (`P(a,b) / P(a)P(b)`) that divides chance out
  of each edge; when those are unavailable lift falls back to 1.0 and the model
  degrades to directional weighting rather than failing.
- **Permission to say nothing.** The ranker previously always answered.
  Confidence is now separation of the leader from the runner-up, scaled by how
  much evidence exists, and below `MIN_CONFIDENCE` the result is `None` and the
  card stays silent. A single co-occurrence no longer names a cause at all.

One subtlety the tests caught: separation as a pure *ratio* is scale-free, so
when every node leads and follows equally the nets collapse to floating-point
dust and the ratio between two specks reads as a landslide. A `MIN_DOMINANCE`
floor asks the absolute question instead — of everything this node did, how
much of it was net leading — and that is what makes the bad-deploy case return
nothing.

The hint a responder reads is now the incident's **title and a confidence
percentage** rather than a UUID and a raw weight.

This is also the honest answer to "is there ML in this?" — there does not need
to be, and a prior plus decayed counts is more defensible than a model nobody
can debug at 3am.

**Still outstanding:** the structural prior itself. Manifest-derived service
topology would let the ranker discount a pair with no call path between them,
which lift cannot do on timing evidence alone.

## 4. Event time and processing time are mixed — FIXED

The quiet deadline is computed from **source** time:

```python
quiet_at_ms = _event_time_ms(event) + window   # event.fired_at, from the monitor
```

but `TimerWorker` fires it against **wall-clock** time (`time.time()`). The two
are only interchangeable when every monitor's clock is correct and no event is
ever delayed.

- A monitor whose clock is 10 minutes slow produces a deadline already in the
  past — the incident fires immediately, defeating batching entirely.
- A clock 10 minutes fast delays the notification by 10 minutes.
- Alerts replayed after a monitoring outage arrive with old `fired_at` and get
  deadlines in the past, so a recovered Alertmanager causes a burst.

`last_gap = max(0.0, ...)` already quietly clamps negative gaps, which is the
symptom showing through.

**This gap bit twice before it was fixed.** The background root-cause worker
was first written to decide whether a scope needed re-ranking by comparing
`ranked_at` (wall clock) against `last_observed_at` (the monitor's clock); a
source running behind would have left its scope permanently clean and root
cause would have stopped updating silently. That one is now a revision counter,
which has no clock to disagree with — the general lesson being that when two
clocks would otherwise be compared, the best fix is often to remove the clock
from the question entirely.

**This was worse than the entry described.** Writing the reproduction found a
third instance, and it is the most damaging one in the system so far:
`SilenceSweeper` measured silence as `wall_clock_now - last_alert_at` — our
clock minus the monitor's. A source running twenty minutes behind therefore
makes every **brand-new** incident look like it has already been quiet for
twenty minutes, past the fifteen-minute floor. The sweeper closes it and the
card reads *"presumed resolved"*.

That is the alerting system silencing a live production incident because a VM
drifted — the same outcome as the forged-resolve attack in gap 1, with **no
attacker required**. It is now the first test in
[test_clock_skew.py](../tests/test_clock_skew.py).

**Fixed** by naming the two clocks in the schema and keeping them apart:

- `first_alert_at` / `last_alert_at` remain **event time**, and stay the basis
  for inter-arrival gaps. A source's own clock is the right measure of its own
  cadence — a constant offset cancels out in a difference — and recomputing
  gaps from arrival time would let network jitter rewrite the EWMA.
- New `first_ingested_at` / `last_ingested_at` are **processing time**, and now
  drive every *elapsed-time* decision: the quiet deadline (fired against wall
  clock by the timer wheel, so it must be computed against wall clock),
  silence detection, and the correlation window. Both are nullable and every
  read falls back to the event-time column, so an existing database keeps
  working rather than scheduling everything at the epoch.

**Drift is now also visible.** Separating the clocks stops drift *breaking*
things; `source_clock_skew` stops it *hiding*. Skew is recorded per
`(source, scope_key)` on the write path as a single upsert with no preceding
read, keeping the worst offset ever seen rather than only the latest, and a
throttled warning names the drifted source. Without it a team has a system that
quietly behaves oddly and no way to learn which exporter slipped.

Measured: a live incident under twenty minutes of drift now survives, its quiet
deadline lands **+5s in the future** instead of ~20 minutes in the past, and the
log names the source and the offset.

## 5. Webhook retries inflate the numbers

`normalize_prometheus` mints `event_id=uuid4()` per parse. Alertmanager retries
on any non-2xx and re-sends on its own schedule, so the same logical alert
arrives repeatedly and each arrival is a fresh event: a new ledger row, another
`alert_count` increment, another outbox intent.

Dedupe still collapses them onto one incident, so this does not produce extra
Slack messages — but `alert_count` is the headline number on the card and in
the pitch. "517 alerts collapsed" is not a number you want inflated by your own
retry handling.

**Design.** Idempotency belongs at the delivery boundary, not the identity one:
derive the event id from what the *source* considers stable — Alertmanager's
`groupKey` plus the alert fingerprint plus `startsAt` — and make `event_id` a
unique key. A retry then becomes a no-op insert rather than a new event. The
inbound plane already does exactly this with `inbound_events`; ingest should
use the same pattern.

## 6. Nothing watches the watcher — FIXED

If PulseGraph crashes, nobody finds out. There are no alerts about the alerting
system, and its silence is indistinguishable from a quiet night — which is the
same ambiguity the silence sweeper was built to resolve for incidents, left
unresolved for the system itself.

It is worse than a normal outage, because failures correlate: the thing most
likely to take down PulseGraph is the same infrastructure event that should be
generating the alerts it is failing to deliver.

**Fixed** in [src/selfcheck/](../src/selfcheck/), around one distinction:
**being alive is not being healthy.**

A liveness probe proves the process is running. It proves nothing about whether
alerts reach anyone — and PulseGraph answers `200 OK` on every request while
its outbox stalls, its breakers sit open, or its drain loop is dead. That gap
is now demonstrable in two lines:

```
/v1/health      → {"status": "healthy"}
/v1/health/self → {"verdict": "unhealthy",
                   "reasons": ["background worker(s) not running: outbox"]}
```

Three pieces:

- **`/v1/health/self`** reports on *delivery*, not uptime. Every signal it
  returns was already being recorded and none of it was reachable:
  `outbox` knows what is undelivered, `channel_health` which providers are
  down, `channel_outages` what is still open, `source_clock_skew` which
  exporter drifted, `graph_scope_stats` how far ranking has fallen behind.
  Gathering is read-only and never raises — a self-check that fails when the
  system is unwell reports nothing exactly when it matters, so an unreadable
  signal becomes its own finding.
- **A dead man's switch** (`HeartbeatEmitter`) pings an external watchdog, and
  is **gated on the verdict rather than on being alive**. A liveness-driven
  heartbeat would keep insisting all is well while nothing is delivered — an
  *active* all-clear, which is worse than no heartbeat at all. When delivery
  is broken it goes silent and the watchdog pages. It is a direct HTTP call,
  never an outbox row: routing the heartbeat through the machinery it checks
  would mean a stalled outbox also stalls the heartbeat, which happens to page
  but only by luck, and a beat queued behind a backlog would report a system
  that died an hour ago as fine.
- **A severity split that respects the delivery plane.** An open breaker is
  `DEGRADED`, not `UNHEALTHY`, and still heartbeats. Failing over is the system
  working, and paging for it would punish correct behaviour — during the
  provider incident a responder is already handling. `UNHEALTHY` is reserved
  for "alerts are not getting out".

**The honest non-answer.** Silence from every source is reported as an
*observation* and never pages. A quiet night and a severed intake path look
identical from inside, and guessing wrong is costly both ways: page on every
quiet night and the heartbeat becomes noise; treat a severed intake as calm and
the blind spot is total. Only something outside can tell them apart — which is
the argument for the external watchdog restated from the other side.

With `HEARTBEAT_URL` unset the emitter logs a warning saying plainly that
nothing anywhere will notice if this process dies, rather than quietly doing
nothing.

**Still outstanding:** deploying outside the blast radius is a deployment
decision, not a code one — an alerting system inside the cluster it monitors
will be down when it is needed. And the dashboard tile for `/v1/health/self`
is not built; the endpoint is there for it.

## 7. No backpressure — load is absorbed by getting slower

Ingest takes the global write lock per event and does the full engine, graph,
and ledger work synchronously before responding. There is no queue, no
admission control, and no load shedding. Under a genuine storm the lock queue
grows, HTTP requests time out, and Alertmanager retries — adding load in
exactly the moment there is too much of it.

**Design.** Split accept from process, which the outbox pattern already
establishes on the way out:

- Accept, validate, append to a durable intake queue, return `202`. The
  expensive path drains asynchronously.
- **Shed by severity, not arrival order.** Under pressure, drop or sample
  `low`; never shed `critical`. The priority column added for outbox drain is
  the same idea applied at the other end.
- Publish a documented ingest rate limit per source, so a runaway alert rule
  degrades its own source rather than everyone's.

## 8. One channel, no ownership

`SLACK_CHANNEL_ID` is a single global setting: every incident for every service
goes to one channel and one PagerDuty key. Real organisations route by
ownership, and routing is what makes an alerting system adoptable beyond one
team.

**Design.** Ownership as data, not configuration:

- A routing table `service → (slack channel, escalation policy, owning team)`,
  resolved at delivery time. The failover chain then becomes *per route*, so a
  team without PagerDuty falls back differently from one with it.
- Fall back to a catch-all channel for unmapped services, and surface the
  unmapped list — unrouted alerts are an ownership gap worth showing.
- The `service → repository` mapping in the GitHub integration is already
  half of this table.

## 9. No suppression, so deploys look like outages

There is no maintenance window, no deploy-aware suppression, and no dependency
suppression. A rolling deploy lights up every downstream service, and the
system faithfully pages for all of it. Teams respond by muting the channel,
which quietly defeats the entire product.

**Design.**

- **Maintenance windows** scoped by service and time, suppressing notification
  while still recording incidents — suppressed, not discarded, so the audit
  ledger stays complete.
- **Deploy correlation.** The GitHub App already receives repository events;
  an incident that starts within minutes of a deploy to its mapped repository
  should say so on the card. That is a high-value, low-effort use of a
  component that already exists.
- **Dependency suppression.** If a database incident is the ranked root cause,
  its downstream children are consequences; notify once for the cause and
  summarise the effects. This is the same feature as cross-incident storm
  grouping, seen from the routing side.

## 10. Acknowledgement has no timeout

Now that acknowledgement works, the obvious follow-up is what happens when it
*doesn't*. Nothing escalates. A critical acknowledged by nobody sits exactly as
quietly as one being actively worked, and an incident acknowledged then
abandoned is invisible.

**Design.** Ack is a promise with a deadline: if a critical is unacknowledged
after N minutes, escalate to the next channel in the failover chain — the
mechanism already exists, it just needs a timer as a trigger instead of an
outage. The timer wheel already does durable, restart-surviving deadlines.

## 11. Flapping will now produce churn — FIXED

The silence sweeper closes quiet incidents, and `_next_state` reopens a
`RESOLVED` incident on the next alert. A service flapping on a cycle longer
than its silence threshold will resolve and reopen indefinitely, and each
transition posts a card update. Two correct features compose into a noise
generator.

**Measured before fixing:** ten close/reopen cycles produced **21 card
updates** for a single incident — the system built to stop alert fatigue
generating it.

**Fixed** with damping and hysteresis:

- **Reopens are counted.** `incidents.reopen_count` separates "this resolved
  and came back" from "this alert is broken". Past
  `FLAP_REOPEN_THRESHOLD` the incident is marked flapping.
- **The repeats collapse, the news does not.** While flapping, at most one card
  update per `FLAP_DIGEST_INTERVAL_SECONDS`. The early transitions still
  notify, because a first close and a first reopen are genuinely new
  information; only the repetition is suppressed. The incident keeps being
  recorded either way — damping changes what is *said*, never what is *known*.
- **Closing gets harder each time.** The silence threshold is multiplied by
  `FLAP_HYSTERESIS_FACTOR ** reopens`, capped so a long-running flapper widens
  toward the ceiling rather than past it. Closing on the same evidence that
  closed it last time guarantees the next reopen.
- **The flapping is the finding.** The card says so, names the reopen count,
  and says plainly that repeated cycling usually means the alert threshold is
  wrong rather than the service being unhealthy. That is an alert-quality
  problem, and this system is the only thing in the stack positioned to notice
  it.

After: 21 transitions produce a handful of cards, and — the property that
actually matters — twenty *more* transitions add **zero**. A service can flap
for hours; the notification cost stops growing with it. `FLAP_DAMPING_ENABLED`
turns it off for an operator who wants every transition.

## 12. Nothing is ever deleted

`raw_events`, `edges`, `outbox`, and now `inbound_events` grow without bound in
a single SQLite file on local disk. There is no retention, archival, or
compaction, and no answer for what happens at 50M rows.

There is a real tension here worth naming rather than hiding: the ledger is
**hash-chained**, so deleting old rows breaks verification. Retention and
tamper-evidence pull in opposite directions.

**Design.** Checkpoint rather than delete: periodically seal a range, store a
signed checkpoint hash covering it, archive the rows to cold storage, and let
`verify_chain.py` verify across checkpoints. That keeps the tamper-evidence
property while bounding the live database — and "we thought about what happens
to the audit log at scale" is a better answer than an unbounded file.

---

## Three ideas that raise the level

Beyond closing gaps, these change what the system *is*.

**Alert quality as the product.** Every fact needed to grade a team's alerting
is already in the ledger: which alerts never got acknowledged, which resolved
themselves before anyone looked, which fingerprints flap, which fire nightly
and are always ignored. Today the system reduces noise; it could *report* on
the noise — "these 6 rules produced 40% of your alerts and 0 acknowledgements"
— and turn a fatigue filter into a system that gets alerting permanently
fixed. It is a reporting query over data that already exists.

**A feedback loop on grouping.** Correlation currently has no ground truth. A
single "these were not related" / "this was the cause" control on the card
gives labelled data, which can tune edge weights and root-cause thresholds per
environment. It is the difference between a heuristic that is fixed at
implementation time and one that improves with use — and it needs no ML, only
a stored correction.

**Confidence as a first-class output.** Several outputs are already assertions
of differing strength: a confirmed resolve versus an inferred one, a strong
causal chain versus a coincidence, a diagnosis grounded in source versus a
fallback. The inferred-resolution label proved the pattern works. Applying it
everywhere — every claim carrying how much it should be trusted — is what
separates a tool responders believe from one they learn to second-guess.
