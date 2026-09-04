"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchGithubRepositories, PulseGraphApiError } from "@/lib/api";
import type { GithubRepository } from "@/lib/types";

/**
 * Whether code investigation is available, and if not, why.
 *
 * Every branch carries operator-facing copy, because "Investigate code" being
 * greyed out is only useful if it also says what to fix. This reads the
 * repository list once — it is not polled, since a GitHub App installation
 * doesn't change on a four-second cadence.
 */
export type GithubReadiness =
  | { kind: "loading" }
  | { kind: "ready"; repositories: GithubRepository[] }
  | { kind: "unconfigured"; message: string }
  | { kind: "unauthorized"; message: string }
  | { kind: "backend_unavailable"; message: string }
  | { kind: "error"; message: string };

export function useGithubReadiness() {
  const [readiness, setReadiness] = useState<GithubReadiness>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  const reload = useCallback(() => {
    setReadiness({ kind: "loading" });
    setAttempt((count) => count + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      let next: GithubReadiness;
      try {
        next = { kind: "ready", repositories: await fetchGithubRepositories() };
      } catch (error) {
        next = classify(error);
      }
      if (!cancelled) setReadiness(next);
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  return { readiness, reload };
}

function classify(error: unknown): GithubReadiness {
  if (!(error instanceof PulseGraphApiError)) {
    return { kind: "error", message: "The GitHub connection check failed." };
  }

  // The dashboard server has no token, so the request never left it.
  if (error.code === "admin_token_missing") {
    return {
      kind: "unconfigured",
      message:
        "GitHub investigation is not configured on the dashboard server. Set GITHUB_ADMIN_TOKEN in web/.env.local and restart.",
    };
  }

  if (error.code === "backend_unreachable") {
    return {
      kind: "backend_unavailable",
      message: "The PulseGraph backend is unreachable, so GitHub investigation is unavailable.",
    };
  }

  // The backend answers 503 when its own GITHUB_ADMIN_TOKEN is unset, and 401
  // when the two halves disagree about its value.
  if (error.upstreamStatus === 503) {
    // FastAPI detail strings do not end in a full stop, so add one before
    // appending our own sentence.
    const detail = error.message.replace(/\s*$/, "").replace(/\.?$/, ".");
    return {
      kind: "unconfigured",
      message: `${detail} Configure it in the backend's .env, then restart the backend.`,
    };
  }

  if (error.upstreamStatus === 401) {
    return {
      kind: "unauthorized",
      message:
        "The backend rejected the dashboard's admin token. GITHUB_ADMIN_TOKEN must match in web/.env.local and the backend's .env.",
    };
  }

  return { kind: "error", message: error.message };
}

/**
 * The repository a service's code investigation would read, if one is mapped.
 * Service names come from incident titles, which the engine builds as
 * `${service} — ${alertname}`, and are matched case-insensitively because a
 * mapping is typed by hand.
 */
export function repositoryForService(
  readiness: GithubReadiness,
  service: string,
): GithubRepository | null {
  if (readiness.kind !== "ready") return null;
  const needle = service.trim().toLowerCase();
  if (!needle) return null;
  return (
    readiness.repositories.find((repository) => repository.service?.toLowerCase() === needle) ?? null
  );
}

export interface InvestigationAvailability {
  enabled: boolean;
  /** Why the action is unavailable, or what it will do when it is. */
  explanation: string;
}

/**
 * Whether "Investigate code" can run for one incident.
 *
 * A disabled control that doesn't say why is a dead end, so every branch
 * returns the specific blocker — an unconfigured token, an unmapped service,
 * a demo incident that has no real counterpart in the database.
 */
export function investigationAvailability(
  readiness: GithubReadiness,
  service: string,
  isSample: boolean,
): InvestigationAvailability {
  if (isSample) {
    return {
      enabled: false,
      explanation:
        "This is sample data shown because the backend is unreachable. Start the backend to investigate a real incident.",
    };
  }

  switch (readiness.kind) {
    case "loading":
      return { enabled: false, explanation: "Checking the GitHub connection…" };
    case "unconfigured":
    case "unauthorized":
    case "backend_unavailable":
    case "error":
      return { enabled: false, explanation: readiness.message };
    case "ready": {
      if (readiness.repositories.length === 0) {
        return {
          enabled: false,
          explanation:
            "No repositories are connected. Install the read-only PulseGraph GitHub App and select the repositories it may read.",
        };
      }
      const repository = repositoryForService(readiness, service);
      if (!repository) {
        return {
          enabled: false,
          explanation: `No repository is mapped to “${service}”. Map the service to one of the connected repositories before investigating.`,
        };
      }
      return {
        enabled: true,
        explanation: `Reads pinned source from ${repository.full_name}. PulseGraph cannot push, commit, branch, or open a pull request.`,
      };
    }
  }
}
