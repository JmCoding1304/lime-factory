/**
 * Audit C1: the dashboard binds loopback only and gates every request on a
 * local Host (defense against DNS rebinding) plus, for writes, a local Origin.
 * No token or cookie by design.
 */
import type { ApiError } from "../shared/types.ts";

/**
 * Mirrors `vite.config.ts` `server.port`. The Vite dev proxy forwards the
 * browser's `Origin: http://localhost:4601` unchanged, so it must be allowed.
 */
export const UI_PORT = 4601;

const LOCAL_HOSTS = ["localhost", "127.0.0.1", "[::1]"];

/** host:port, strictly — no userinfo, path or trailing text. */
const AUTHORITY = /^(localhost|127\.0\.0\.1|\[::1\]):(\d{1,5})$/;

export function isLocalAuthority(
  authority: string | null,
  ports: readonly number[],
): boolean {
  if (authority === null) return false;
  const match = AUTHORITY.exec(authority.toLowerCase());
  if (!match) return false;
  if (!LOCAL_HOSTS.includes(match[1])) return false;
  return ports.includes(Number(match[2]));
}

export function isLocalOrigin(
  origin: string,
  ports: readonly number[],
): boolean {
  const lower = origin.toLowerCase();
  let rest: string;
  if (lower.startsWith("http://")) rest = origin.slice("http://".length);
  else if (lower.startsWith("https://")) rest = origin.slice("https://".length);
  else return false;
  return isLocalAuthority(rest, ports);
}

/**
 * A request from anything but this Mac gets a 403; otherwise null.
 *
 * With `originOnGet`, a present non-local Origin is refused on GET/HEAD too;
 * without it the read methods skip the Origin check as before.
 */
export function refuseUnlessLocal(
  req: Request,
  ports: readonly number[],
  opts: { originOnGet?: boolean } = {},
): Response | null {
  if (!isLocalAuthority(req.headers.get("host"), ports)) {
    return new Response(
      JSON.stringify({
        error: "host not allowed: the dashboard answers only this Mac",
      } satisfies ApiError),
      {
        status: 403,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "no-store",
        },
      },
    );
  }

  const origin = req.headers.get("origin");
  const isRead = req.method === "GET" || req.method === "HEAD";
  if (origin !== null && (!isRead || opts.originOnGet === true) && !isLocalOrigin(origin, ports)) {
    return new Response(
      JSON.stringify({
        error: "origin not allowed: the dashboard answers only this Mac",
      } satisfies ApiError),
      {
        status: 403,
        headers: {
          "content-type": "application/json; charset=utf-8",
          "cache-control": "no-store",
        },
      },
    );
  }

  return null;
}
