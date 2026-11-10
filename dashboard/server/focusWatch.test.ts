/**
 * The Focus tab watcher (ticket 237). Every spawn uses a fake `lime` shell
 * script, so no test here ever runs the real CLI; the watched tree and the
 * dashboard home are temp dirs.
 *
 * The fake records each invocation in `calls.log`, answers from per-verb
 * `.out`/`.err`/`.code`/`.sleep` files and sources an optional `<verb>.run`
 * hook (step 2 uses it), so a read's output can change between calls.
 */
import { describe, expect, test } from "bun:test";
import {
  appendFileSync,
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  DEBOUNCE_MS,
  dashboardHome,
  FocusWatcher,
  FOCUS_FILE,
  READ_GAP_MS,
  watchTargets,
} from "./focusWatch.ts";
import type {
  FocusGoal,
  HomeFocusResponse,
  ReportItem,
  StatusReport,
} from "../shared/types.ts";

/** An executable fake `lime`; keys are status, focus (list) and summarize. */
function fakeLime(dir: string): string {
  const bin = join(dir, "lime");
  writeFileSync(
    bin,
    `#!/bin/sh
D='${dir}'
echo "$* | LIME_PROJECTS=$LIME_PROJECTS" >> "$D/calls.log"
case "$1 $2" in
  "status --json") k=status;;
  "focus list") k=focus;;
  "focus summarize") k=summarize;;
  *) echo "fake lime: unexpected $*" >&2; exit 9;;
esac
[ -f "$D/$k.run" ] && . "$D/$k.run"
[ -f "$D/$k.sleep" ] && exec sleep "$(cat "$D/$k.sleep")"
[ -f "$D/$k.out" ] && cat "$D/$k.out"
[ -f "$D/$k.err" ] && cat "$D/$k.err" >&2
exit "$(cat "$D/$k.code" 2>/dev/null || echo 0)"
`,
  );
  chmodSync(bin, 0o755);
  return bin;
}

function tmpDir(prefix: string): string {
  return mkdtempSync(join(tmpdir(), prefix));
}

interface World {
  root: string;
  fakeDir: string;
  bin: string;
  registry: string;
  homeDir: string;
  projects: { name: string; path: string }[];
  calls(): string[];
  cleanup(): void;
}

/** A temp root: the fake lime, a temp home with focus.md, and project trees. */
function makeWorld(names: string[] = ["alpha", "beta"]): World {
  const root = tmpDir("focus-watch-");
  const fakeDir = join(root, "bin");
  mkdirSync(fakeDir, { recursive: true });
  const bin = fakeLime(fakeDir);
  const registry = join(root, "projects");
  const projects = names.map((name) => {
    const path = join(root, name);
    mkdirSync(join(path, ".lime", "tickets"), { recursive: true });
    mkdirSync(join(path, ".lime", "runs"), { recursive: true });
    mkdirSync(join(path, "docs", "adr"), { recursive: true });
    return { name, path };
  });
  writeFileSync(registry, `# repos\n${projects.map((p) => p.path).join("\n")}\n`);
  const homeDir = join(root, "home");
  mkdirSync(homeDir, { recursive: true });
  writeFileSync(join(homeDir, FOCUS_FILE), "# focus\n");
  return {
    root,
    fakeDir,
    bin,
    registry,
    homeDir,
    projects,
    calls() {
      const path = join(fakeDir, "calls.log");
      return existsSync(path)
        ? readFileSync(path, "utf8").split("\n").filter(Boolean)
        : [];
    },
    cleanup() {
      rmSync(root, { recursive: true, force: true });
    },
  };
}

function item(over: Partial<ReportItem> = {}): ReportItem {
  return {
    group: "needs_you",
    reason: "ticket",
    project: null,
    ticket: null,
    title: null,
    run_id: null,
    branch: null,
    since: null,
    text: null,
    cause: null,
    count: null,
    kind: null,
    focus: null,
    actions: [],
    pr: null,
    note: null,
    summary: null,
    summary_state: null,
    ...over,
  };
}

function report(items: ReportItem[]): StatusReport {
  return {
    generated_at: "2026-01-01T00:00:00.000Z",
    items,
    home: { ahead: 0, push_error: null, last_visit: null },
    notes: [],
  };
}

/** Write the two outputs a healthy read answers with. */
function seedRead(world: World, items: ReportItem[] = []): void {
  writeFileSync(join(world.fakeDir, "status.out"), JSON.stringify(report(items)));
  writeFileSync(join(world.fakeDir, "focus.out"), JSON.stringify([] as FocusGoal[]));
}

async function waitFor(cond: () => boolean, ms: number): Promise<void> {
  const deadline = Date.now() + ms;
  for (;;) {
    if (cond()) return;
    if (Date.now() > deadline) throw new Error("waitFor timed out");
    await Bun.sleep(25);
  }
}

/** Recursive listing of a tree, for the "watches write nothing" case. */
function listTree(dir: string): string[] {
  const out: string[] = [];
  const walk = (current: string, prefix: string): void => {
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      out.push(entry.isDirectory() ? `${rel}/` : rel);
      if (entry.isDirectory()) walk(join(current, entry.name), rel);
    }
  };
  walk(dir, "");
  return out.sort();
}

function opts(world: World) {
  return {
    projects: world.projects,
    homeDir: world.homeDir,
    bin: world.bin,
    registry: world.registry,
    log: (_line: string) => {},
  };
}

test("DEBOUNCE_MS and READ_GAP_MS are the documented constants", () => {
  expect(DEBOUNCE_MS).toBe(500);
  expect(READ_GAP_MS).toBe(10_000);
});

test("dashboardHome joins HOME with .lime/dashboard", () => {
  expect(dashboardHome()).toBe(join(process.env.HOME ?? "", ".lime", "dashboard"));
});

describe("watchTargets", () => {
  test("returns the three dirs per project plus the home target", () => {
    const world = makeWorld(["alpha"]);
    try {
      const targets = watchTargets(world.projects, world.homeDir);
      const alpha = world.projects[0];
      expect(targets.map((t) => t.dir)).toEqual([
        join(alpha.path, ".lime", "tickets"),
        join(alpha.path, ".lime", "runs"),
        join(alpha.path, "docs", "adr"),
        world.homeDir,
      ]);
      const byDir = new Map(targets.map((t) => [t.dir, t]));
      const tickets = byDir.get(join(alpha.path, ".lime", "tickets"))!;
      const runs = byDir.get(join(alpha.path, ".lime", "runs"))!;
      const home = byDir.get(world.homeDir)!;

      expect(tickets.project).toBe("alpha");
      expect(tickets.match("240-x.md")).toBe(true);
      expect(tickets.match("notes.txt")).toBe(false);
      expect(tickets.match("last-visit")).toBe(false);

      expect(runs.match("lime.db")).toBe(true);
      expect(runs.match("lime.db-wal")).toBe(true);
      expect(runs.match("lime.db-shm")).toBe(false);
      expect(runs.match("focus.md")).toBe(false);

      expect(home.project).toBeNull();
      expect(home.match("focus.md")).toBe(true);
      expect(home.match("240-x.md")).toBe(false);
      expect(home.match("last-visit")).toBe(false);
    } finally {
      world.cleanup();
    }
  });
});

describe("FocusWatcher", () => {
  test(
    "start runs one catch-up read and publishes it",
    async () => {
      const world = makeWorld();
      const got: HomeFocusResponse[] = [];
      const watcher = new FocusWatcher(opts(world));
      try {
        seedRead(world, [item({ project: "alpha", ticket: "227-x" })]);
        watcher.subscribe((res) => got.push(res));
        watcher.start();
        await waitFor(() => got.length >= 1, 5000);

        const status = world.calls().filter((l) => l.includes("status --json"));
        const focus = world.calls().filter((l) => l.includes("focus list --json"));
        expect(status).toHaveLength(1);
        expect(focus).toHaveLength(1);
        expect(status[0]).toContain(`LIME_PROJECTS=${world.registry}`);
        expect(focus[0]).toContain(`LIME_PROJECTS=${world.registry}`);
        expect(got[0].report?.items[0].ticket).toBe("227-x");
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "a new ticket file reaches a subscriber within 5 s",
    async () => {
      const world = makeWorld(["alpha"]);
      const got: HomeFocusResponse[] = [];
      // The production gap (10 s) applies; the ticket's shorter debounce path
      // must still deliver inside the 5 s deadline, so this overrides nothing.
      const watcher = new FocusWatcher(opts(world));
      try {
        seedRead(world);
        watcher.subscribe((res) => got.push(res));
        watcher.start();
        await waitFor(() => got.length >= 1, 5000);

        writeFileSync(
          join(world.fakeDir, "status.out"),
          JSON.stringify(report([item({ project: "alpha", ticket: "240-new" })])),
        );
        const writeTime = Date.now();
        writeFileSync(
          join(world.projects[0].path, ".lime", "tickets", "240-new.md"),
          "# 240\n",
        );

        await waitFor(
          () => got.some((r) => r.report?.items.some((i) => i.ticket === "240-new")),
          5000,
        );
        expect(Date.now() - writeTime).toBeLessThan(5000);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "a burst at the default gap coalesces into one read",
    async () => {
      const world = makeWorld(["alpha"]);
      const watcher = new FocusWatcher(opts(world));
      try {
        seedRead(world);
        watcher.start();
        const wal = join(world.projects[0].path, ".lime", "runs", "lime.db-wal");
        for (let i = 0; i < 40; i += 1) {
          appendFileSync(wal, "x");
          await Bun.sleep(50);
        }
        await Bun.sleep(1000);
        const status = world.calls().filter((l) => l.includes("status --json"));
        expect(status).toHaveLength(1);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    15_000,
  );

  test(
    "reads never overlap and start at least the gap apart",
    async () => {
      const world = makeWorld(["alpha"]);
      const starts: number[] = [];
      const ends: number[] = [];
      const empty: HomeFocusResponse = { report: null, goals: null, errors: [] };
      const watcher = new FocusWatcher({
        ...opts(world),
        debounceMs: 50,
        gapMs: 600,
        read: async () => {
          starts.push(Date.now());
          await Bun.sleep(200);
          ends.push(Date.now());
          return empty;
        },
      });
      try {
        watcher.start();
        const wal = join(world.projects[0].path, ".lime", "runs", "lime.db-wal");
        const until = Date.now() + 2500;
        while (Date.now() < until) {
          appendFileSync(wal, "x");
          await Bun.sleep(50);
        }
        await Bun.sleep(700);

        for (let i = 1; i < starts.length; i += 1) {
          expect(starts[i]).toBeGreaterThanOrEqual(ends[i - 1]);
          expect(starts[i] - starts[i - 1]).toBeGreaterThanOrEqual(585);
        }
        expect(starts.length).toBeGreaterThanOrEqual(2);
        expect(starts.length).toBeLessThanOrEqual(7);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    15_000,
  );

  test(
    "refresh joins the watcher's single-flight and reuses a fresh read",
    async () => {
      const world = makeWorld(["alpha"]);
      let reads = 0;
      const empty: HomeFocusResponse = {
        report: report([]),
        goals: [],
        errors: [],
      };
      const watcher = new FocusWatcher({
        ...opts(world),
        // Keep the scheduled catch-up read out of the way; refresh drives here.
        debounceMs: 10_000,
        read: async () => {
          reads += 1;
          await Bun.sleep(150);
          return empty;
        },
      });
      try {
        watcher.start();
        // Three concurrent mount/reconnect fetches share one read.
        const [a, b, c] = await Promise.all([
          watcher.refresh(),
          watcher.refresh(),
          watcher.refresh(),
        ]);
        expect(reads).toBe(1);
        expect(a).toBe(empty);
        expect(b).toBe(empty);
        expect(c).toBe(empty);
        // A fetch inside the read gap reuses the last result, spawning nothing.
        const again = await watcher.refresh();
        expect(again).toBe(empty);
        expect(reads).toBe(1);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "writing -shm and unrelated files triggers no read",
    async () => {
      const world = makeWorld(["alpha"]);
      const got: HomeFocusResponse[] = [];
      const watcher = new FocusWatcher({ ...opts(world), gapMs: 300 });
      try {
        seedRead(world);
        watcher.subscribe((res) => got.push(res));
        watcher.start();
        await waitFor(() => got.length >= 1, 5000);
        const before = got.length;

        writeFileSync(
          join(world.projects[0].path, ".lime", "runs", "lime.db-shm"),
          "shm",
        );
        writeFileSync(
          join(world.projects[0].path, ".lime", "tickets", "notes.txt"),
          "hi",
        );
        await Bun.sleep(1500);
        expect(got.length).toBe(before);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "a focus.md rewrite in the home triggers a read",
    async () => {
      const world = makeWorld(["alpha"]);
      const got: HomeFocusResponse[] = [];
      const watcher = new FocusWatcher({ ...opts(world), gapMs: 300 });
      try {
        seedRead(world);
        watcher.subscribe((res) => got.push(res));
        watcher.start();
        await waitFor(() => got.length >= 1, 5000);
        const before = got.length;

        writeFileSync(join(world.homeDir, FOCUS_FILE), "# changed\n");
        await waitFor(() => got.length > before, 5000);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "watching writes nothing",
    async () => {
      const world = makeWorld(["alpha"]);
      const watcher = new FocusWatcher({ ...opts(world), gapMs: 300 });
      try {
        seedRead(world);
        const projectBefore = listTree(world.projects[0].path);
        const homeBefore = listTree(world.homeDir);
        const focusBefore = readFileSync(join(world.homeDir, FOCUS_FILE), "utf8");

        watcher.start();
        await Bun.sleep(1500);
        watcher.stop();

        expect(listTree(world.projects[0].path)).toEqual(projectBefore);
        expect(listTree(world.homeDir)).toEqual(homeBefore);
        expect(readFileSync(join(world.homeDir, FOCUS_FILE), "utf8")).toBe(focusBefore);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "a missing project folder is logged once and skipped",
    async () => {
      const world = makeWorld(["alpha"]);
      const logs: string[] = [];
      const got: HomeFocusResponse[] = [];
      world.projects.push({ name: "ghost", path: join(world.root, "ghost") });
      const watcher = new FocusWatcher({
        ...opts(world),
        gapMs: 300,
        log: (line) => logs.push(line),
      });
      try {
        seedRead(world);
        watcher.subscribe((res) => got.push(res));
        expect(() => watcher.start()).not.toThrow();

        const ghosts = logs.filter(
          (l) => l.includes("ghost") && l.includes("skipped"),
        );
        expect(ghosts).toHaveLength(1);

        await waitFor(() => got.length >= 1, 5000);
        const before = got.length;
        writeFileSync(
          join(world.projects[0].path, ".lime", "tickets", "241-y.md"),
          "# 241\n",
        );
        await waitFor(() => got.length > before, 5000);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "a failed read publishes its error and the watcher keeps watching",
    async () => {
      const world = makeWorld(["alpha"]);
      const logs: string[] = [];
      const got: HomeFocusResponse[] = [];
      const watcher = new FocusWatcher({
        ...opts(world),
        gapMs: 300,
        log: (line) => logs.push(line),
      });
      try {
        writeFileSync(join(world.fakeDir, "status.code"), "1");
        writeFileSync(join(world.fakeDir, "status.err"), "boom");
        writeFileSync(join(world.fakeDir, "focus.out"), "[]");
        watcher.subscribe((res) => got.push(res));
        watcher.start();

        await waitFor(() => got.length >= 1, 5000);
        expect(got[0].errors).toEqual(["lime status --json failed (exit 1): boom"]);
        expect(logs).toContain("lime status --json failed (exit 1): boom");

        rmSync(join(world.fakeDir, "status.code"));
        rmSync(join(world.fakeDir, "status.err"));
        writeFileSync(
          join(world.fakeDir, "status.out"),
          JSON.stringify(report([item({ project: "alpha", ticket: "241-ok" })])),
        );
        writeFileSync(
          join(world.projects[0].path, ".lime", "tickets", "241-ok.md"),
          "# 241\n",
        );
        await waitFor(
          () => got.some((r) => r.report?.items.some((i) => i.ticket === "241-ok")),
          5000,
        );
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "after stop a ticket write triggers no read",
    async () => {
      const world = makeWorld(["alpha"]);
      const got: HomeFocusResponse[] = [];
      const watcher = new FocusWatcher({ ...opts(world), gapMs: 300 });
      try {
        seedRead(world);
        watcher.subscribe((res) => got.push(res));
        watcher.start();
        await waitFor(() => got.length >= 1, 5000);
        watcher.stop();
        const before = got.length;

        writeFileSync(
          join(world.projects[0].path, ".lime", "tickets", "242-z.md"),
          "# 242\n",
        );
        await Bun.sleep(1500);
        expect(got.length).toBe(before);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );
});

describe("summaries", () => {
  test(
    "summarizes each pending project once on the catch-up read",
    async () => {
      const world = makeWorld();
      const watcher = new FocusWatcher({ ...opts(world), gapMs: 300 });
      try {
        seedRead(world, [
          item({ project: "alpha", ticket: "237-a", summary_state: "pending" }),
          item({ project: "alpha", ticket: "237-b", summary_state: null }),
          item({ project: "beta", ticket: "237-c", summary_state: "pending" }),
        ]);
        watcher.start();
        await waitFor(
          () =>
            world.calls().some((l) => l.includes("focus summarize --project beta")),
          5000,
        );
        const alpha = world
          .calls()
          .filter((l) => l.includes("focus summarize --project alpha"));
        const beta = world
          .calls()
          .filter((l) => l.includes("focus summarize --project beta"));
        expect(alpha).toHaveLength(1);
        expect(beta).toHaveLength(1);
        expect(alpha[0]).toContain(`LIME_PROJECTS=${world.registry}`);
        expect(beta[0]).toContain(`LIME_PROJECTS=${world.registry}`);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "runs one summarize per project at a time",
    async () => {
      const world = makeWorld(["alpha"]);
      const watcher = new FocusWatcher({ ...opts(world), gapMs: 300 });
      try {
        seedRead(world, [
          item({ project: "alpha", ticket: "237-a", summary_state: "pending" }),
        ]);
        writeFileSync(join(world.fakeDir, "summarize.sleep"), "1.5");
        watcher.start();
        await waitFor(
          () =>
            world.calls().some((l) => l.includes("focus summarize --project alpha")),
          5000,
        );
        const wal = join(world.projects[0].path, ".lime", "runs", "lime.db-wal");
        for (let i = 0; i < 5; i += 1) {
          appendFileSync(wal, "x");
          await Bun.sleep(240);
        }
        const during = world
          .calls()
          .filter((l) => l.includes("focus summarize --project alpha"));
        expect(during).toHaveLength(1);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    15_000,
  );

  test(
    "a change that brings a pending item summarizes on change",
    async () => {
      const world = makeWorld(["alpha"]);
      const watcher = new FocusWatcher({ ...opts(world), gapMs: 300 });
      try {
        seedRead(world);
        watcher.start();
        await waitFor(
          () => world.calls().some((l) => l.includes("status --json")),
          5000,
        );
        expect(world.calls().filter((l) => l.includes("focus summarize"))).toHaveLength(0);

        writeFileSync(
          join(world.fakeDir, "status.out"),
          JSON.stringify(
            report([
              item({ project: "alpha", ticket: "237-a", summary_state: "pending" }),
            ]),
          ),
        );
        writeFileSync(
          join(world.projects[0].path, ".lime", "tickets", "237-a.md"),
          "# 237\n",
        );
        await waitFor(
          () =>
            world
              .calls()
              .filter((l) => l.includes("focus summarize --project alpha"))
              .length === 1,
          5000,
        );
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "the summary text lands in a published report",
    async () => {
      const world = makeWorld(["alpha"]);
      const got: HomeFocusResponse[] = [];
      const watcher = new FocusWatcher({ ...opts(world), gapMs: 300 });
      try {
        seedRead(world, [
          item({ project: "alpha", ticket: "237-a", summary_state: "pending" }),
        ]);
        const ok = report([
          item({
            project: "alpha",
            ticket: "237-a",
            summary_state: "ok",
            summary: "Deploy it now.",
          }),
        ]);
        writeFileSync(
          join(world.fakeDir, "status-ok.json"),
          JSON.stringify(ok),
        );
        const wal = join(world.projects[0].path, ".lime", "runs", "lime.db-wal");
        writeFileSync(
          join(world.fakeDir, "summarize.run"),
          `cp "$D/status-ok.json" "$D/status.out"\nprintf x >> "${wal}"\n`,
        );
        watcher.subscribe((res) => got.push(res));
        watcher.start();
        await waitFor(
          () =>
            got.some((r) =>
              r.report?.items.some((i) => i.summary === "Deploy it now."),
            ),
          5000,
        );
        expect(world.calls().filter((l) => l.includes("focus summarize"))).toHaveLength(1);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "a non-zero summarize exit logs one line and the watcher keeps running",
    async () => {
      const world = makeWorld(["alpha"]);
      const logs: string[] = [];
      const got: HomeFocusResponse[] = [];
      const watcher = new FocusWatcher({
        ...opts(world),
        gapMs: 300,
        log: (line) => logs.push(line),
      });
      try {
        seedRead(world, [
          item({ project: "alpha", ticket: "237-a", summary_state: "pending" }),
        ]);
        writeFileSync(join(world.fakeDir, "summarize.code"), "1");
        writeFileSync(join(world.fakeDir, "summarize.err"), "failed alpha 237: auth");
        watcher.subscribe((res) => got.push(res));
        watcher.start();
        await waitFor(
          () =>
            logs.some((l) => l.includes("focus summarize --project alpha")),
          5000,
        );
        expect(logs).toContain(
          "lime focus summarize --project alpha failed (exit 1): failed alpha 237: auth",
        );

        const before = got.length;
        writeFileSync(
          join(world.projects[0].path, ".lime", "tickets", "238-b.md"),
          "# 238\n",
        );
        await waitFor(() => got.length > before, 5000);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "a summarize timeout logs one line and frees the slot",
    async () => {
      const world = makeWorld(["alpha"]);
      const logs: string[] = [];
      const watcher = new FocusWatcher({
        ...opts(world),
        gapMs: 300,
        summarizeTimeoutMs: 300,
        log: (line) => logs.push(line),
      });
      try {
        seedRead(world, [
          item({ project: "alpha", ticket: "237-a", summary_state: "pending" }),
        ]);
        writeFileSync(join(world.fakeDir, "summarize.sleep"), "5");
        watcher.start();
        await waitFor(
          () =>
            logs.includes(
              "lime focus summarize --project alpha timed out after 0.3 s",
            ),
          5000,
        );
        rmSync(join(world.fakeDir, "summarize.sleep"));
        const before = world
          .calls()
          .filter((l) => l.includes("focus summarize --project alpha")).length;
        appendFileSync(
          join(world.projects[0].path, ".lime", "runs", "lime.db-wal"),
          "x",
        );
        await waitFor(
          () =>
            world
              .calls()
              .filter((l) => l.includes("focus summarize --project alpha"))
              .length > before,
          5000,
        );
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );

  test(
    "a pending item for a missing project is skipped with a log line",
    async () => {
      const world = makeWorld(["alpha"]);
      const logs: string[] = [];
      world.projects.push({ name: "ghost", path: join(world.root, "ghost") });
      const watcher = new FocusWatcher({
        ...opts(world),
        gapMs: 300,
        log: (line) => logs.push(line),
      });
      try {
        seedRead(world, [
          item({ project: "ghost", ticket: "237-g", summary_state: "pending" }),
        ]);
        watcher.start();
        await waitFor(
          () =>
            logs.some(
              (l) => l.includes("focus summarize: ghost") && l.includes("skipped"),
            ),
          5000,
        );
        expect(world.calls().some((l) => l.includes("--project ghost"))).toBe(false);
      } finally {
        watcher.stop();
        world.cleanup();
      }
    },
    10_000,
  );
});

