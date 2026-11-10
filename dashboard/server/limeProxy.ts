/**
 * The home proxies: spawn `lime` like profiles.ts; never read or write the
 * dashboard home under `~/.lime` directly.
 *
 * `lime status --json`, `lime focus list --json`, `lime dash visit` and the
 * four owner write commands (`lime focus done`, `lime focus edit`, `lime ticket
 * close`, `lime ticket reopen`) are all owned by the Python CLI — this module
 * only shells out and parses stdout. Each spawn races its own timeout, so a
 * slow `lime` (gh calls inside status can take
 * 20 s, a git fetch up to 30 s) is killed and answered without waiting for its
 * pipes. A failure yields one line for that spawn; the other spawn's data still
 * rides along. Nothing here ever retries, and `dash visit` is idempotent because
 * lime itself records the visit only after its 30-min gap.
 */
import { LIME_BIN } from "./profiles.ts";
import type {
  FocusGoal,
  HomeFocusResponse,
  HomeTickRequest,
  HomeTickResponse,
  HomeVisitResponse,
  StatusReport,
} from "../shared/types.ts";

/** `lime status --json` is allowed the longest: it can gh+git fetch in-flight. */
export const STATUS_TIMEOUT_MS = 60_000;

/** `focus list` and `dash visit` share this shorter ceiling. */
export const SPAWN_TIMEOUT_MS = 30_000;

/**
 * `focus summarize` runs one model call per pending item, and lime caps each
 * item at 120 s, so the ceiling is generous.
 */
export const SUMMARIZE_TIMEOUT_MS = 600_000;

export interface ProxyOpts {
  /** Override the lime binary (tests pass a fake). */
  bin?: string;
  /** Value for LIME_PROJECTS so lime sees the same registry as the server. */
  registry?: string;
}

export interface LimeRun {
  exitCode: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
  spawnError: string | null;
}

/** First non-empty line of a block of text, trimmed; null when there is none. */
function firstNonEmptyLine(text: string): string | null {
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (trimmed) return trimmed;
  }
  return null;
}

type Settled =
  | { kind: "done"; stdout: string; stderr: string; exitCode: number }
  | { kind: "error"; message: string };

/**
 * Run `lime <args…>` with its own timeout. A spawn throw, a rejected `exited`
 * or a timeout all resolve to a `LimeRun`; the caller never sees a throw and
 * never blocks on a killed child's still-open pipes.
 */
export async function runLime(
  args: string[],
  timeoutMs: number,
  opts: ProxyOpts = {},
): Promise<LimeRun> {
  let proc: Bun.Subprocess<"ignore", "pipe", "pipe">;
  try {
    proc = Bun.spawn([opts.bin ?? LIME_BIN, ...args], {
      stdin: "ignore",
      stdout: "pipe",
      stderr: "pipe",
      env: {
        ...process.env,
        ...(opts.registry ? { LIME_PROJECTS: opts.registry } : {}),
      },
    });
  } catch (error) {
    return {
      exitCode: null,
      stdout: "",
      stderr: "",
      timedOut: false,
      spawnError: (error as Error).message,
    };
  }

  // Never leave an unhandled rejection behind: a killed child's pipes can
  // reject after the timeout already won the race.
  const run: Promise<Settled> = Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ]).then(
    ([stdout, stderr, exitCode]): Settled => ({
      kind: "done",
      stdout,
      stderr,
      exitCode,
    }),
    (error): Settled => ({ kind: "error", message: (error as Error).message }),
  );

  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<{ kind: "timeout" }>((resolve) => {
    timer = setTimeout(() => resolve({ kind: "timeout" }), timeoutMs);
  });

  try {
    const settled = await Promise.race([run, timeout]);
    if (settled.kind === "timeout") {
      proc.kill();
      return {
        exitCode: null,
        stdout: "",
        stderr: "",
        timedOut: true,
        spawnError: null,
      };
    }
    if (settled.kind === "error") {
      return {
        exitCode: null,
        stdout: "",
        stderr: "",
        timedOut: false,
        spawnError: settled.message,
      };
    }
    return {
      exitCode: settled.exitCode,
      stdout: settled.stdout,
      stderr: settled.stderr,
      timedOut: false,
      spawnError: null,
    };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Exactly one human line for a failed run, or null on exit 0.
 * `label` is the command as typed, e.g. `lime status --json`.
 */
export function failureLine(
  label: string,
  run: LimeRun,
  timeoutMs: number,
): string | null {
  if (run.spawnError) return `${label} could not start: ${run.spawnError}`;
  if (run.timedOut) return `${label} timed out after ${timeoutMs / 1000} s`;
  if (run.exitCode === 0) return null;
  const base = `${label} failed (exit ${run.exitCode})`;
  const first = firstNonEmptyLine(run.stderr);
  return first ? `${base}: ${first}` : base;
}

/** `lime dash visit` — records a visit and reports where the home repo is. */
export async function homeVisit(
  opts: ProxyOpts & { timeoutMs?: number } = {},
): Promise<HomeVisitResponse> {
  const timeoutMs = opts.timeoutMs ?? SPAWN_TIMEOUT_MS;
  const run = await runLime(["dash", "visit"], timeoutMs, opts);
  if (run.exitCode === 0) {
    return { ok: true, home_missing: false, message: null };
  }
  if (run.stderr.includes("run lime dash init")) {
    return {
      ok: false,
      home_missing: true,
      message: firstNonEmptyLine(run.stderr),
    };
  }
  return {
    ok: false,
    home_missing: false,
    message: failureLine("lime dash visit", run, timeoutMs),
  };
}

/** Valid only when stdout is an object with an array `items` and an object `home`. */
function asStatusReport(stdout: string): StatusReport | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return null;
  }
  const d = parsed as Record<string, unknown>;
  if (!Array.isArray(d.items)) return null;
  if (typeof d.home !== "object" || d.home === null || Array.isArray(d.home)) {
    return null;
  }
  return parsed as StatusReport;
}

/** Valid only when stdout parses to an array. */
function asGoals(stdout: string): FocusGoal[] | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    return null;
  }
  return Array.isArray(parsed) ? (parsed as FocusGoal[]) : null;
}

/**
 * `lime status --json` and `lime focus list --json` in parallel. Each spawn has
 * its own timeout; a bad spawn nulls only its own half and adds exactly one
 * error line, status first.
 */
export async function homeFocus(
  opts: ProxyOpts & { statusTimeoutMs?: number; focusTimeoutMs?: number } = {},
): Promise<HomeFocusResponse> {
  const statusTimeoutMs = opts.statusTimeoutMs ?? STATUS_TIMEOUT_MS;
  const focusTimeoutMs = opts.focusTimeoutMs ?? SPAWN_TIMEOUT_MS;
  const [statusRun, focusRun] = await Promise.all([
    runLime(["status", "--json"], statusTimeoutMs, opts),
    runLime(["focus", "list", "--json"], focusTimeoutMs, opts),
  ]);

  const errors: string[] = [];

  let report: StatusReport | null = null;
  if (statusRun.exitCode === 0) {
    report = asStatusReport(statusRun.stdout);
    if (!report) errors.push("lime status --json printed no JSON");
  } else {
    const line = failureLine("lime status --json", statusRun, statusTimeoutMs);
    if (line) errors.push(line);
  }

  let goals: FocusGoal[] | null = null;
  if (focusRun.exitCode === 0) {
    goals = asGoals(focusRun.stdout);
    if (!goals) errors.push("lime focus list --json printed no JSON");
  } else {
    const line = failureLine("lime focus list --json", focusRun, focusTimeoutMs);
    if (line) errors.push(line);
  }

  return { report, goals, errors };
}

const GOAL_ID = /^f\d+$/;
const PROJECT_NAME = /^[A-Za-z0-9_][A-Za-z0-9._-]*$/;
const TICKET_NUMBER = /^\d+$/;

/**
 * Map a tick body to lime args, or null when the shape is not tickable. A
 * goal's `done` toggles `focus done` / `focus edit --reopen`; a ticket's `done`
 * toggles `ticket close` / `ticket reopen`. Every field is validated here, so a
 * hostile body can never reach the spawn.
 */
export function tickArgs(body: unknown): string[] | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return null;
  }
  const b = body as Record<string, unknown>;
  if (typeof b.done !== "boolean") return null;
  if (b.kind === "goal") {
    if (typeof b.id !== "string" || !GOAL_ID.test(b.id)) return null;
    return b.done ? ["focus", "done", b.id] : ["focus", "edit", b.id, "--reopen"];
  }
  if (b.kind === "ticket") {
    if (typeof b.project !== "string" || !PROJECT_NAME.test(b.project)) return null;
    if (typeof b.number !== "string" || !TICKET_NUMBER.test(b.number)) return null;
    return ["ticket", b.done ? "close" : "reopen", b.project, b.number];
  }
  return null;
}

/**
 * `lime focus done|edit` or `lime ticket close|reopen` — one owner write. A bad
 * body is never spawned; a spawn failure, a timeout or a non-zero exit all
 * become `{ok:false, message}` (the stderr's first non-empty line, else one
 * failure line), never a throw.
 */
export async function homeTick(
  req: HomeTickRequest,
  opts: ProxyOpts & { timeoutMs?: number } = {},
): Promise<HomeTickResponse> {
  const args = tickArgs(req);
  if (!args) return { ok: false, message: "bad tick request" };
  const timeoutMs = opts.timeoutMs ?? SPAWN_TIMEOUT_MS;
  const run = await runLime(args, timeoutMs, opts);
  if (run.exitCode === 0) return { ok: true, message: null };
  return {
    ok: false,
    message:
      firstNonEmptyLine(run.stderr) ??
      failureLine("lime " + args.join(" "), run, timeoutMs),
  };
}
