import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

export async function updateSession(request: NextRequest) {
  const supabaseUrl = process.env.SUPABASE_URL;
  const supabaseAnonKey = process.env.SUPABASE_ANON_KEY;

  const path = request.nextUrl.pathname;
  const isAuthRoute = path === "/login" || path.startsWith("/auth");

  // API routes authenticate themselves (cookie session OR API key) via
  // resolveAuth in each handler. A bearer-token request carries no session
  // cookie, so middleware must not gate /api — otherwise it would redirect to
  // /login before the handler runs.
  if (path.startsWith("/api/")) {
    return NextResponse.next({ request });
  }

  // Fail open if env vars aren't configured yet (fresh deploy without secrets).
  if (!supabaseUrl || !supabaseAnonKey) {
    return NextResponse.next({ request });
  }

  try {
    let supabaseResponse = NextResponse.next({ request });

    const supabase = createServerClient(supabaseUrl, supabaseAnonKey, {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value }) =>
            request.cookies.set(name, value),
          );
          supabaseResponse = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) =>
            supabaseResponse.cookies.set(name, value, options),
          );
        },
      },
    });

    // Do not run code between createServerClient and getUser — it refreshes
    // the session cookie and must be the first auth call.
    const {
      data: { user },
    } = await supabase.auth.getUser();

    // Auth routes are always reachable so a blocked user can re-authenticate.
    if (isAuthRoute) {
      if (user && user.email === process.env.ALLOWED_EMAIL && path === "/login") {
        const url = request.nextUrl.clone();
        url.pathname = "/";
        return NextResponse.redirect(url);
      }
      return supabaseResponse;
    }

    if (!user) {
      const url = request.nextUrl.clone();
      url.pathname = "/login";
      return NextResponse.redirect(url);
    }

    // Single-user allowlist: any other authenticated email is forbidden.
    if (user.email !== process.env.ALLOWED_EMAIL) {
      return new NextResponse("Forbidden", { status: 403 });
    }

    return supabaseResponse;
  } catch {
    // Auth routes must never be redirected — that's what caused the loop.
    if (isAuthRoute) return NextResponse.next({ request });
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }
}
