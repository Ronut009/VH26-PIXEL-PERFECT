"use client";

import { useId, useState } from "react";
import {
  createRepositorySnapshot,
  fetchGithubInstallUrl,
  fetchGithubSnapshot,
  mapServiceToRepository,
  PulseGraphApiError,
  syncGithubInstallation,
} from "@/lib/api";
import type { GithubRepository, GithubSnapshot } from "@/lib/types";

/**
 * Whether the pinned commit is behind the branch.
 *
 * Only answerable once a sync has recorded a head; until then the honest
 * answer is "unknown", and the row says nothing rather than implying the pin
 * is current.
 */
function behindHead(repository: GithubRepository): boolean {
  return (
    repository.head_commit_sha !== null &&
    repository.pinned_commit_sha !== null &&
    repository.head_commit_sha !== repository.pinned_commit_sha
  );
}

/** When a snapshot was pinned, in the viewer's locale, date included. */
function shortTime(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

function message(error: unknown, fallback: string): string {
  return error instanceof PulseGraphApiError ? error.message : fallback;
}

function SmallButton({
  onClick,
  disabled,
  children,
  tone = "default",
}: {
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
  tone?: "default" | "primary";
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`rounded-md px-2.5 py-1 text-[12px] font-medium transition-colors disabled:cursor-wait disabled:opacity-60 ${
        tone === "primary"
          ? "bg-brand text-[#10120F] hover:bg-[#D8FF66]"
          : "border border-edge bg-panel text-text hover:bg-panel-2"
      }`}
    >
      {children}
    </button>
  );
}

/** Lazily fetch the App's install URL, so an unconfigured slug isn't probed on every load. */
function InstallLink() {
  const [url, setUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setBusy(true);
    setError(null);
    try {
      setUrl(await fetchGithubInstallUrl());
    } catch (caught) {
      setError(message(caught, "The install URL could not be read."));
    } finally {
      setBusy(false);
    }
  };

  if (url) {
    return (
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        className="rounded-md border border-edge bg-panel px-2.5 py-1 text-[12px] font-medium text-brand transition-colors hover:bg-panel-2"
      >
        Open GitHub install page ↗
      </a>
    );
  }

  return (
    <span className="flex items-center gap-2">
      <SmallButton onClick={load} disabled={busy}>
        {busy ? "Loading…" : "Get install link"}
      </SmallButton>
      {error && (
        <span role="alert" className="text-[12px] text-[#B45309]">
          {error}
        </span>
      )}
    </span>
  );
}

/**
 * Connect repositories to services, and pin the commit an analysis will read.
 *
 * Every action here is a read of GitHub plus a write to PulseGraph's own
 * database. Nothing pushes, commits, branches, merges, or opens a pull
 * request — the App holds read scope only, so those are not capabilities it
 * could be asked for.
 */
export function RepositoryManager({
  repositories,
  serviceSuggestions,
  onChanged,
}: {
  repositories: GithubRepository[];
  /** Service names seen on incidents, offered as completions. */
  serviceSuggestions: string[];
  /** Re-read the repository list after a mapping or sync changes it. */
  onChanged: () => void;
}) {
  const listId = useId();
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [results, setResults] = useState<Record<string, string | null>>({});
  const [snapshots, setSnapshots] = useState<Record<number, GithubSnapshot>>({});
  const [mappingFor, setMappingFor] = useState<number | null>(null);
  const [serviceDraft, setServiceDraft] = useState("");

  const installations = Array.from(new Set(repositories.map((r) => r.installation_id)));

  const setError = (key: string, value: string | null) =>
    setErrors((previous) => ({ ...previous, [key]: value }));

  const setResult = (key: string, value: string | null) =>
    setResults((previous) => ({ ...previous, [key]: value }));

  const sync = async (installationId: number) => {
    const key = `sync:${installationId}`;
    setBusyKey(key);
    setError(key, null);
    setResult(key, null);
    try {
      const before = repositories.filter((r) => r.installation_id === installationId).length;
      const synced = await syncGithubInstallation(installationId);
      const after = synced.repository_ids.length;
      // A successful sync that changes nothing used to render exactly like a
      // dead button: a moment of "Refreshing...", then the same screen. Most
      // of the time nothing *has* changed, so saying so is the common case,
      // not the edge case.
      setResult(
        key,
        after === before
          ? `No change — ${plural(after, "repository", "repositories")}.`
          : `Now ${plural(after, "repository", "repositories")}, was ${before}.`,
      );
      onChanged();
    } catch (caught) {
      setError(key, message(caught, "The repository list could not be refreshed."));
    } finally {
      setBusyKey(null);
    }
  };

  const saveMapping = async (repositoryId: number) => {
    const key = `map:${repositoryId}`;
    const service = serviceDraft.trim();
    if (!service) {
      setError(key, "Enter the service name exactly as it appears on incidents.");
      return;
    }
    setBusyKey(key);
    setError(key, null);
    try {
      await mapServiceToRepository(service, repositoryId);
      setMappingFor(null);
      setServiceDraft("");
      onChanged();
    } catch (caught) {
      setError(key, message(caught, "The service mapping could not be saved."));
    } finally {
      setBusyKey(null);
    }
  };

  const pinSnapshot = async (repositoryId: number) => {
    const key = `snap:${repositoryId}`;
    setBusyKey(key);
    setError(key, null);
    try {
      const created = await createRepositorySnapshot(repositoryId);
      // Re-read with the file inventory so the pin is inspectable, not just
      // a commit SHA the operator has to take on trust.
      const detailed = await fetchGithubSnapshot(created.snapshot_id, {
        includeFiles: true,
        fileLimit: 200,
      });
      setSnapshots((previous) => ({ ...previous, [repositoryId]: detailed }));
    } catch (caught) {
      setError(key, message(caught, "The snapshot could not be pinned."));
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <div className="space-y-4">
      <datalist id={listId}>
        {serviceSuggestions.map((service) => (
          <option key={service} value={service} />
        ))}
      </datalist>

      <div className="flex flex-wrap items-center gap-2">
        <InstallLink />
        {installations.map((installationId) => {
          const key = `sync:${installationId}`;
          return (
            <span key={installationId} className="flex items-center gap-2">
              <SmallButton onClick={() => sync(installationId)} disabled={busyKey === key}>
                {busyKey === key ? "Refreshing…" : `Refresh installation ${installationId}`}
              </SmallButton>
              {errors[key] && (
                <span role="alert" className="text-[12px] text-[#B91C1C]">
                  {errors[key]}
                </span>
              )}
              {!errors[key] && results[key] && (
                <span role="status" className="text-[12px] text-text-2">
                  {results[key]}
                </span>
              )}
            </span>
          );
        })}
      </div>

      <ul className="space-y-2">
        {repositories.map((repository) => {
          const mapKey = `map:${repository.repository_id}`;
          const snapKey = `snap:${repository.repository_id}`;
          const snapshot = snapshots[repository.repository_id];
          const isMapping = mappingFor === repository.repository_id;

          return (
            <li
              key={repository.repository_id}
              className="rounded-md border border-edge bg-panel-2 px-4 py-3"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-mono text-[13px] text-text">{repository.full_name}</p>
                  <p className="mt-0.5 text-[12px] text-text-2">
                    {repository.services.length > 0 ? (
                      <>
                        mapped to{" "}
                        <span className="font-mono text-text">
                          {repository.services.join(", ")}
                        </span>
                      </>
                    ) : (
                      <span className="text-text-3">no service mapped</span>
                    )}
                    <span className="text-text-3">
                      {" "}
                      · {repository.default_branch} · {repository.account_login}
                      {repository.is_private ? " · private" : ""}
                    </span>
                  </p>
                  <p className="mt-0.5 text-[12px] text-text-3">
                    {repository.pinned_commit_sha ? (
                      <>
                        reads pinned commit{" "}
                        <span className="font-mono text-text-2">
                          {repository.pinned_commit_sha.slice(0, 7)}
                        </span>
                        {repository.pinned_file_count !== null
                          ? ` · ${repository.pinned_file_count.toLocaleString()} files`
                          : ""}
                        {repository.pinned_at ? ` · pinned ${shortTime(repository.pinned_at)}` : ""}
                      </>
                    ) : (
                      "no snapshot pinned — an analysis has no source to read until you pin one"
                    )}
                  </p>
                  {behindHead(repository) && (
                    <p role="status" className="mt-0.5 text-[12px] text-[#B45309]">
                      {repository.default_branch} has moved to{" "}
                      <span className="font-mono">
                        {repository.head_commit_sha!.slice(0, 7)}
                      </span>{" "}
                      — pin a snapshot to read the newer code
                    </p>
                  )}
                </div>

                <div className="flex shrink-0 flex-wrap items-center gap-2">
                  <SmallButton
                    onClick={() => {
                      setMappingFor(isMapping ? null : repository.repository_id);
                      // Blank, not prefilled: mapping is additive, so
                      // prefilling an existing name invites overwriting a
                      // mapping when the intent is usually to add another.
                      setServiceDraft("");
                      setError(mapKey, null);
                    }}
                  >
                    {repository.services.length > 0 ? "Map another service" : "Map service"}
                  </SmallButton>
                  <SmallButton
                    onClick={() => pinSnapshot(repository.repository_id)}
                    disabled={busyKey === snapKey}
                    tone="primary"
                  >
                    {busyKey === snapKey ? "Pinning…" : "Pin snapshot"}
                  </SmallButton>
                </div>
              </div>

              {isMapping && (
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    saveMapping(repository.repository_id);
                  }}
                  className="mt-3 flex flex-wrap items-end gap-2"
                >
                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] font-medium uppercase tracking-wide text-text-3">
                      Service name
                    </span>
                    <input
                      autoFocus
                      list={listId}
                      value={serviceDraft}
                      onChange={(event) => setServiceDraft(event.target.value)}
                      placeholder="payment-api"
                      className="w-56 rounded-md border border-edge bg-panel px-2.5 py-1.5 font-mono text-[13px] text-text placeholder:text-text-3 focus:border-brand focus:outline-none"
                    />
                  </label>
                  <SmallButton
                    onClick={() => saveMapping(repository.repository_id)}
                    disabled={busyKey === mapKey}
                    tone="primary"
                  >
                    {busyKey === mapKey ? "Saving…" : "Save"}
                  </SmallButton>
                  <SmallButton onClick={() => setMappingFor(null)}>Cancel</SmallButton>
                  <p className="w-full text-[12px] text-text-2">
                    Must match the service on incident titles exactly — PulseGraph looks the
                    mapping up by that name.
                  </p>
                </form>
              )}

              {errors[mapKey] && (
                <p role="alert" className="mt-2 text-[12px] text-[#B91C1C]">
                  {errors[mapKey]}
                </p>
              )}
              {errors[snapKey] && (
                <p role="alert" className="mt-2 text-[12px] text-[#B91C1C]">
                  {errors[snapKey]}
                </p>
              )}

              {snapshot && (
                <div className="mt-3 rounded border border-edge bg-panel px-3 py-2">
                  <p className="text-[12px] text-text-2">
                    Pinned <span className="font-mono text-text">{snapshot.ref}</span> at{" "}
                    <span className="font-mono text-text">
                      {snapshot.commit_sha.slice(0, 10)}
                    </span>{" "}
                    · {snapshot.file_count.toLocaleString()} files
                    {snapshot.tree_truncated ? " · tree truncated by the entry limit" : ""}
                  </p>
                  {snapshot.files && snapshot.files.length > 0 && (
                    <details className="mt-1.5">
                      <summary className="cursor-pointer text-[12px] text-brand">
                        Show inventory ({snapshot.files.length} shown)
                      </summary>
                      <ul className="mt-1.5 max-h-56 overflow-y-auto font-mono text-[12px] text-text-2">
                        {snapshot.files.map((file) => (
                          <li key={file.path} className="flex justify-between gap-4 py-0.5">
                            <span className="truncate">{file.path}</span>
                            <span className="shrink-0 tabular-nums text-text-3">
                              {file.size_bytes?.toLocaleString() ?? "—"}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
