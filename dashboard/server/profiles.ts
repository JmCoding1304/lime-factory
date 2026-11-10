/**
 * The dashboard's seam to `lime profiles get|set` (ticket 87) — the one write
 * path: a project's .lime/config.yaml.
 *
 * Everything shells out to the `lime` CLI with cwd = the project's working
 * tree: Python owns the config schema, the static validation and the
 * span-preserving rewrite, so this module never reads or writes the file (or
 * any db) itself. A rejected payload comes back as `lime profiles set`'s
 * `{ok:false, errors}` stdout, with the file already guaranteed untouched.
 *
 * Plus the model catalog: pi's own catalog via `lime models` (ticket 92) —
 * every provider pi is authenticated for, not just OpenRouter — with a ledger
 * fallback: the distinct (runtime, model) pairs this installation has run.
 * Plus the per-model thinking support table from profiles get (ticket 91).
 */
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import type {
  AgentEntry,
  CatalogResponse,
  ModelCatalogEntry,
  ProfileModelChange,
  ProfileSyncResult,
  ProfilesResponse,
} from "../shared/types.ts";
import type { LimeRegistry } from "./db.ts";

/**
 * The binary spawned for every profiles read/write. LIME_BIN overrides;
 * otherwise the repo's own venv wins over a stale globally installed `lime`
 * (the profiles subcommand ships with this branch), falling back to PATH.
 */
export const LIME_BIN =
  process.env.LIME_BIN ??
  (existsSync(resolve(import.meta.dir, "..", "..", ".venv", "bin", "lime"))
    ? resolve(import.meta.dir, "..", "..", ".venv", "bin", "lime")
    : "lime");

/** Can the resolved binary actually be spawned? (Tests skip on false.) */
export function limeAvailable(): boolean {
  return LIME_BIN.includes("/")
    ? existsSync(LIME_BIN)
    : Bun.which(LIME_BIN) !== null;
}

/** `lime profiles get`'s stdout, parsed. */
export interface ProfilesGet {
  agents: AgentEntry[];
  profiles: ProfilesResponse["profiles"];
  defaults: Record<string, unknown>;
  merged: Record<string, AgentEntry[]>;
  profiles_list: string[];
  thinking_support: Record<string, string[]>;
}

/** `lime profiles set`'s stdout, parsed. */
export type ProfilesSetResult =
  | { ok: true; wrote: boolean }
  | { ok: false; errors: string[] };

/** `lime profiles merge`'s stdout, parsed. */
export type ProfilesMergeResult =
  | { ok: true; wrote: boolean }
  | { ok: false; errors: string[] };

export interface LimeExit {
  exitCode: number;
  /** Parsed stdout JSON, or null when stdout wasn't valid JSON. */
  json: unknown;
  stderr: string;
}

const SPAWN_TIMEOUT_MS = 30_000;

/** Spawn `lime profiles <verb>` in the project's working tree, parse stdout. */
export async function runLimeProfiles(
  projectPath: string,
  verb: "get" | "set" | "merge",
  stdin?: string,
): Promise<LimeExit> {
  const proc = Bun.spawn([LIME_BIN, "profiles", verb], {
    cwd: projectPath,
    stdin: stdin === undefined ? "ignore" : "pipe",
    stdout: "pipe",
    stderr: "pipe",
  });
  if (stdin !== undefined && proc.stdin) {
    proc.stdin.write(stdin);
    proc.stdin.end();
  }
  const killer = setTimeout(() => proc.kill(), SPAWN_TIMEOUT_MS);
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ]);
  clearTimeout(killer);
  let json: unknown = null;
  try {
    json = JSON.parse(stdout) as unknown;
  } catch {
    // exit code + stderr carry the failure; the route turns them into a 400/500
  }
  return { exitCode, json, stderr };
}

function asProfilesGet(json: unknown): ProfilesGet | null {
  if (typeof json !== "object" || json === null) return null;
  const d = json as Record<string, unknown>;
  if (!Array.isArray(d.agents) || typeof d.profiles !== "object" || d.profiles === null) {
    return null;
  }
  return d as unknown as ProfilesGet;
}

/** GET …/profiles — `lime profiles get`, framed with the project's identity. */
export async function getProfiles(
  project: string,
  projectPath: string,
): Promise<ProfilesResponse> {
  const res = await runLimeProfiles(projectPath, "get");
  const data = res.exitCode === 0 ? asProfilesGet(res.json) : null;
  if (!data) {
    throw new Error(
      `lime profiles get failed (exit ${res.exitCode})${res.stderr.trim() ? `: ${res.stderr.trim()}` : ""}`,
    );
  }
  return {
    project,
    config_path: join(projectPath, ".lime", "config.yaml"),
    agents: data.agents,
    profiles: data.profiles,
    defaults: data.defaults,
    merged: data.merged,
    profiles_list: data.profiles_list,
    thinking_support: data.thinking_support ?? {},
  };
}

/**
 * PUT …/profiles — the body is shape-checked by the route; validation proper
 * happens inside `lime profiles set`. `ok:false` carries the exact `errors`
 * strings Python produced (the `validate()` messages) and the file is already
 * guaranteed untouched; success returns the fresh roster so the UI needs no
 * second round-trip.
 */
export async function putProfiles(
  project: string,
  projectPath: string,
  body: { agents: AgentEntry[]; profiles: ProfilesResponse["profiles"] },
): Promise<{ ok: true; response: ProfilesResponse } | { ok: false; errors: string[] }> {
  const res = await runLimeProfiles(projectPath, "set", `${JSON.stringify(body)}\n`);
  const parsed = res.json as ProfilesSetResult | null;
  if (res.exitCode !== 0 || !parsed || parsed.ok !== true) {
    const errors =
      parsed && parsed.ok === false && Array.isArray(parsed.errors) && parsed.errors.length > 0
        ? parsed.errors
        : [
            `lime profiles set failed (exit ${res.exitCode})${res.stderr.trim() ? `: ${res.stderr.trim()}` : ""}`,
          ];
    return { ok: false, errors };
  }
  return { ok: true, response: await getProfiles(project, projectPath) };
}

// --- profiles merge: batch-sync (step 2) ---

/**
 * Run ``lime profiles merge`` for a single target project with the given
 * changes.  Returns the parsed result; any catch-level failure (spawn error,
 * non-JSON stdout) is turned into an ok:false row with a descriptive error.
 */
export async function mergeProfiles(
  projectPath: string,
  changes: ProfileModelChange[],
): Promise<ProfilesMergeResult> {
  const stdin = `${JSON.stringify({ changes })}\n`;
  const res = await runLimeProfiles(projectPath, "merge", stdin);
  const parsed = res.json as ProfilesMergeResult | null;
  if (res.exitCode !== 0 || !parsed || parsed.ok !== true) {
    const errors =
      parsed && parsed.ok === false && Array.isArray(parsed.errors) && parsed.errors.length > 0
        ? parsed.errors
        : [
            `lime profiles merge failed (exit ${res.exitCode})${res.stderr.trim() ? `: ${res.stderr.trim()}` : ""}`,
          ];
    return { ok: false, errors };
  }
  return parsed;
}

/**
 * Batch merge: resolve every requested target, invoke one ``mergeProfiles`` per
 * registered target, catch failures per target, and continue after failures.
 * A successful target is never rolled back because another target fails.
 */
export async function batchMergeProfiles(
  targets: { name: string; path: string | null }[],
  changes: ProfileModelChange[],
): Promise<ProfileSyncResult[]> {
  const results: ProfileSyncResult[] = [];
  for (const target of targets) {
    if (!target.path) {
      results.push({
        project: target.name,
        ok: false,
        wrote: false,
        errors: [`unregistered project ${target.name}`],
      });
      continue;
    }
    try {
      const mergeResult = await mergeProfiles(target.path, changes);
      results.push({
        project: target.name,
        ok: mergeResult.ok,
        wrote: mergeResult.ok ? (mergeResult as { ok: true; wrote: boolean }).wrote : false,
        errors: (mergeResult as { ok: false; errors: string[] }).errors ?? [],
      });
    } catch (error) {
      results.push({
        project: target.name,
        ok: false,
        wrote: false,
        errors: [(error as Error).message],
      });
    }
  }
  return results;
}

// --- model catalog: pi's own catalog first, the session ledger as the fallback ---

const numOrNull = (v: unknown): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

function toCatalogEntry(raw: unknown): ModelCatalogEntry | null {
  if (typeof raw !== "object" || raw === null) return null;
  const m = raw as Record<string, unknown>;
  if (typeof m.id !== "string") return null;
  return {
    id: m.id,
    name: typeof m.name === "string" ? m.name : m.id,
    provider: typeof m.provider === "string" ? m.provider : m.id.split("/")[0]!,
    prompt_price: numOrNull(m.prompt_price),
    completion_price: numOrNull(m.completion_price),
    context_length: numOrNull(m.context_length),
  };
}

/** `lime models` — every model pi can run, `provider/id` as a config writes it.
 *  null = no catalog (no pi store, or the spawn failed): the caller falls back
 *  to the ledger. Local file read behind a subprocess, so no cache and no TTL:
 *  a provider you authenticate mid-session shows up on the next page load.
 *  `bin` is the tests' seam for an unspawnable binary. */
export async function piModelCatalog(
  bin: string = LIME_BIN,
): Promise<ModelCatalogEntry[] | null> {
  try {
    const proc = Bun.spawn([bin, "models"], { stdout: "pipe", stderr: "pipe" });
    const killer = setTimeout(() => proc.kill(), SPAWN_TIMEOUT_MS);
    const [stdout, exitCode] = await Promise.all([
      new Response(proc.stdout).text(),
      proc.exited,
    ]);
    clearTimeout(killer);
    if (exitCode !== 0) throw new Error(`lime models exited ${exitCode}`);
    const body = JSON.parse(stdout) as { models?: unknown };
    const raw = Array.isArray(body.models) ? body.models : [];
    const models = raw
      .map(toCatalogEntry)
      .filter((e): e is ModelCatalogEntry => e !== null);
    if (models.length >= 1) return models;
  } catch (error) {
    console.warn(
      `[profiles] pi model catalog unavailable: ${(error as Error).message}`,
    );
  }
  return null;
}

/** Ledger fallback: distinct (runtime, model) pairs this installation has run.
 *  No price table — claude_code aliases ride the subscription bucket, pi
 *  models take their model-id prefix as the provider. */
export function ledgerCatalog(reg: LimeRegistry): CatalogResponse {
  const seen = new Set<string>();
  const models: ModelCatalogEntry[] = [];
  for (const row of reg.ledgerModels()) {
    const key = `${row.runtime}\u0000${row.model}`;
    if (seen.has(key)) continue;
    seen.add(key);
    models.push({
      id: row.model,
      name: row.model,
      provider:
        row.runtime === "claude_code"
          ? "claude_code"
          : (row.model.split("/")[0] ?? row.model),
      prompt_price: null,
      completion_price: null,
      context_length: null,
    });
  }
  return { source: "ledger", models };
}

export async function modelCatalog(
  reg: LimeRegistry,
  bin: string = LIME_BIN,
): Promise<CatalogResponse> {
  const pi = await piModelCatalog(bin);
  if (pi) return { source: "pi", models: pi };
  return ledgerCatalog(reg);
}