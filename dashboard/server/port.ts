/**
 * The dashboard API port — one rule for server/index.ts and vite.config.ts:
 * PORT, else LIME_PORT (lime sets it inside a Run, so a worktree never binds
 * the owner's port), else OWNER_PORT. An empty value counts as unset: a Run
 * with no port gets LIME_PORT="" (lime/cli.py _run_env). `lime dash` applies
 * the same rule in lime/cli.py:_dash_port and passes the result as PORT.
 */
export const OWNER_PORT = 4600;

export function apiPort(env: Record<string, string | undefined> = process.env): number {
  const sources = [["PORT", env.PORT], ["LIME_PORT", env.LIME_PORT]] as const;
  for (const [name, value] of sources) {
    const raw = value?.trim();
    if (!raw) continue;
    const port = Number(raw);
    if (!/^\d+$/.test(raw) || port < 1 || port > 65535) {
      throw new Error(`${name}="${raw}" is not a port (1-65535)`);
    }
    return port;
  }
  return OWNER_PORT;
}
