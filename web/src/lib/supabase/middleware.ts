import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

export async function updateSession(request: NextRequest) {
  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  // Fail open if env vars aren't configured yet (fresh deploy without secrets).
  // Requests still reach the app; auth is enforced once the vars are present.
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

    const path = request.nextUrl.pathname;
    const isAuthRoute = path === "/login" || path.startsWith("/auth");

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
    // If Supabase is unreachable or throws, redirect to login rather than 500.
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }
}
