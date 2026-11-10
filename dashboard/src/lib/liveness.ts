import type { RunSummary } from "./types";
import { ts } from "./format";

/** The Runs sheet's label clock (elapsed, idle, age) while it is open; it
 *  fetches nothing — the pool refetches on the live feed's `running` (App.vue). */
export const CLOCK_MS = 1000;

/** A running Run that has not emitted an event for this long is called idle. */
export const IDLE_AFTER_MS = 10 * 60_000;

/** Minutes since a running Run's last event (falling back to its start),
 *  or null while it is fresh — and null for any non-running status. */
export function idleMinutes(
  run: Pick<RunSummary, "status" | "last_event_at" | "started_at">,
  nowMs: number,
): number | null {
  if (run.status !== "running") return null;
  const ref = ts(run.last_event_at ?? run.started_at);
  if (!Number.isFinite(ref)) return null;
  if (nowMs - ref < IDLE_AFTER_MS) return null;
  return Math.floor((nowMs - ref) / 60_000);
}

/** The Runs-row idle column: `idle 37m` while a running Run is stale, ""
 *  otherwise (so the column is always present and the row stays aligned). */
export function idleLabel(
  run: Pick<RunSummary, "status" | "last_event_at" | "started_at">,
  nowMs: number,
): string {
  const m = idleMinutes(run, nowMs);
  return m === null ? "" : `idle ${m}m`;
}

/** The document title: how many Runs are running and how many failed. */
export function titleLine(runs: Pick<RunSummary, "status">[]): string {
  const running = runs.filter((r) => r.status === "running").length;
  const failed = runs.filter((r) => r.status === "fail").length;
  return `${running} running · ${failed} failed`;
}
