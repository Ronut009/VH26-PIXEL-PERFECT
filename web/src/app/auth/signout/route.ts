import { NextResponse, type NextRequest } from "next/server";
import { createClient, supabaseConfigured } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

/**
 * Sign out. POST only, so a stray link prefetch or an image tag cannot end
 * someone's session.
 */
export async function POST(request: NextRequest): Promise<Response> {
  if (supabaseConfigured()) {
    const supabase = await createClient();
    await supabase.auth.signOut();
  }
  return NextResponse.redirect(`${request.nextUrl.origin}/login`, { status: 303 });
}
