// Pure engine for the profiles page — a port of the proto's engine.js +
// shared.js validateSlot over the real ticket-87 shapes. Mutating helpers on a
// `Draft` (the raw `agents:`/`profiles:` blocks), no mock data, no DOM.
// Resolution precedence is the server's (config.py merged_agents):
// profile patch > agent entry > profile defaults > config defaults.

import type {
  AgentEntry,
  ProfileModelChange,
  ProfileModelPatch,
  ProfilesPayload,
  ProfilesResponse,
  SessionCost,
} from "./types";
import { modelStats } from "./cost";

export const ROLE_ORDER = [
  "scout",
  "researcher",
  "planner",
  "builder",
  "reviewer",
  "documenter",
  "designer",
] as const;

export const THINKING = [
  "off",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
] as const;

export type ThinkingSupport = Record<string, readonly string[]>;

/** The levels a model supports — the table from Python, else the full enum. */
export function levelsFor(
  support: ThinkingSupport | undefined,
  model: string,
): readonly string[] {
  const levels = support?.[model];
  return levels && levels.length > 0 ? levels : THINKING;
}

/** Clamp a thinking level to the nearest supported one; ties go higher;
 *  an unknown `cur` ranks as `medium`; empty levels pass through. */
export function clampThinking(cur: string, levels: readonly string[]): string {
  if (levels.length === 0 || levels.includes(cur)) return cur;
  const ref = THINKING;
  const curIdx = ref.indexOf(cur as (typeof ref)[number]);
  const refIdx = curIdx >= 0 ? curIdx : ref.indexOf("medium");
  // Find the nearest level by THINKING index, ties go to the higher level
  let best = levels[0];
  let bestDist = Infinity;
  for (const l of levels) {
    const li = ref.indexOf(l as (typeof ref)[number]);
    const d = Math.abs(li - refIdx);
    if (d < bestDist || (d === bestDist && li > ref.indexOf(best as (typeof ref)[number]))) {
      best = l;
      bestDist = d;
    }
  }
  return best;
}

/** Cost/time multipliers per thinking level — a stepper moves the estimate the
 *  way it would move the bill. */
export const THINK_COST: Record<string, number> = {
  off: 0.72,
  minimal: 0.8,
  low: 0.88,
  medium: 1,
  high: 1.28,
  xhigh: 1.55,
  max: 1.9,
};
export const THINK_TIME: Record<string, number> = {
  off: 0.75,
  minimal: 0.85,
  low: 0.9,
  medium: 1,
  high: 1.3,
  xhigh: 1.65,
  max: 2.1,
};

export const CC_ALIASES = ["haiku", "sonnet", "opus", "fable"] as const;

/** What the UI edits — timeout_s is never touched client-side. */
export const PROFILE_KEYS = ["runtime", "model", "thinking"] as const;

export type Profiles = ProfilesPayload["profiles"];

export interface Draft {
  agents: AgentEntry[];
  profiles: Profiles;
}

export type Src = "patch" | "agent" | "profile" | "config" | "none";

export interface Slot {
  runtime?: string;
  model?: string;
  thinking?: string;
}

export interface Resolved {
  role: string;
  runtime: string;
  model: string;
  thinking: string;
  src: Record<"runtime" | "model" | "thinking", Src>;
  patched: boolean;
  invalid: string | null;
}

export const str = (v: unknown): string | undefined =>
  typeof v === "string" ? v : undefined;

export const nameOf = (a: AgentEntry | undefined): string => str(a?.name) ?? "";

export function cloneDraft(
  r: Pick<ProfilesResponse, "agents" | "profiles">,
): Draft {
  return structuredClone({ agents: r.agents, profiles: r.profiles });
}

export function profileNames(d: Draft): string[] {
  return ["default", ...Object.keys(d.profiles)];
}

/** ROLE_ORDER members first (that order), then the rest in given order, deduped. */
export function orderRoles(names: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const r of ROLE_ORDER) {
    if (names.includes(r)) {
      out.push(r);
      seen.add(r);
    }
  }
  for (const n of names) {
    if (!seen.has(n)) {
      out.push(n);
      seen.add(n);
    }
  }
  return out;
}

/** "default" → the whole roster in chain order; else the profile's patch roles. */
export function benchRoles(d: Draft, profile: string): string[] {
  if (profile === "default") {
    return orderRoles(d.agents.map(nameOf));
  }
  return orderRoles(
    (d.profiles[profile]?.agents ?? []).map(nameOf),
  );
}

export function agentByName(
  d: Draft,
  role: string,
): AgentEntry | undefined {
  return d.agents.find((a) => nameOf(a) === role);
}

/** The profile's patch entry for a role — undefined under default. */
export function patchOf(
  d: Draft,
  profile: string,
  role: string,
): AgentEntry | undefined {
  if (profile === "default") return undefined;
  return d.profiles[profile]?.agents?.find((a) => nameOf(a) === role);
}

/** patch > entry > profile defaults > config defaults (only strings count). */
export function resolveRole(
  d: Draft,
  defaults: Record<string, unknown>,
  profile: string,
  role: string,
  support?: ThinkingSupport,
): Resolved {
  const entry = agentByName(d, role);
  const patch = patchOf(d, profile, role);
  const pdefs = profile === "default" ? undefined : d.profiles[profile]?.defaults;
  const src: Record<"runtime" | "model" | "thinking", Src> = {
    runtime: "none",
    model: "none",
    thinking: "none",
  };
  const vals: Record<"runtime" | "model" | "thinking", string> = {
    runtime: "",
    model: "",
    thinking: "medium",
  };
  for (const k of ["runtime", "model", "thinking"] as const) {
    const pv = patch ? str(patch[k]) : undefined;
    const ev = str(entry?.[k]);
    const dv = pdefs ? str(pdefs[k]) : undefined;
    const cv = str(defaults[k]);
    if (pv !== undefined) {
      src[k] = "patch";
      vals[k] = pv;
    } else if (ev !== undefined) {
      src[k] = "agent";
      vals[k] = ev;
    } else if (dv !== undefined) {
      src[k] = "profile";
      vals[k] = dv;
    } else if (cv !== undefined) {
      src[k] = "config";
      vals[k] = cv;
    }
  }
  // Unset thinking resolves to medium (the server's default) but stays "none" so
  // the block shows it as inherited; an unresolved runtime/model is empty.
  if (src.thinking === "none") vals.thinking = "medium";
  return {
    role,
    runtime: vals.runtime,
    model: vals.model,
    thinking: vals.thinking,
    src,
    patched: !!patch,
    invalid: validateSlot({
      runtime: vals.runtime,
      model: vals.model,
      thinking: vals.thinking,
    }, support),
  };
}

/**
 * Derive a ProfileModelChange for a model pick on a role.
 * Computes the patch from the untouched draft (including clamped thinking),
 * then the caller mutates with setRole and returns this change for sync.
 */
export function modelChange(
  d: Draft,
  defaults: Record<string, unknown>,
  profile: string,
  role: string,
  model: string,
  support?: ThinkingSupport,
): ProfileModelChange {
  const patch = modelPatch(d, defaults, profile, role, model, support);
  const result: ProfileModelPatch = { model: patch.model!, runtime: patch.runtime! };
  if (patch.thinking !== undefined && patch.thinking !== null) {
    result.thinking = patch.thinking;
  }
  return { profile, role, patch: result };
}

/**
 * Derive a ProfileModelChange for a named-profile defaults model pick.
 * role is null for defaults changes.
 */
export function defaultsModelChange(
  d: Draft,
  profile: string,
  model: string,
  support?: ThinkingSupport,
): ProfileModelChange {
  const defs = d.profiles[profile]?.defaults ?? {};
  const cur = str(defs.thinking) ?? "medium";
  const runtime = runtimeFor(model);
  const patch: ProfileModelPatch = { model, runtime };
  const levels = levelsFor(support, model);
  if (!levels.includes(cur)) {
    patch.thinking = clampThinking(cur, levels);
  }
  return { profile, role: null, patch };
}

/**
 * Like addRole but returns the ProfileModelChange for sync.
 * Mutates the draft then returns the change.
 */
export function addRoleWithChange(
  d: Draft,
  profile: string,
  role: string,
  model: string,
  defaults: Record<string, unknown> = {},
  support?: ThinkingSupport,
): ProfileModelChange {
  const change = modelChange(d, defaults, profile, role, model, support);
  if (profile !== "default") {
    setRole(d, profile, role, change.patch as SlotPatch);
  }
  return change;
}

/** The contract `lime run --profile X` enforces (config.py, minus "<role>: "). */
export function validateSlot(s: Slot, support?: ThinkingSupport): string | null {
  if (s.runtime !== "pi" && s.runtime !== "claude_code") {
    return `runtime '${s.runtime}' is neither pi nor claude_code`;
  }
  if (!s.model) return "no model resolves (defaults set none either)";
  if (s.runtime === "pi") {
    if (!s.model.includes("/")) {
      return `pi model '${s.model}' needs the provider/model-id form`;
    }
    if (s.model.startsWith("anthropic/")) {
      return "anthropic/* on pi would spend the subscription through a third-party harness — use openrouter/anthropic/*";
    }
  } else if (
    !CC_ALIASES.includes(s.model as (typeof CC_ALIASES)[number]) &&
    !s.model.startsWith("claude-")
  ) {
    return `claude_code model '${s.model}' is no alias (haiku, sonnet, opus, fable) or claude-* id`;
  }
  if (s.thinking !== undefined && !THINKING.includes(s.thinking as never)) {
    return `thinking '${s.thinking}' is not one of (${THINKING.join(", ")})`;
  }
  // per-model check for pi only
  if (s.runtime === "pi" && s.thinking !== undefined) {
    const levels = levelsFor(support, s.model);
    if (!levels.includes(s.thinking)) {
      return `thinking '${s.thinking}' is not supported by ${s.model} — supported: (${levels.join(", ")})`;
    }
  }
  return null;
}

export const runtimeFor = (model: string): "pi" | "claude_code" =>
  model.includes("/") ? "pi" : "claude_code";

/** The patch a model pick writes: model + runtime plus clamped thinking when
 *  the current level is not in the model's supported set. */
export function modelPatch(
  d: Draft,
  defaults: Record<string, unknown>,
  profile: string,
  role: string,
  model: string,
  support?: ThinkingSupport,
): SlotPatch {
  const patch: SlotPatch = { model, runtime: runtimeFor(model) };
  const cur = resolveRole(d, defaults, profile, role).thinking;
  const levels = levelsFor(support, model);
  if (!levels.includes(cur)) {
    patch.thinking = clampThinking(cur, levels);
  }
  return patch;
}

export type SlotPatch = Partial<
  Record<"runtime" | "model" | "thinking", string | null>
>;

function applyPatch(entry: AgentEntry, patch: SlotPatch): void {
  for (const [k, v] of Object.entries(patch)) {
    if (v === null) delete entry[k];
    else entry[k] = v;
  }
}

/** default: mutate the roster entry in place (other keys untouched). Profile:
 *  upsert exactly one patch entry; an entry left with only `name` stays — it
 *  is membership, not an override (ticket 89); removeRole is the way off. */
export function setRole(
  d: Draft,
  profile: string,
  role: string,
  patch: SlotPatch,
): void {
  if (profile === "default") {
    const entry = agentByName(d, role);
    if (!entry) return;
    applyPatch(entry, patch);
    return;
  }
  const prof = (d.profiles[profile] ??= {});
  const agents = (prof.agents ??= []);
  let entry = agents.find((a) => nameOf(a) === role);
  if (!entry) {
    entry = { name: role };
    agents.push(entry);
  }
  applyPatch(entry, patch);
}

/** to = trimmed; blank or unchanged → false. default renames the entry and every
 *  patch naming it; a profile renames its patch (dropping an existing target
 *  first — no duplicate). No validation here — the server decides. */
export function renameRole(
  d: Draft,
  profile: string,
  from: string,
  to: string,
): boolean {
  const name = to.trim();
  if (!name || name === from) return false;
  if (profile === "default") {
    let changed = false;
    const entry = agentByName(d, from);
    if (entry) {
      entry.name = name;
      changed = true;
    }
    for (const p of Object.values(d.profiles)) {
      for (const a of p.agents ?? []) {
        if (nameOf(a) === from) {
          a.name = name;
          changed = true;
        }
      }
    }
    return changed;
  }
  const agents = d.profiles[profile]?.agents;
  if (!agents) return false;
  const target = agents.findIndex((a) => nameOf(a) === name);
  if (target >= 0) agents.splice(target, 1);
  const patch = agents.find((a) => nameOf(a) === from);
  if (!patch) return false;
  patch.name = name;
  return true;
}

/** default: drop the entry and every patch naming it; profile: drop the patch. */
export function removeRole(d: Draft, profile: string, role: string): void {
  if (profile === "default") {
    d.agents = d.agents.filter((a) => nameOf(a) !== role);
    for (const p of Object.values(d.profiles)) {
      if (p.agents) p.agents = p.agents.filter((a) => nameOf(a) !== role);
    }
    return;
  }
  const prof = d.profiles[profile];
  if (prof?.agents) prof.agents = prof.agents.filter((a) => nameOf(a) !== role);
}

/** Profile only: seed a patch with the model + its implied runtime. */
export function addRole(
  d: Draft,
  profile: string,
  role: string,
  model: string,
  defaults: Record<string, unknown> = {},
  support?: ThinkingSupport,
): void {
  if (profile === "default") return;
  setRole(d, profile, role, modelPatch(d, defaults, profile, role, model, support));
}

/** null deletes a key; an emptied defaults object is removed from the profile. */
export function setDefaults(
  d: Draft,
  profile: string,
  patch: SlotPatch,
): void {
  if (profile === "default") return;
  const prof = d.profiles[profile];
  if (!prof) return;
  const defs = (prof.defaults ??= {});
  for (const [k, v] of Object.entries(patch)) {
    if (v === null) delete defs[k];
    else defs[k] = v;
  }
  if (!Object.keys(defs).length) delete prof.defaults;
}

export function clearDefaults(d: Draft, profile: string): void {
  if (profile === "default") return;
  delete d.profiles[profile]?.defaults;
}

/** lower, [^a-z0-9-]+ → "-", trim "-", max 32; may return "". */
export function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32);
}

/** id = slugify(name) || "new-profile"; an existing id is returned untouched.
 *  copyFrom (not "default") clones that profile, else a fresh {agents: []}. */
export function createProfile(
  d: Draft,
  name: string,
  copyFrom?: string | null,
): string {
  const id = slugify(name) || "new-profile";
  if (d.profiles[id]) return id;
  d.profiles[id] =
    copyFrom && copyFrom !== "default"
      ? structuredClone(d.profiles[copyFrom] ?? { agents: [] })
      : { agents: [] };
  return id;
}

export function deleteProfile(d: Draft, name: string): void {
  delete d.profiles[name];
}

/** engine.js semantics; returns the effective id (from when refused). */
export function renameProfile(d: Draft, from: string, to: string): string {
  const id = slugify(to);
  if (!id || id === from || d.profiles[id]) return from;
  d.profiles[id] = d.profiles[from];
  delete d.profiles[from];
  return id;
}

/** index wraps around the given levels (default THINKING); unknown cur counts as its clamp. */
export function stepThinking(cur: string, delta: number, levels: readonly string[] = THINKING): string {
  if (levels.length === 0) return cur;
  let idx = levels.indexOf(cur as string);
  if (idx < 0) {
    cur = clampThinking(cur, levels);
    idx = levels.indexOf(cur as string);
  }
  const len = levels.length;
  return levels[(idx + delta + len * len) % len];
}

/** Cost of one session = api_cost + (notional_cost ?? 0). */
export const sessionCost = (r: SessionCost): number =>
  r.api_cost + (r.notional_cost ?? 0);

/** Mean $/session for a model AND role when ≥1 such row, else the model's rows. */
export function sessionAvg(
  rows: SessionCost[],
  model: string,
  role?: string,
): number | null {
  const mean = (rs: SessionCost[]): number | null =>
    rs.length
      ? rs.reduce((a, r) => a + sessionCost(r), 0) / rs.length
      : null;
  const byModel = rows.filter((r) => r.model === model);
  if (role) {
    const scoped = byModel.filter((r) => r.role === role);
    if (scoped.length) return mean(scoped);
  }
  return mean(byModel);
}

export interface Leg extends Resolved {
  sub: boolean;
  avg: number | null;
  cost: number | null;
  rate: number;
  runs: number;
  median: number | null;
}

export interface Estimate {
  legs: Leg[];
  sum: number;
  pAll: number;
  wall: number;
  per: number | null;
  broken: boolean;
  empty: boolean;
}

/** est $/task for the priced legs on the bench = Σ(their session cost) ÷ P(they all succeed); null when a slot is invalid (broken) or nothing has cost history. pAll spans every leg. */
export function estimate(
  d: Draft,
  defaults: Record<string, unknown>,
  profile: string,
  rows: SessionCost[],
  notionalOn: boolean,
  support?: ThinkingSupport,
): Estimate {
  const stats = new Map(
    modelStats(rows, true).map((s) => [s.model, s] as const),
  );
  const legs: Leg[] = benchRoles(d, profile).map((role) => {
    const r = resolveRole(d, defaults, profile, role, support);
    const st = stats.get(r.model);
    const sub = r.runtime === "claude_code";
    const avg = sessionAvg(rows, r.model, role);
    const cost =
      r.invalid || avg == null
        ? null
        : sub && !notionalOn
          ? 0
          : avg * (THINK_COST[r.thinking] ?? 1);
    const rate = st ? Math.min(0.95, Math.max(0.3, st.successRate)) : 0.5;
    return {
      ...r,
      sub,
      avg,
      cost,
      rate,
      runs: st?.runs ?? 0,
      median: st?.medianMs ?? null,
    };
  });
  const sum = legs.reduce((a, l) => a + (l.cost ?? 0), 0);
  const pAll = legs.reduce((a, l) => a * l.rate, 1);
  const wall = legs.reduce(
    (a, l) => a + (l.median ?? 0) * (THINK_TIME[l.thinking] ?? 1),
    0,
  );
  const empty = legs.length === 0;
  // broken = a slot the server would refuse (validateSlot). A leg with no ledger
  // rows is merely unpriced: it drops out of `per` as it already does from `wall`.
  const broken = empty || legs.some((l) => !!l.invalid);
  const priced = legs.filter((l) => l.cost !== null);
  const pPriced = priced.reduce((a, l) => a * l.rate, 1);
  const per = broken || priced.length === 0 ? null : sum / pPriced;
  return { legs, sum, pAll, wall, per, broken, empty };
}