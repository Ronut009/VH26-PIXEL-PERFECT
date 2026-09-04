import { createClient, supabaseConfigured } from "@/lib/supabase/server";
import { errorResponse } from "@/server/pulsegraph";
import type { DashboardUser } from "@/lib/types";

export type { DashboardUser };

if (typeof window !== "undefined") {
  // GITHUB_ALLOWED_LOGINS is read here. A stray client import would evaluate
  // it as undefined and quietly allow nobody (or, worse, be mistaken for a
  // client-readable value), so fail loudly instead.
  throw new Error("@/server/auth is server-only and must not be imported by client code");
}


export type AuthState =
  | { status: "signed-out" }
  | { status: "not-allowed"; user: DashboardUser }
  | { status: "unconfigured" }
  | { status: "ok"; user: DashboardUser };

/**
 * Allowlisted GitHub usernames, comma separated, compared case-insensitively.
 *
 * An unset or empty value denies everyone rather than admitting everyone:
 * a missing env var must never be the thing that opens the dashboard up.
 */
function allowedLogins(): string[] {
  return (process.env.GITHUB_ALLOWED_LOGINS ?? "")
    .split(",")
    .map((entry) => entry.trim().toLowerCase())
    .filter(Boolean);
}

export function isAllowed(login: string): boolean {
  const allowed = allowedLogins();
  if (allowed.length === 0) return false;
  return allowed.includes(login.trim().toLowerCase());
}

/** True when no allowlist is configured, so the UI can explain the cause. */
export function allowlistConfigured(): boolean {
  return allowedLogins().length > 0;
}

/**
 * Resolve the current dashboard user.
 *
 * Uses `getUser()`, not `getSession()`: `getUser()` revalidates the token with
 * Supabase, whereas `getSession()` trusts whatever is in the cookie. For an
 * authorization decision the cookie alone is not good enough.
 */
export async function getAuthState(): Promise<AuthState> {
  if (!supabaseConfigured()) return { status: "unconfigured" };

  const supabase = await createClient();
  const { data, error } = await supabase.auth.getUser();
  if (error || !data.user) return { status: "signed-out" };

  const metadata = data.user.user_metadata ?? {};
  // The GitHub provider spells the username `user_name`; some payloads carry
  // `preferred_username` instead. Read both before giving up.
  const login =
    (typeof metadata.user_name === "string" && metadata.user_name) ||
    (typeof metadata.preferred_username === "string" && metadata.preferred_username) ||
    "";

  const user: DashboardUser = {
    id: data.user.id,
    login,
    name: typeof metadata.full_name === "string" ? metadata.full_name : null,
    avatarUrl: typeof metadata.avatar_url === "string" ? metadata.avatar_url : null,
    email: data.user.email ?? null,
  };

  if (!login || !isAllowed(login)) return { status: "not-allowed", user };
  return { status: "ok", user };
}

/** Convenience for server components: the user, or null if not admitted. */
export async function getDashboardUser(): Promise<DashboardUser | null> {
  const state = await getAuthState();
  return state.status === "ok" ? state.user : null;
}

/**
 * Route-handler guard. Returns `null` when the caller may proceed, otherwise a
 * ready-to-return error Response in the same shape every other API failure
 * uses, so the client can switch on `error.code`.
 */
export async function requireDashboardUser(): Promise<Response | null> {
  const state = await getAuthState();

  if (state.status === "ok") return null;

  if (state.status === "unconfigured") {
    return errorResponse(
      503,
      "auth_unconfigured",
      "Sign-in is not configured. Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY in web/.env.local.",
    );
  }

  if (state.status === "not-allowed") {
    return errorResponse(
      403,
      "forbidden",
      "This GitHub account is not on the dashboard allowlist.",
    );
  }

  return errorResponse(401, "unauthorized", "Sign in with GitHub to use PulseGraph.");
}
