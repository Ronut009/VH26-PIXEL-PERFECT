import { redirect } from "next/navigation";
import { SignInButton } from "@/components/auth/SignInButton";
import { allowlistConfigured, getAuthState } from "@/server/auth";

export const dynamic = "force-dynamic";

function Wordmark() {
  return (
    <div className="flex items-center gap-2">
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden>
        <path
          d="M1 10h3.2l2-5.2 3 11L12 8.2l1.4 1.8H19"
          stroke="#2563EB"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span className="text-[15px] font-semibold tracking-tight text-text">PulseGraph</span>
    </div>
  );
}

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const state = await getAuthState();
  if (state.status === "ok") redirect("/");

  const { error } = await searchParams;

  return (
    <main className="flex min-h-dvh items-center justify-center bg-app px-6 py-12">
      <div className="w-full max-w-sm">
        <Wordmark />

        <h1 className="mt-6 text-[20px] font-semibold tracking-tight text-text">
          Sign in to the incident console
        </h1>
        <p className="mt-1.5 text-[13px] text-text-2">
          PulseGraph collapses duplicate alerts, correlates incidents, and pages a human only when
          it matters. Access is limited to approved GitHub accounts.
        </p>

        <div className="mt-6 rounded-lg border border-edge bg-panel p-5">
          {state.status === "unconfigured" ? (
            <div>
              <p className="text-[13px] font-medium text-text">Sign-in is not configured</p>
              <p className="mt-1.5 text-[12px] text-text-2">
                Set <span className="font-mono">NEXT_PUBLIC_SUPABASE_URL</span> and{" "}
                <span className="font-mono">NEXT_PUBLIC_SUPABASE_ANON_KEY</span> in{" "}
                <span className="font-mono">web/.env.local</span>, then restart the dev server. See{" "}
                <span className="font-mono">web/README.md</span> for the Supabase and GitHub OAuth
                setup steps.
              </p>
            </div>
          ) : state.status === "not-allowed" ? (
            <div>
              <p className="text-[13px] font-medium text-text">This account cannot open PulseGraph</p>
              <p className="mt-1.5 text-[12px] text-text-2">
                Signed in as{" "}
                <span className="font-mono text-text">
                  {state.user.login || state.user.email || state.user.id}
                </span>
                {allowlistConfigured()
                  ? ", which is not on the dashboard allowlist."
                  : ". No allowlist is configured, so nobody is admitted yet. Add GITHUB_ALLOWED_LOGINS to web/.env.local."}
              </p>
              <form action="/auth/signout" method="post" className="mt-4">
                <button
                  type="submit"
                  className="w-full rounded-md border border-edge bg-panel px-4 py-2 text-[13px] font-medium text-text-2 transition-colors hover:bg-panel-2 hover:text-text"
                >
                  Sign out and try another account
                </button>
              </form>
            </div>
          ) : (
            <SignInButton />
          )}

          {error && state.status === "signed-out" && (
            <p className="mt-3 rounded-md border border-edge bg-[#FEF2F2] px-3 py-2 text-[12px] text-[#B91C1C]">
              {error}
            </p>
          )}
        </div>

        <p className="mt-4 text-[12px] text-text-3">
          PulseGraph only ever reads from GitHub. It cannot push, commit, or open pull requests.
        </p>
      </div>
    </main>
  );
}
