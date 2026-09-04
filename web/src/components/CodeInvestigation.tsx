"use client";

import { Panel } from "./OverviewPanels";
import { repositoryForService, type GithubReadiness } from "@/hooks/useGithubReadiness";
import { serviceOf } from "@/lib/theme";
import type { Incident } from "@/lib/types";

/**
 * What the read-only design actually guarantees, stated where an operator can
 * see it before they ask PulseGraph to read their source.
 */
const GUARANTEES = [
  {
    title: "Read-only GitHub App",
    body: "Metadata and Contents, read scope only, on the repositories you explicitly select. The app has no write permission to grant.",
  },
  {
    title: "Analysis is pinned to a commit",
    body: "A snapshot records an immutable commit, tree and blob inventory, so a diagnosis always refers to source that existed at one exact revision.",
  },
  {
    title: "Diagnoses cite bounded evidence",
    body: "Every claim points at a specific file and line range from that snapshot. Anything ungrounded is replaced by a stated fallback instead of a guess.",
  },
  {
    title: "Patches are previews, never commits",
    body: "A patch preview is a unified diff built in a temporary workspace and thrown away. PulseGraph cannot push, commit, branch, merge, or open a pull request.",
  },
];

function StatusNote({
  tone,
  title,
  body,
  action,
}: {
  tone: "ok" | "warn" | "crit" | "muted";
  title: string;
  body: string;
  action?: React.ReactNode;
}) {
  const styles = {
    ok: "border-[#BBF7D0] bg-[#F0FDF4] text-[#15803D]",
    warn: "border-[#FDE68A] bg-[#FFFBEB] text-[#B45309]",
    crit: "border-[#FECACA] bg-[#FEF2F2] text-[#B91C1C]",
    muted: "border-edge bg-panel-2 text-text-2",
  }[tone];

  return (
    <div className={`rounded-md border px-4 py-3 ${styles}`}>
      <p className="text-[13px] font-medium">{title}</p>
      <p className="mt-1 text-[12px] leading-relaxed opacity-90">{body}</p>
      {action && <div className="mt-2.5">{action}</div>}
    </div>
  );
}

function RetryButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-md border border-edge bg-panel px-3 py-1.5 text-[12px] font-medium text-text transition-colors hover:bg-panel-2"
    >
      Check again
    </button>
  );
}

function ConnectionState({
  readiness,
  onRetry,
}: {
  readiness: GithubReadiness;
  onRetry: () => void;
}) {
  switch (readiness.kind) {
    case "loading":
      return (
        <StatusNote tone="muted" title="Checking connection" body="Reading the connected repository list." />
      );
    case "unconfigured":
      return (
        <StatusNote
          tone="warn"
          title="Not configured"
          body={readiness.message}
          action={<RetryButton onClick={onRetry} />}
        />
      );
    case "unauthorized":
      return (
        <StatusNote
          tone="crit"
          title="Admin token rejected"
          body={readiness.message}
          action={<RetryButton onClick={onRetry} />}
        />
      );
    case "backend_unavailable":
      return (
        <StatusNote
          tone="crit"
          title="Backend unreachable"
          body={readiness.message}
          action={<RetryButton onClick={onRetry} />}
        />
      );
    case "error":
      return (
        <StatusNote
          tone="crit"
          title="Connection check failed"
          body={readiness.message}
          action={<RetryButton onClick={onRetry} />}
        />
      );
    case "ready":
      if (readiness.repositories.length === 0) {
        return (
          <StatusNote
            tone="warn"
            title="No repositories connected"
            body="The GitHub App is configured but no repositories are selected for it yet. Install it and choose the repositories PulseGraph may read."
            action={<RetryButton onClick={onRetry} />}
          />
        );
      }
      return (
        <StatusNote
          tone="ok"
          title="Connected"
          body={`${readiness.repositories.length} ${
            readiness.repositories.length === 1 ? "repository is" : "repositories are"
          } readable, and ${
            readiness.repositories.filter((repository) => repository.service).length
          } mapped to a monitored service.`}
        />
      );
  }
}

/**
 * The Code Investigation view.
 *
 * It deliberately shows connection state and the service-to-repository map
 * read-only — there are no controls here that install an app, change a
 * mapping, pin a snapshot, or run a diagnosis. Those screens are not built
 * yet, and a control that looks like it works but doesn't is worse than none.
 */
export function CodeInvestigation({
  readiness,
  onRetry,
  target,
}: {
  readiness: GithubReadiness;
  onRetry: () => void;
  /** The incident sent here by "Investigate code", if any. */
  target: Incident | null;
}) {
  const repositories = readiness.kind === "ready" ? readiness.repositories : [];
  const targetService = target ? serviceOf(target.title) : null;
  const targetRepository = targetService ? repositoryForService(readiness, targetService) : null;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-[20px] font-semibold tracking-tight text-text">Code Investigation</h1>
        <p className="mt-1 text-[13px] text-text-2">
          Connects an incident to the source that produced it, through a read-only GitHub App
          pinned to a specific commit.
        </p>
      </div>

      <Panel title="Connection" hint="Status of the read-only GitHub App for this dashboard">
        <ConnectionState readiness={readiness} onRetry={onRetry} />
      </Panel>

      <Panel
        title="Selected incident"
        hint="The incident sent here from the incident drawer"
      >
        {!target ? (
          <p className="text-[13px] text-text-2">
            No incident selected. Open an incident and choose{" "}
            <span className="font-medium text-text">Investigate code</span> to bring it here.
          </p>
        ) : (
          <dl className="divide-y divide-edge text-[13px]">
            <div className="flex flex-wrap justify-between gap-4 pb-2.5">
              <dt className="text-text-2">Incident</dt>
              <dd className="text-right font-medium text-text">{target.title}</dd>
            </div>
            <div className="flex flex-wrap justify-between gap-4 py-2.5">
              <dt className="text-text-2">Service</dt>
              <dd className="text-right font-mono text-text">{targetService}</dd>
            </div>
            <div className="flex flex-wrap justify-between gap-4 py-2.5">
              <dt className="text-text-2">Mapped repository</dt>
              <dd className="text-right font-mono text-text">
                {targetRepository ? (
                  targetRepository.full_name
                ) : (
                  <span className="font-sans text-[#B45309]">none</span>
                )}
              </dd>
            </div>
            <div className="flex flex-wrap justify-between gap-4 pt-2.5">
              <dt className="text-text-2">Incident ID</dt>
              <dd className="break-all text-right font-mono text-[12px] text-text-2">
                {target.incident_id}
              </dd>
            </div>
          </dl>
        )}

        {target && !targetRepository && readiness.kind === "ready" && (
          <div className="mt-4">
            <StatusNote
              tone="warn"
              title="No repository mapped"
              body={`Nothing is mapped to “${targetService}”, so there is no pinned source to read. Map the service to one of the connected repositories to investigate it.`}
            />
          </div>
        )}

        {target && targetRepository && (
          <div className="mt-4">
            <StatusNote
              tone="muted"
              title="Diagnosis is not wired up yet"
              body={`This incident is ready to investigate against ${targetRepository.full_name}. Running a bounded diagnosis and reviewing a patch preview is the next slice; the backend endpoints and the proxy routes for them already exist.`}
            />
          </div>
        )}
      </Panel>

      <Panel
        title="Connected repositories"
        hint="Read-only, and only the repositories selected on the installation"
      >
        {repositories.length === 0 ? (
          <p className="text-[13px] text-text-2">
            No repositories to show. This list is populated by the GitHub App installation.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] border-collapse text-left">
              <thead>
                <tr className="border-b border-edge text-[11px] uppercase tracking-wide text-text-3">
                  <th className="py-2.5 pr-3 font-medium">Repository</th>
                  <th className="px-3 py-2.5 font-medium">Mapped service</th>
                  <th className="px-3 py-2.5 font-medium">Default branch</th>
                  <th className="py-2.5 pl-3 font-medium">Installation</th>
                </tr>
              </thead>
              <tbody>
                {repositories.map((repository) => (
                  <tr key={repository.repository_id} className="border-b border-edge/70">
                    <td className="py-3 pr-3">
                      <p className="font-mono text-[13px] text-text">{repository.full_name}</p>
                      {repository.is_private ? (
                        <p className="mt-0.5 text-[11px] text-text-3">private</p>
                      ) : null}
                    </td>
                    <td className="px-3 py-3 text-[13px]">
                      {repository.service ? (
                        <span className="font-mono text-text">{repository.service}</span>
                      ) : (
                        <span className="text-text-3">unmapped</span>
                      )}
                    </td>
                    <td className="px-3 py-3 font-mono text-[13px] text-text-2">
                      {repository.default_branch}
                    </td>
                    <td className="py-3 pl-3 text-[13px] text-text-2">
                      {repository.account_login}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title="What PulseGraph can and cannot do" hint="The safety model, in full">
        <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {GUARANTEES.map((guarantee) => (
            <li key={guarantee.title} className="rounded-md border border-edge bg-panel-2 px-4 py-3">
              <p className="text-[13px] font-medium text-text">{guarantee.title}</p>
              <p className="mt-1 text-[12px] leading-relaxed text-text-2">{guarantee.body}</p>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
