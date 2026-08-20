// Vercel Edge Middleware: HTTP Basic auth gate on every route, including data.json.
// Fails closed: if DASH_PASS is not set, all requests are denied. This keeps the
// sensitive SLA data behind a password no matter what.
export const config = { matcher: "/:path*" };

export default function middleware(request) {
  const USER = process.env.DASH_USER || "netflix";
  const PASS = process.env.DASH_PASS;
  const auth = request.headers.get("authorization");

  if (PASS && auth) {
    const [scheme, encoded] = auth.split(" ");
    if (scheme === "Basic" && encoded) {
      const decoded = atob(encoded);
      const i = decoded.indexOf(":");
      const u = decoded.slice(0, i);
      const p = decoded.slice(i + 1);
      if (u === USER && p === PASS) {
        return; // authorized, continue to the static file
      }
    }
  }
  return new Response("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Netflix MS SLA", charset="UTF-8"' },
  });
}
