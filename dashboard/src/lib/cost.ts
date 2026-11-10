import { fmtCost } from "./format";
import type { SessionCost, RunStatus } from "./types";
import { isCashModel } from "./types";

export const MIN_RUNS = 2;
export const ROLE_ORDER = [
  "planner",
  "builder",
  "reviewer",
  "scout",
  "documenter",
  "researcher",
  "designer",
];

export const isWin = (s: RunStatus | null) =>
  s === "success" || s === "deployed";

export interface ModelStat {
  model: string;
  sub: boolean;
  runs: number;
  wins: number;
  api: number;
  notional: number;
  tokens: number;
  successRate: number;
  priced: number | null;
  perWin: number | null;
  timed: number;
  medianMs: number | null;
  minMs: number | null;
  maxMs: number | null;
}

export function median(nums: number[]): number | null {
  if (nums.length === 0) return null;
  const sorted = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[mid];
  return (sorted[mid - 1] + sorted[mid]) / 2;
}

function groupByModel(
  rows: SessionCost[],
  notionalOn: boolean,
  role?: string,
): Map<string, ModelStat> {
  const map = new Map<string, ModelStat>();

  // Filter by role if given, aggregate per model.
  const filtered = role
    ? rows.filter((r) => r.role === role)
    : rows;

  // Distinct run_ids per model to count runs/wins correctly.
  const seenRuns = new Map<string, Set<string>>();
  const seenWins = new Map<string, Set<string>>();

  for (const r of filtered) {
    const key = r.model;
    if (!seenRuns.has(key)) seenRuns.set(key, new Set());
    seenRuns.get(key)!.add(r.run_id);
    if (isWin(r.run_status)) {
      if (!seenWins.has(key)) seenWins.set(key, new Set());
      seenWins.get(key)!.add(r.run_id);
    }
  }

  // Sum api, notional, tokens per model.
  const apiSum = new Map<string, number>();
  const notionalSum = new Map<string, number>();
  const tokensSum = new Map<string, number>();
  const isSubMap = new Map<string, boolean>();
  // Collect durations per model
  const durations = new Map<string, number[]>();

  for (const r of filtered) {
    const key = r.model;
    apiSum.set(key, (apiSum.get(key) ?? 0) + r.api_cost);
    notionalSum.set(
      key,
      (notionalSum.get(key) ?? 0) + (r.notional_cost ?? 0),
    );
    tokensSum.set(key, (tokensSum.get(key) ?? 0) + r.tokens);
    isSubMap.set(key, !isCashModel(r.model));
    if (r.duration_ms !== null) {
      if (!durations.has(key)) durations.set(key, []);
      durations.get(key)!.push(r.duration_ms);
    }
  }

  for (const key of seenRuns.keys()) {
    const runs = seenRuns.get(key)!.size;
    const wins = (seenWins.get(key) ?? new Set()).size;
    const api = apiSum.get(key) ?? 0;
    const notional = notionalSum.get(key) ?? 0;
    const tokens = tokensSum.get(key) ?? 0;
    const sub = isSubMap.get(key) ?? false;

    const durs = durations.get(key) ?? [];
    const timed = durs.length;
    const medianMs = timed > 0 ? median(durs) : null;
    const minMs = timed > 0 ? Math.min(...durs) : null;
    const maxMs = timed > 0 ? Math.max(...durs) : null;

    // priced = sub && !notionalOn ? null : api + (sub ? notional : 0)
    const priced: number | null =
      sub && !notionalOn
        ? null
        : api + (sub ? notional : 0);

    // perWin = priced === null || wins === 0 ? null : priced / wins
    const perWin: number | null =
      priced === null || wins === 0
        ? null
        : priced / wins;

    map.set(key, {
      model: key,
      sub,
      runs,
      wins,
      api,
      notional,
      tokens,
      successRate: runs > 0 ? wins / runs : 0,
      priced,
      perWin,
      timed,
      medianMs,
      minMs,
      maxMs,
    });
  }

  return map;
}

export function modelStats(
  rows: SessionCost[],
  notionalOn: boolean,
  role?: string,
): ModelStat[] {
  return Array.from(groupByModel(rows, notionalOn, role).values());
}

export function rankModels(
  rows: SessionCost[],
  notionalOn: boolean,
): ModelStat[] {
  const stats = modelStats(rows, notionalOn);
  // Sort: unpriced last; among priced, 0-win first, then perWin descending.
  return stats.toSorted((a, b) => {
    // unpriced last
    if (a.priced === null && b.priced !== null) return 1;
    if (a.priced !== null && b.priced === null) return -1;
    if (a.priced === null && b.priced === null) return 0;
    // both priced: 0-win first
    if (a.wins === 0 && b.wins > 0) return -1;
    if (a.wins > 0 && b.wins === 0) return 1;
    // both have wins or both have 0 wins: perWin descending
    const pa = a.perWin ?? 0;
    const pb = b.perWin ?? 0;
    return pb - pa;
  });
}

export function rankUsage(
  rows: SessionCost[],
  notionalOn: boolean,
): ModelStat[] {
  return modelStats(rows, notionalOn).toSorted((a, b) => {
    if (a.priced === null && b.priced !== null) return 1;
    if (a.priced !== null && b.priced === null) return -1;
    if (a.priced === null && b.priced === null) return 0;
    return (b.priced ?? 0) - (a.priced ?? 0);
  });
}

export function rankSpeed(
  rows: SessionCost[],
  notionalOn: boolean,
): ModelStat[] {
  return modelStats(rows, notionalOn).toSorted((a, b) => {
    if (a.medianMs === null && b.medianMs !== null) return 1;
    if (a.medianMs !== null && b.medianMs === null) return -1;
    if (a.medianMs === null && b.medianMs === null) return 0;
    return (b.medianMs ?? 0) - (a.medianMs ?? 0);
  });
}

export function bySpeed(stats: ModelStat[]): ModelStat[] {
  return [...stats].toSorted((a, b) => {
    if (a.medianMs === null && b.medianMs !== null) return 1;
    if (a.medianMs !== null && b.medianMs === null) return -1;
    if (a.medianMs === null && b.medianMs === null) return 0;
    return (a.medianMs ?? 0) - (b.medianMs ?? 0);
  });
}

export interface RoleCard {
  role: string;
  stats: ModelStat[];
  best: string | null;
  fastest: string | null;
}

export function roleCards(
  rows: SessionCost[],
  notionalOn: boolean,
): RoleCard[] {
  // Distinct roles
  const roles = new Set<string>();
  for (const r of rows) {
    roles.add(r.role);
  }

  // Order: ROLE_ORDER first, then alphabetical
  const ordered: string[] = [];
  const roleSet = new Set(roles);
  for (const r of ROLE_ORDER) {
    if (roleSet.has(r)) {
      ordered.push(r);
      roleSet.delete(r);
    }
  }
  // Remaining roles alphabetically
  const remaining = Array.from(roleSet).sort();
  ordered.push(...remaining);

  return ordered.map((role) => {
    const stats = modelStats(rows, notionalOn, role);
    // Sort: perWin ascending, 0-win first, unpriced at end
    stats.sort((a, b) => {
      // unpriced at end
      if (a.priced === null && b.priced !== null) return 1;
      if (a.priced !== null && b.priced === null) return -1;
      if (a.priced === null && b.priced === null) return 0;
      // 0-win first among priced
      if (a.wins === 0 && b.wins > 0) return -1;
      if (a.wins > 0 && b.wins === 0) return 1;
      // both have wins or 0 wins: perWin ascending
      return (a.perWin ?? 0) - (b.perWin ?? 0);
    });

    // best = the priced model with runs >= MIN_RUNS && wins >= 1 and lowest perWin
    let best: string | null = null;
    let bestPerWin = Infinity;
    for (const s of stats) {
      if (
        s.priced !== null &&
        s.runs >= MIN_RUNS &&
        s.wins >= 1 &&
        s.perWin !== null &&
        s.perWin < bestPerWin
      ) {
        bestPerWin = s.perWin;
        best = s.model;
      }
    }

    // fastest = model with lowest medianMs where timed >= MIN_RUNS
    let fastest: string | null = null;
    let bestMedian = Infinity;
    for (const s of stats) {
      if (
        s.timed >= MIN_RUNS &&
        s.medianMs !== null &&
        s.medianMs < bestMedian
      ) {
        bestMedian = s.medianMs;
        fastest = s.model;
      }
    }

    return { role, stats, best, fastest };
  });
}

export function fmtUsd(n: number): string {
  if (n >= 0.01) return fmtCost(n);
  return "<$0.01";
}