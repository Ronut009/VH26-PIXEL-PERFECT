import { NextResponse, type NextRequest } from "next/server";
import { createClient, supabaseConfigured } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

/**
 * OAuth landing point. GitHub redirects to Supabase, Supabase redirects here
 * with a PKCE `code`, and this exchanges it for a session cookie.
 */
export async function GET(request: NextRequest): Promise<Response> {
  const { searchParams, origin } = request.nextUrl;

  const failure = (reason: string) =>
    NextResponse.redirect(`${origin}/login?error=${encodeURIComponent(reason)}`);

  // Supabase reports provider-side failures (user cancelled, app misconfigured)
  // as query params rather than an HTTP error.
  const providerError = searchParams.get("error_description") ?? searchParams.get("error");
  if (providerError) return failure(providerError);

  if (!supabaseConfigured()) return failure("Sign-in is not configured on this server.");

  const code = searchParams.get("code");
  if (!code) return failure("The sign-in link was missing its authorization code.");

  const supabase = await createClient();
  const { error } = await supabase.auth.exchangeCodeForSession(code);
  if (error) return failure(error.message);

  return NextResponse.redirect(`${origin}/`);
}
