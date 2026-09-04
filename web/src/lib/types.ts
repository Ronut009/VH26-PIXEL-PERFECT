// Mirrors src/db/schema.sql. Lifecycle values are the uppercase set the
// backend enforces via CHECK (status IN ('OPEN','ACKNOWLEDGED','QUIESCENT',
// 'RESOLVED')) — see src/db/schema.sql. The SSE stream calls this same field
// `state` and camelCases everything; normalizeIncident() below absorbs both
// shapes so components only ever see one.

export type Severity = "critical" | "high" | "medium" | "low";
export type Lifecycle = "OPEN" | "ACKNOWLEDGED" | "QUIESCENT" | "RESOLVED";
export type RouteDecision = "slack" | "pagerduty" | "email" | "suppressed" | null;

export interface Incident {
  incident_id: string;
  title: string;
  summary: string | null;
  severity: Severity;
  status: Lifecycle;
  alert_count: number;
  first_alert_at: string | null;
  last_alert_at: string | null;
  ewma_rate: number;
  route_decision: RouteDecision;
  root_cause_hint: string | null;
  quiet_at_ms: number | null;
  created_at: string | null;
  updated_at: string;
}

export interface IncidentEdge {
  src_incident_id: string;
  dst_incident_id: string;
  weight: number;
  last_seen_at: string;
}

const LIFECYCLE_ALIASES: Record<string, Lifecycle> = {
  OPEN: "OPEN",
  ACKNOWLEDGED: "ACKNOWLEDGED",
  QUIESCENT: "QUIESCENT",
  RESOLVED: "RESOLVED",
  // Pre-Phase-2 rows and the older frontend contract used these.
  new: "OPEN",
  active: "OPEN",
  acknowledged: "ACKNOWLEDGED",
  quiet: "QUIESCENT",
  resolved: "RESOLVED",
};

export function toLifecycle(value: string | null | undefined): Lifecycle {
  if (!value) return "OPEN";
  return LIFECYCLE_ALIASES[value] ?? LIFECYCLE_ALIASES[value.toUpperCase()] ?? "OPEN";
}

/** Accepts a REST row (snake_case) or an SSE incident (camelCase). */
export function normalizeIncident(raw: Record<string, unknown>): Incident {
  const pick = <T>(...keys: string[]): T | undefined => {
    for (const key of keys) {
      const value = raw[key];
      if (value !== undefined && value !== null) return value as T;
    }
    return undefined;
  };

  return {
    incident_id: pick<string>("incident_id", "incidentId") ?? "",
    title: pick<string>("title") ?? "untitled incident",
    summary: pick<string>("summary") ?? null,
    severity: (pick<string>("severity") ?? "medium").toLowerCase() as Severity,
    status: toLifecycle(pick<string>("status", "state")),
    alert_count: Number(pick<number>("alert_count", "alertCount") ?? 1),
    first_alert_at: pick<string>("first_alert_at", "firstAlertAt") ?? null,
    last_alert_at: pick<string>("last_alert_at", "lastAlertAt", "updated_at", "updatedAt") ?? null,
    ewma_rate: Number(pick<number>("ewma_rate", "ewmaRate") ?? 0),
    route_decision: (pick<string>("route_decision", "routeDecision") ?? null) as RouteDecision,
    root_cause_hint: pick<string>("root_cause_hint", "rootCauseHint") ?? null,
    quiet_at_ms: (pick<number>("quiet_at_ms", "quietAtMs") as number | undefined) ?? null,
    created_at: pick<string>("created_at", "createdAt") ?? null,
    updated_at: pick<string>("updated_at", "updatedAt") ?? new Date().toISOString(),
  };
}

/**
 * Accepts a REST edge row or an SSE edge.
 *
 * The stream names the weight `decayed_joint_weight` — the directed decayed
 * joint weight the co-occurrence graph records (src/stream/sse_broker.py, and
 * the graph evidence contract in the root README). Missing that key silently
 * flattened every edge to weight 1, which reads as "all correlations are
 * equally strong".
 */
export function normalizeEdge(raw: Record<string, unknown>): IncidentEdge {
  return {
    src_incident_id: (raw.src_incident_id ?? raw.sourceIncidentId ?? "") as string,
    dst_incident_id: (raw.dst_incident_id ?? raw.targetIncidentId ?? "") as string,
    weight: Number(raw.weight ?? raw.decayed_joint_weight ?? raw.jointWeight ?? 1),
    last_seen_at: (raw.last_seen_at ?? raw.lastSeenAt ?? "") as string,
  };
}

// ── Backend health ───────────────────────────────────────────────────────
// GET /v1/health answers `{ status: "healthy" }`, or `{ status: "unhealthy",
// error }` when the writer connection fails its `SELECT 1`.

export interface HealthReport {
  status: "healthy" | "unhealthy";
  error?: string;
}

// ── Same-origin API errors ───────────────────────────────────────────────
// Every /api/* route handler normalizes failures to this envelope so the
// client has something typed to branch on instead of parsing prose.

export type ApiErrorCode =
  | "backend_unreachable"
  | "admin_token_missing"
  | "invalid_request"
  | "upstream_error"
  | "invalid_upstream_response";

export interface ApiErrorBody {
  error: {
    code: ApiErrorCode;
    message: string;
    upstream_status?: number;
  };
}

// ── GitHub integration ───────────────────────────────────────────────────
// Mirrors src/github_integration/{router,store,analysis_store,diagnosis}.py.
// The integration is read-only by construction: it can pin a snapshot and
// describe a change, and has no capability to push, commit, branch, open a
// pull request, or modify a repository.

// SQLite hands booleans back as 0/1; the API passes the raw column through.
export type SqliteBool = 0 | 1 | boolean;

export interface GithubRepository {
  repository_id: number;
  installation_id: number;
  owner: string;
  name: string;
  full_name: string;
  default_branch: string;
  html_url: string | null;
  is_private: SqliteBool;
  is_archived: SqliteBool;
  is_selected: SqliteBool;
  last_seen_commit_sha: string | null;
  updated_at: string;
  account_login: string;
  installation_status: string;
  /** The monitored service mapped to this repository, if any. */
  service: string | null;
}

export interface GithubInstallationSync {
  status: string;
  installation_id: number;
  repository_ids: number[];
}

export interface GithubServiceMapping {
  service: string;
  repository_id: number;
  full_name: string;
}

export interface GithubSnapshotFile {
  path: string;
  blob_sha: string;
  mode: string;
  object_type: string;
  size_bytes: number | null;
}

/** An immutable commit/tree inventory that pins analysis to one commit. */
export interface GithubSnapshot {
  snapshot_id: string;
  repository_id: number;
  full_name: string;
  ref: string;
  commit_sha: string;
  tree_sha: string;
  file_count: number;
  tree_truncated: SqliteBool;
  created_at: string;
  /** Only present when requested with `include_files`. */
  files?: GithubSnapshotFile[];
}

export type DiagnosisStatus = "diagnosed" | "fallback";

export type DiagnosisFallbackReason =
  | "no_source_excerpts"
  | "provider_unavailable"
  | "invalid_provider_result"
  | "insufficient_evidence";

export interface DiagnosisEvidence {
  kind: "incident" | "source_excerpt";
  explanation: string;
  file_path: string | null;
  blob_sha: string | null;
  start_line: number | null;
  end_line: number | null;
}

export interface RootCauseHypothesis {
  summary: string;
  reasoning: string;
}

export interface ProposedFix {
  summary: string;
  steps: string[];
  affected_paths: string[];
  requires_human_review: true;
  automatically_applied: false;
}

/** Returned instead of a guess when source or model access is unavailable. */
export interface SafeFallback {
  reason: DiagnosisFallbackReason;
  message: string;
  next_steps: string[];
  requires_human_review: true;
}

export interface Diagnosis {
  status: DiagnosisStatus;
  provider: string;
  confidence: number;
  root_cause_hypothesis: RootCauseHypothesis | null;
  evidence: DiagnosisEvidence[];
  proposed_fix: ProposedFix | null;
  fallback: SafeFallback | null;
}

export interface GithubAnalysis {
  analysis_id: string;
  incident_id: string;
  service: string;
  repository_id: number;
  snapshot_id: string;
  diagnosis: Diagnosis;
  source_context: {
    digest: string | null;
    excerpt_count: number;
    byte_count: number;
  };
  created_at: string;
}

export interface PatchChangedFile {
  path: string;
  action: "create" | "update" | "delete";
  before_sha256: string | null;
  after_sha256: string | null;
  before_bytes: number | null;
  after_bytes: number | null;
  explanation: string | null;
}

export interface PatchReview {
  patch_id: string;
  summary: string;
  rationale: string | null;
  changed_files: PatchChangedFile[];
  /** A human-reviewable diff. Nothing in PulseGraph can apply it. */
  unified_diff: string;
  metadata: Record<string, unknown>;
}

export interface GithubPatchPreview {
  analysis_id: string;
  snapshot_id: string;
  human_review_required: boolean;
  automatically_applied: boolean;
  patch: PatchReview;
}
