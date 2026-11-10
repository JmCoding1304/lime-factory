// Runs-sheet model: status filters and local-calendar day grouping. Pure and
// Vue-free, so the sheet component only renders what these return.
import type { RunStatus, RunSummary } from "./types";
import { ts } from "./format";

export type RunFilter = "all" | "running" | "failed" | "done";

/** Filter tabs in display order. `null` statuses means "match everything". */
export const RUN_FILTERS: { id: RunFilter; label: string; statuses: RunStatus[] | null }[] = [
  { id: "all", label: "All", statuses: null },
  { id: "running", label: "Running", statuses: ["running"] },
  { id: "failed", label: "Failed", statuses: ["fail", "budget_exhausted", "needs_rescope"] },
  { id: "done", label: "Done", statuses: ["success", "deployed", "discarded"] },
];

/** `all` matches every status, including a null one. */
export function matchesFilter(status: RunStatus | null, f: RunFilter): boolean {
  const entry = RUN_FILTERS.find((e) => e.id === f);
  if (!entry || !entry.statuses) return true;
  return status != null && entry.statuses.includes(status);
}

export function filterCount(runs: RunSummary[], f: RunFilter): number {
  return runs.filter((r) => matchesFilter(r.status, f)).length;
}

// Local English months, no ICU. Index matches Date.getMonth().
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const startOfDay = (d: Date): number =>
  new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

/** The local calendar-day bucket of `iso` relative to `nowMs`. */
export function dayLabel(iso: string | null, nowMs: number): string {
  if (!iso) return "No date";
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "No date";
  const d = new Date(t);
  const diff = Math.round((startOfDay(new Date(nowMs)) - startOfDay(d)) / 86_400_000);
  if (diff <= 0) return "Today";
  if (diff === 1) return "Yesterday";
  if (diff <= 6) return "This week";
  return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

/**
 * Newest-first runs that match `f`, grouped into consecutive day buckets.
 * Null `started_at` sorts last and lands under "No date". The input is never
 * mutated and no group is ever empty.
 */
export function dayGroups(
  runs: RunSummary[], f: RunFilter, nowMs: number,
): { label: string; runs: RunSummary[] }[] {
  const sorted = runs.toSorted(
    (a, b) => (ts(b.started_at) || 0) - (ts(a.started_at) || 0),
  );
  const groups: { label: string; runs: RunSummary[] }[] = [];
  for (const run of sorted) {
    if (!matchesFilter(run.status, f)) continue;
    const label = dayLabel(run.started_at, nowMs);
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.runs.push(run);
    else groups.push({ label, runs: [run] });
  }
  return groups;
}

/**
 * The request's first sentence, for a Run that has no ticket. The first line
 * only; whitespace runs collapse to one space; the sentence ends at the first
 * `.`, `!` or `?` that is followed by whitespace or end-of-line — so `v4.1`
 * and `file.md` do not cut. A trailing `.` is dropped, `?`/`!` are kept. No
 * word cap: long text is clipped by CSS ellipsis. Empty input → "".
 */
export function firstSentence(request: string | null | undefined): string {
  if (request == null) return "";
  const line = (request.trim().split("\n")[0] ?? "")
    .replace(/\s+/g, " ")
    .trim();
  if (!line) return "";
  let cut = line.length;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === "." || ch === "!" || ch === "?") {
      const next = line[i + 1];
      if (next === undefined || next === " ") {
        cut = i + 1;
        break;
      }
    }
  }
  let s = line.slice(0, cut).trim();
  if (s.endsWith(".")) s = s.slice(0, -1).trim();
  return s;
}

/**
 * The Runs pool's refetch on the live feed's `running`: one load at a time; a
 * trigger during a load queues exactly one more (a second, third … trigger in
 * the same load still means one more), so the last change is always read; a
 * rejected load is swallowed and does not block later triggers.
 */
export function refetcher(load: () => Promise<void>): () => void {
  let inflight = false;
  let queued = false;
  const run = async (): Promise<void> => {
    inflight = true;
    await load().catch(() => {});
    inflight = false;
    if (queued) {
      queued = false;
      void run();
    }
  };
  return () => {
    if (inflight) {
      queued = true;
      return;
    }
    void run();
  };
}

/** `228-runs-sheet` → "Runs sheet": separators to spaces, first char upper. */
function humanize(words: string): string {
  const s = words.replace(/[-_]+/g, " ").replace(/\s+/g, " ").trim();
  if (!s) return s;
  return s[0].toUpperCase() + s.slice(1);
}

/**
 * The Runs-row label: a ticket's number and humanized stem, or the request's
 * first sentence (falling back to the run_id) when there is no ticket.
 */
export function runName(
  run: Pick<RunSummary, "ticket" | "request" | "run_id">,
): { num: string | null; text: string } {
  const ticket = run.ticket;
  if (ticket) {
    const base = ticket.split(/[\\/]/).pop() ?? ticket;
    const stem = base.replace(/\.md$/, "");
    const m = /^(\d+)-(.+)$/.exec(stem);
    if (m) return { num: m[1], text: humanize(m[2]) };
    return { num: null, text: humanize(stem) };
  }
  return { num: null, text: firstSentence(run.request) || run.run_id };
}
