// Pure page logic for the profiles page — labels, projections, geometry glue.
// No DOM, no fetching; the .vue files stay thin.

import { fmtCost } from "./format";
import { GRID, type Pos } from "./bench";
import {
  agentByName,
  benchRoles,
  estimate,
  nameOf,
  orderRoles,
  profileNames,
  str,
  type Draft,
  type Leg,
  type ThinkingSupport,
} from "./profiles";
import type { TrayModel } from "./modelTray";
import type { Binding, KeyEventLike } from "./keymap";
import type {
  AgentEntry,
  ProfileModelChange,
  ProfileSyncResult,
  SessionCost,
} from "./types";

export type Dock =
  | "closed"
  | "profiles"
  | "new-profile"
  | "add-role"
  | "add-model"
  | "sync";

export const AGENT_COLOR_FALLBACK = "var(--dim)";

// ── sync (step 3) ───────────────────────────────────────────────────────────

/** One target project's sync status. */
export type SyncStatus =
  | "not-attempted"
  | "saving"
  | "success"
  | "noop"
  | "error";

export interface SyncTarget {
  name: string;
  selected: boolean;
  status: SyncStatus;
  message?: string;
}

/** A pending change keyed for coalescing. */
export interface PendingChange {
  /** `${profile}/${role ?? "defaults"}` */
  key: string;
  change: ProfileModelChange;
}

/** Initialise sync targets: current project is always selected, others opt-in. */
export function syncTargetsInit(
  currentProject: string,
  registeredProjects: string[],
): SyncTarget[] {
  return registeredProjects.map((name) => ({
    name,
    selected: name === currentProject,
    status: "not-attempted" as SyncStatus,
  }));
}

/** Toggle an extra target on/off; the current project stays selected. */
export function toggleSyncTarget(
  targets: SyncTarget[],
  name: string,
  currentProject: string,
): SyncTarget[] {
  if (name === currentProject) return targets;
  return targets.map((t) =>
    t.name === name
      ? { ...t, selected: !t.selected, status: "not-attempted" as SyncStatus, message: undefined }
      : t,
  );
}

/** Unique key for coalescing pending changes by profile+role scope. */
export function pendingChangesKey(
  profile: string,
  role: string | null,
): string {
  return `${profile}/${role ?? "defaults"}`;
}

/** Add or replace a pending change (deduplicated by key). */
export function coalesceChanges(
  pending: PendingChange[],
  change: ProfileModelChange,
): PendingChange[] {
  const key = pendingChangesKey(change.profile, change.role);
  const idx = pending.findIndex((p) => p.key === key);
  if (idx >= 0) {
    const updated = [...pending];
    updated[idx] = { key, change };
    return updated;
  }
  return [...pending, { key, change }];
}

/** Remove pending changes for a given profile+role scope. */
export function removePendingChange(
  pending: PendingChange[],
  profile: string,
  role: string | null,
): PendingChange[] {
  const key = pendingChangesKey(profile, role);
  return pending.filter((p) => p.key !== key);
}

/** Re-key a pending change when a role is renamed. */
export function rekeyPendingChange(
  pending: PendingChange[],
  profile: string,
  fromRole: string,
  toRole: string,
): PendingChange[] {
  const fromKey = pendingChangesKey(profile, fromRole);
  const toKey = pendingChangesKey(profile, toRole);
  return pending.map((p) =>
    p.key === fromKey ? { ...p, key: toKey, change: { ...p.change, role: toRole } } : p,
  );
}

/** UI label for a sync status. */
export function syncStatusLabel(status: SyncStatus): string {
  switch (status) {
    case "not-attempted":
      return "not attempted";
    case "saving":
      return "saving…";
    case "success":
      return "saved ✓";
    case "noop":
      return "no change";
    case "error":
      return "failed ✗";
  }
}

/** Number of selected targets beyond the current project. */
export function selectedExtraCount(targets: SyncTarget[]): number {
  return targets.filter((t) => t.selected).length - 1;
}

/** Update sync targets from a ProfileSyncResult array. */
export function applySyncResults(
  targets: SyncTarget[],
  results: ProfileSyncResult[],
): SyncTarget[] {
  return targets.map((t) => {
    const r = results.find((res) => res.project === t.name);
    if (!r) return t;
    const status: SyncStatus = r.ok ? (r.wrote ? "success" : "noop") : "error";
    return { ...t, status, message: r.errors.length ? r.errors.join("; ") : undefined };
  });
}

export const colorOf = (a: AgentEntry | undefined): string =>
  str(a?.color) ?? AGENT_COLOR_FALLBACK;

export function money(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return n >= 0.01 ? fmtCost(n) : "<$0.01";
}

/** One cost figure per block (fix 3): blank when invalid, `sub · free` when the
 *  leg rides the subscription and notional is off, `no data` without ledger
 *  rows, else the $/task figure. */
export function costLabel(leg: Leg, notionalOn: boolean): string {
  if (leg.invalid) return "";
  if (leg.sub && !notionalOn) return "sub · free";
  if (leg.cost === null) return "no data";
  return `${money(leg.cost)} / task`;
}

/** The profile card's one line: `empty bench`, `invalid config` only when a slot
 *  fails validateSlot, `no data` when no leg has cost history, else $/task. */
export function cardLabel(c: ProfileCard): string {
  if (c.empty) return "empty bench";
  if (c.broken) return "invalid config";
  if (c.per === null) return "no data";
  return `${money(c.per)} / task`;
}

/** The block's own server messages — "builder: system prompt missing …" →
 *  "system prompt missing …". */
export function errorsForRole(
  errors: string[] | null,
  role: string,
): string[] {
  if (!errors) return [];
  const prefix = `${role}:`;
  return errors
    .filter((m) => m.startsWith(prefix))
    .map((m) => m.slice(prefix.length).replace(/^\s+/, ""));
}

/** "all" resolves to the first registered project; a concrete name passes through. */
export function targetProject(
  project: string,
  projects: string[],
): string | null {
  return project !== "all" ? project : (projects[0] ?? null);
}

export function canvasNote(profile: string): string {
  return profile === "default"
    ? "roster — edits save to agents:"
    : `profile ${profile} — edits save to its patch`;
}

export interface ProfileCard {
  name: string;
  count: number;
  per: number | null;
  empty: boolean;
  broken: boolean;
  models: string[];
}

/** One card per profile, `default` first — a summary of its bench. */
export function profileCards(
  d: Draft,
  defaults: Record<string, unknown>,
  rows: SessionCost[],
  notionalOn: boolean,
  support?: ThinkingSupport,
): ProfileCard[] {
  return profileNames(d).map((name) => {
    const est = estimate(d, defaults, name, rows, notionalOn, support);
    const models: string[] = [];
    for (const leg of est.legs) {
      if (!models.includes(leg.model)) models.push(leg.model);
    }
    return {
      name,
      count: est.legs.length,
      per: est.per,
      empty: est.empty,
      broken: est.broken,
      models,
    };
  });
}

/** The tray's most-used face — a query reveals the rest of the catalog. */
export const TRAY_FACE = 8;

export function trayFace(models: TrayModel[], q: string): TrayModel[] {
  return q.trim() ? models : models.slice(0, TRAY_FACE);
}

/** Arrow keys move a block by the grid (×4 with shift), never below 0. */
export function nudge(pos: Pos, key: string, shift: boolean): Pos | null {
  const step = GRID * (shift ? 4 : 1);
  const next = { ...pos };
  if (key === "ArrowLeft") next.x = Math.max(0, pos.x - step);
  else if (key === "ArrowRight") next.x = pos.x + step;
  else if (key === "ArrowUp") next.y = Math.max(0, pos.y - step);
  else if (key === "ArrowDown") next.y = pos.y + step;
  else return null;
  return next;
}

// ── popovers ──────────────────────────────────────────────────────────────────────────

export interface PopPos {
  left: number;
  top: number;
}

/** Clamp a 340px popover to the viewport: beside the anchor, flipped above
 *  when it would not fit below (the proto's openPop rule). */
export function popPosition(
  anchor: { left: number; right: number; top: number; bottom: number },
  w: number,
  h: number,
  vw: number,
  vh: number,
): PopPos {
  const left = Math.min(Math.max(8, anchor.left), vw - w - 8);
  let top = anchor.bottom + 6;
  if (top + h > vh - 8) top = Math.max(8, anchor.top - h - 6);
  return { left, top };
}

export type Pop =
  | { kind: "model"; role: string }
  | { kind: "defaults" }
  | { kind: "rename-profile" };

// ── add-agent flow ──────────────────────────────────────────────────────────────────

export interface RosterCell {
  name: string;
  purpose: string;
  color: string;
  onBench: boolean;
}

/** The agents: roster in chain order — step 1 of the add flow. */
export function rosterGrid(d: Draft, profile: string): RosterCell[] {
  const bench = benchRoles(d, profile);
  return orderRoles(d.agents.map(nameOf)).map((name) => {
    const entry = agentByName(d, name);
    return {
      name,
      purpose: str(entry?.purpose) ?? "",
      color: colorOf(entry),
      onBench: bench.includes(name),
    };
  });
}

/** Proto keyboard map — p profiles · m models · a add · n new · / search. */
export const SHORTCUTS: Record<
  string,
  "profiles" | "tray" | "add" | "new" | "search"
> = {
  p: "profiles",
  m: "tray",
  a: "add",
  n: "new",
  "/": "search",
};

/** Keys Profiles answers to — declared in the shell keymap, handled by `onKey`. */
export const PROFILE_KEYS: string[] = [
  "Delete",
  "Backspace",
  "[",
  "]",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  ...Object.keys(SHORTCUTS),
];

/**
 * The Profiles page's shell-keymap bindings. `whileTyping` is left unset (default
 * false), so the shell keymap skips every one of these keys while focus is in a
 * text field. Escape is not here: the shell owns the only Escape binding.
 */
export function profileBindings(
  run: (e: KeyEventLike) => boolean | void,
): Binding[] {
  return PROFILE_KEYS.map((key) => ({
    id: `profiles: ${key}`,
    key,
    prevent: false,
    run,
  }));
}