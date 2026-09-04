import { NextResponse, type NextRequest } from "next/server";
import { createServerClient } from "@supabase/ssr";

/**
 * Session refresh and the unauthenticated redirect.
 *
 * This is `proxy.ts`, not `middleware.ts`: Next.js 16 renamed the convention
 * and the exported function (see the v16 upgrade guide). The runtime is
 * Node.js and cannot be configured here.
 *
 * Note this is deliberately NOT the only place auth is enforced. The Next
 * docs warn that a matcher change can silently remove proxy coverage, so every
 * route handler calls `requireDashboardUser()` itself and `/` re-checks on the
 * server. This layer exists to keep the Supabase cookie fresh and to send
 * signed-out humans to the login screen instead of a broken dashboard.
 */
export async function proxy(request: NextRequest) {
  const response = NextResponse.next({ request });

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  // Without Supabase configured there is no session to refresh. Let the
  // request through so the login screen can explain what is missing rather
  // than bouncing the user around a redirect loop.
  if (!url || !key) return response;

  const supabase = createServerClient(url, key, {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        for (const { name, value } of cookiesToSet) {
          request.cookies.set(name, value);
        }
        for (const { name, value, options } of cookiesToSet) {
          response.cookies.set(name, value, options);
        }
      },
    },
  });

  // Revalidates the token and rotates the cookie when it is close to expiry.
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const { pathname } = request.nextUrl;
  const isAuthRoute = pathname.startsWith("/auth");
  const isLogin = pathname === "/login";
  const isApi = pathname.startsWith("/api");

  if (!user && !isAuthRoute && !isLogin && !isApi) {
    const target = request.nextUrl.clone();
    target.pathname = "/login";
    target.search = "";
    return NextResponse.redirect(target);
  }

  return response;
}

export const config = {
  // Everything except Next's own assets and the favicon. Without a negative
  // match the redirect above would also intercept CSS, JS and images.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
