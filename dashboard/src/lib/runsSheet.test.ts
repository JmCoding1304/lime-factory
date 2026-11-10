import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { RunStatus, RunSummary } from "./types";
import {
  RUN_FILTERS, dayGroups, dayLabel, filterCount, matchesFilter, refetcher, runName,
  type RunFilter,
} from "./runsSheet";

function run(o: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: "r", project: "p", workflow: null, request: null, status: null,
    ticket: null, started_at: null, ended_at: null, total_cost: null,
    warns: 0, cash: 0, sub: false, last_event_at: null, ...o,
  };
}

// Local times, then toISOString, so both building and reading use local days.
const now = new Date(2026, 10, 5, 15).getTime();
const iso = (y: number, m: number, d: number, h = 9): string =>
  new Date(y, m, d, h).toISOString();

const STATUSES: RunStatus[] = [
  "running", "success", "fail", "budget_exhausted",
  "needs_rescope", "deployed", "discarded",
];
const NON_ALL: RunFilter[] = ["running", "failed", "done"];

describe("run filters", () => {
  test("every status matches exactly one non-all filter", () => {
    for (const s of STATUSES) {
      expect(NON_ALL.filter((f) => matchesFilter(s, f))).toHaveLength(1);
      expect(matchesFilter(s, "all")).toBe(true);
    }
    expect(matchesFilter(null, "all")).toBe(true);
  });

  test("filterCount counts a mixed fixture", () => {
    const runs = [
      run({ status: "running" }),
      run({ status: "success" }),
      run({ status: "fail" }),
      run({ status: "budget_exhausted" }),
      run({ status: "needs_rescope" }),
      run({ status: "deployed" }),
      run({ status: "discarded" }),
      run({ status: null }),
    ];
    expect(filterCount(runs, "all")).toBe(8);
    expect(filterCount(runs, "running")).toBe(1);
    expect(filterCount(runs, "failed")).toBe(3);
    expect(filterCount(runs, "done")).toBe(3);
  });
});

describe("dayLabel", () => {
  test("same day, previous day, this week, older, null and future", () => {
    expect(dayLabel(iso(2026, 10, 5), now)).toBe("Today");
    expect(dayLabel(iso(2026, 10, 4), now)).toBe("Yesterday");
    expect(dayLabel(iso(2026, 10, 2), now)).toBe("This week");
    expect(dayLabel(iso(2026, 8, 26), now)).toBe("September 2026");
    expect(dayLabel(null, now)).toBe("No date");
    expect(dayLabel(iso(2026, 10, 10), now)).toBe("Today");
  });
});

describe("dayGroups", () => {
  test("newest first, day buckets in order, null last", () => {
    const runs = [
      run({ run_id: "old", started_at: iso(2026, 8, 26, 10), status: "success" }),
      run({ run_id: "today-a", started_at: iso(2026, 10, 5, 9), status: "success" }),
      run({ run_id: "yest", started_at: iso(2026, 10, 4, 9), status: "success" }),
      run({ run_id: "week", started_at: iso(2026, 10, 2, 9), status: "success" }),
      run({ run_id: "none", started_at: null, status: "success" }),
      run({ run_id: "today-b", started_at: iso(2026, 10, 5, 14), status: "success" }),
    ];
    const g = dayGroups(runs, "all", now);
    expect(g.map((x) => x.label)).toEqual([
      "Today", "Yesterday", "This week", "September 2026", "No date",
    ]);
    expect(g[0].runs.map((r) => r.run_id)).toEqual(["today-b", "today-a"]);
    expect(g[4].runs.map((r) => r.run_id)).toEqual(["none"]);
  });

  test("failed keeps only failed runs and drops empty days", () => {
    const runs = [
      run({ run_id: "f1", started_at: iso(2026, 10, 5, 9), status: "fail" }),
      run({ run_id: "ok", started_at: iso(2026, 10, 5, 10), status: "success" }),
      run({ run_id: "b1", started_at: iso(2026, 10, 4, 9), status: "budget_exhausted" }),
      run({ run_id: "s1", started_at: iso(2026, 10, 2, 9), status: "success" }),
      run({ run_id: "nr", started_at: iso(2026, 10, 2, 10), status: "needs_rescope" }),
    ];
    const g = dayGroups(runs, "failed", now);
    expect(g.map((x) => x.label)).toEqual(["Today", "Yesterday", "This week"]);
    expect(g[0].runs.map((r) => r.run_id)).toEqual(["f1"]);
    expect(g[1].runs.map((r) => r.run_id)).toEqual(["b1"]);
    expect(g[2].runs.map((r) => r.run_id)).toEqual(["nr"]);
  });

  test("a filter with no match returns []", () => {
    const runs = [run({ status: "success", started_at: iso(2026, 10, 5) })];
    expect(dayGroups(runs, "running", now)).toEqual([]);
  });

  test("does not reorder its input array", () => {
    const input = [
      run({ run_id: "a", started_at: iso(2026, 10, 4) }),
      run({ run_id: "b", started_at: iso(2026, 10, 5) }),
    ];
    const before = input.map((r) => r.run_id);
    dayGroups(input, "all", now);
    expect(input.map((r) => r.run_id)).toEqual(before);
  });

  test("RUN_FILTERS exposes the four ordered tabs", () => {
    expect(RUN_FILTERS.map((f) => f.id)).toEqual(["all", "running", "failed", "done"]);
  });
});

describe("runName", () => {
  test("a ticket becomes its number and humanized stem", () => {
    expect(
      runName({ ticket: "/x/.lime/tickets/228-runs-sheet.md", request: null, run_id: "r" }),
    ).toEqual({ num: "228", text: "Runs sheet" });
  });

  test("a ticket without a number prefix has no num", () => {
    expect(
      runName({ ticket: "notes_v2.md", request: null, run_id: "r" }),
    ).toEqual({ num: null, text: "Notes v2" });
  });

  test("no ticket uses the request's first sentence; passes stay distinct", () => {
    const make = (p: string) => ({
      ticket: null,
      request: `Ticket #86 (AcmeWeb front-end overhaul), pass ${p}. Break down the UI references…`,
      run_id: "r",
    });
    const names = ["A", "B", "C", "D"].map((p) => runName(make(p)).text);
    expect(new Set(names).size).toBe(4);
    expect(runName(make("D")).text).toBe(
      "Ticket #86 (AcmeWeb front-end overhaul), pass D",
    );
  });

  test("a dotted version does not cut the sentence", () => {
    expect(
      runName({ ticket: null, request: "bump v4.1 now. next", run_id: "r" }).text,
    ).toBe("bump v4.1 now");
  });

  test("a question keeps its mark", () => {
    expect(
      runName({ ticket: null, request: "Why does X fail? Look.", run_id: "r" }).text,
    ).toBe("Why does X fail?");
  });

  test("a newline ends the sentence", () => {
    expect(
      runName({
        ticket: null,
        request: "Repo-only, apps/web on origin/main\nProduce…",
        run_id: "r",
      }).text,
    ).toBe("Repo-only, apps/web on origin/main");
  });

  test("a 30-word first sentence with no stop comes back whole", () => {
    const words = Array.from({ length: 30 }, (_, i) => `w${i}`).join(" ");
    expect(runName({ ticket: null, request: words, run_id: "r" }).text).toBe(words);
  });

  test("whitespace collapses and a trailing period drops", () => {
    expect(
      runName({ ticket: null, request: "  a   b. c", run_id: "r" }).text,
    ).toBe("a b");
  });

  test("a null, empty or whitespace request falls back to the run_id", () => {
    expect(runName({ ticket: null, request: null, run_id: "r" }).text).toBe("r");
    expect(runName({ ticket: null, request: "", run_id: "r" }).text).toBe("r");
    expect(runName({ ticket: null, request: "   ", run_id: "r" }).text).toBe("r");
  });
});

// ── Runs sheet wiring (source reads) ─────────────────────────────────────────
const read = (name: string): string =>
  readFileSync(join(import.meta.dir, "../components", name), "utf8");

/** The `function name` body, sliced up to the first column-0 `}` (focus.test.ts idiom). */
function fnBody(src: string, name: string): string {
  const start = src.indexOf(`function ${name}`);
  if (start < 0) return "";
  const end = src.indexOf("\n}", start);
  return src.slice(start, end < 0 ? undefined : end);
}

/** The single source line containing `needle`, or "". */
function lineWith(src: string, needle: string): string {
  return src.split("\n").find((l) => l.includes(needle)) ?? "";
}

/** The `selector {` … `\n}` CSS rule body, or "". */
function cssRule(src: string, selector: string): string {
  const start = src.indexOf(`${selector} {`);
  if (start < 0) return "";
  const end = src.indexOf("\n}", start);
  return src.slice(start, end < 0 ? undefined : end);
}

describe("Runs sheet wiring", () => {
  test("the top bar's Runs opens the sheet; Esc and an outside click close it", () => {
    const workspace = read("Workspace.vue");
    expect(workspace).toContain("<RunsSheet");
    expect(workspace).toContain('v-if="runsOpen"');
    expect(workspace).toContain('@open="openRun"');
    expect(fnBody(workspace, "closeTop")).toContain("runsOpen.value = false");

    const sheet = read("RunsSheet.vue");
    expect(lineWith(sheet, "rs-scrim")).toContain(`@mousedown="emit('close')"`);
    expect(sheet).not.toContain("addEventListener");
    expect(sheet).not.toContain("useKeys");
  });

  test("a sheet row opens a Run tab, never the hash", () => {
    const list = read("RunsList.vue");
    expect(list).toContain("emit('open'");
    expect(list).toContain("dayGroups(");
    const hashWrite = /\bnavigate\(|\bhrefFor\(|location\.hash/;
    expect(hashWrite.test(list)).toBe(false);
    expect(hashWrite.test(read("RunsSheet.vue"))).toBe(false);
  });

  test("the row drops cost; the sheet keeps its spend line", () => {
    const list = read("RunsList.vue");
    expect(list).not.toContain("r.cash");
    expect(list).not.toContain("+sub");
    expect(list).not.toContain("fmtCost");
    expect(list).not.toContain("r.warns");
    expect(list).not.toContain("r.workflow");
    expect(list).not.toContain("{{ r.run_id }}");
    expect(list).toContain("idleLabel(");
    const sheet = read("RunsSheet.vue");
    expect(sheet).toContain("spendLine(");
    expect(sheet).toContain("+sub");
  });

  test("each row is one line: glyph, name, idle, project, run time, age", () => {
    const src = read("RunsList.vue");
    const start = src.indexOf("<button");
    const end = src.indexOf("</button>", start);
    const row = src.slice(start, end + "</button>".length);
    const order = ["st-dot", "run-what", "run-idle", "chip repo", "run-dur", "run-age"];
    const idx = order.map((c) => row.indexOf(c));
    for (let i = 0; i < idx.length; i++) expect(idx[i]).toBeGreaterThan(-1);
    for (let i = 1; i < idx.length; i++) expect(idx[i]).toBeGreaterThan(idx[i - 1]);
    for (const dead of ["row-line", "warnbadge", "row-cost", "chip wf", "title="]) {
      expect(row).not.toContain(dead);
    }
    expect(row).toContain("fmtAgo(");
    expect(row).toContain("fmtDuration(");
    expect(src).toContain("runName(");
  });

  test("long names ellipsize and a running Run pulses", () => {
    const src = read("RunsList.vue");
    const runrow = cssRule(src, ".runrow");
    expect(runrow).toContain("grid-template-columns");
    expect(runrow).not.toContain("flex-direction: column");
    const what = cssRule(src, ".run-what");
    expect(what).toContain("white-space: nowrap");
    expect(what).toContain("overflow: hidden");
    expect(what).toContain("text-overflow: ellipsis");
    expect(cssRule(src, ".st-running .st-dot")).toContain("animation: pulse");
  });

  test("the run time stays on one line in a column that fits its longest form", () => {
    const src = read("RunsList.vue");
    const dur = cssRule(src, ".run-dur");
    expect(dur).toContain("white-space: nowrap");
    expect(dur).toContain("font-variant-numeric: tabular-nums");

    const m = cssRule(src, ".runrow").match(/grid-template-columns:\s*([^;]+);/);
    expect(m).not.toBeNull();
    const tracks = m![1].match(/minmax\([^)]*\)|\S+/g)!;

    // The other five tracks stay byte-identical.
    expect(tracks).toHaveLength(6);
    expect(tracks.slice(0, 4)).toEqual(["10px", "minmax(0, 1fr)", "auto", "auto"]);
    expect(tracks[5]).toBe("64px");

    // The timer track fits "999h 59m": 70px at 14.5px Space Grotesk with tabular digits.
    const timer = tracks[4];
    const minPx = Number(/^minmax\((\d+(?:\.\d+)?)px,\s*max-content\)$/.exec(timer)?.[1] ?? NaN);
    expect(minPx).toBeGreaterThanOrEqual(72);
  });
});

describe("refetcher", () => {
  const flush = () => new Promise((r) => setTimeout(r, 0));

  test("one trigger loads once", async () => {
    let loads = 0;
    let resolve!: () => void;
    const gate = new Promise<void>((r) => (resolve = r));
    const refetch = refetcher(async () => {
      loads++;
      await gate;
    });
    refetch();
    expect(loads).toBe(1);
    resolve();
    await flush();
    expect(loads).toBe(1);
  });

  test("triggers during a load queue exactly one more", async () => {
    let loads = 0;
    const gates: Array<() => void> = [];
    const refetch = refetcher(
      () =>
        new Promise<void>((resolve) => {
          loads++;
          gates.push(resolve);
        }),
    );
    refetch();
    refetch();
    refetch();
    refetch();
    expect(loads).toBe(1);
    gates[0]();
    await flush();
    expect(loads).toBe(2);
  });

  test("a rejected load does not block the next trigger", async () => {
    let loads = 0;
    const refetch = refetcher(async () => {
      loads++;
      if (loads === 1) throw new Error("boom");
    });
    refetch();
    await flush();
    expect(loads).toBe(1);
    refetch();
    await flush();
    expect(loads).toBe(2);
  });
});
