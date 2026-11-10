/**
 * The home proxies (ticket 227). Every spawn uses a fake `lime` shell script,
 * so the owner's real home under `~/.lime` is never touched — no test here ever
 * runs the real `lime dash visit`, `lime status` or `lime focus`.
 *
 * The fake records each invocation in `calls.log` and answers from per-verb
 * `.out`/`.err`/`.code`/`.sleep` files, which lets us pin failures, slow spawns
 * and unspawnable binaries without a real CLI.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import {
  failureLine,
  homeFocus,
  homeTick,
  homeVisit,
  runLime,
  SPAWN_TIMEOUT_MS,
  STATUS_TIMEOUT_MS,
  SUMMARIZE_TIMEOUT_MS,
  tickArgs,
} from "./limeProxy.ts";
import type { FocusGoal, StatusReport } from "../shared/types.ts";

interface SpawnSpec {
  out?: string;
  err?: string;
  code?: number;
  sleep?: number;
}

type FakeSpec = Partial<Record<"status" | "focus" | "visit" | "fdone" | "fedit" | "tclose" | "treopen", SpawnSpec>>;

/** Write an executable fake `lime` and its per-verb answer files. */
function fakeLime(dir: string, spec: FakeSpec = {}): string {
  const bin = join(dir, "lime");
  writeFileSync(
    bin,
    `#!/bin/sh
D='${dir}'
echo "$* | LIME_PROJECTS=$LIME_PROJECTS" >> "$D/calls.log"
case "$1 $2" in "status --json") k=status;; "focus list") k=focus;; "dash visit") k=visit;; "focus done") k=fdone;; "focus edit") k=fedit;; "ticket close") k=tclose;; "ticket reopen") k=treopen;; *) echo "fake lime: unexpected $*" >&2; exit 9;; esac
[ -f "$D/$k.sleep" ] && exec sleep "$(cat "$D/$k.sleep")"
[ -f "$D/$k.out" ] && cat "$D/$k.out"
[ -f "$D/$k.err" ] && cat "$D/$k.err" >&2
exit "$(cat "$D/$k.code" 2>/dev/null || echo 0)"
`,
  );
  chmodSync(bin, 0o755);
  for (const k of ["status", "focus", "visit", "fdone", "fedit", "tclose", "treopen"] as const) {
    const s = spec[k];
    if (!s) continue;
    if (s.out !== undefined) writeFileSync(join(dir, `${k}.out`), s.out);
    if (s.err !== undefined) writeFileSync(join(dir, `${k}.err`), s.err);
    if (s.code !== undefined) writeFileSync(join(dir, `${k}.code`), String(s.code));
    if (s.sleep !== undefined) writeFileSync(join(dir, `${k}.sleep`), String(s.sleep));
  }
  return bin;
}

const STATUS: StatusReport = {
  generated_at: "2026-01-01T00:00:00.000Z",
  items: [
    {
      group: "needs_you",
      reason: "ticket",
      project: "alpha",
      ticket: "227-focus-tab",
      title: "Focus tab",
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
    },
    {
      group: "done",
      reason: "merged",
      project: "alpha",
      ticket: "220-older",
      title: "Older",
      run_id: "r1",
      branch: null,
      since: "2025-12-01T00:00:00.000Z",
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
    },
  ],
  home: { ahead: 0, push_error: null, last_visit: null },
  notes: [],
};

const GOALS: FocusGoal[] = [
  {
    id: "f1",
    title: "Ship the tab",
    due: "2026-02-01",
    project: "alpha",
    done: false,
    group: "today",
    overdue: false,
  },
  {
    id: "f2",
    title: "Write docs",
    due: null,
    project: null,
    done: false,
    group: "upcoming",
    overdue: false,
  },
];

function tmpDir(prefix: string): string {
  return mkdtempSync(join(tmpdir(), prefix));
}

describe("limeProxy", () => {
  test("timeouts are the documented constants", () => {
    expect(STATUS_TIMEOUT_MS).toBe(60_000);
    expect(SPAWN_TIMEOUT_MS).toBe(30_000);
    expect(SUMMARIZE_TIMEOUT_MS).toBe(600_000);
  });

  test("runLime passes the args verbatim and LIME_PROJECTS from opts.registry", async () => {
    const dir = tmpDir("lime-proxy-args-");
    const bin = fakeLime(dir, { status: { out: JSON.stringify(STATUS) } });
    try {
      const run = await runLime(["status", "--json"], STATUS_TIMEOUT_MS, {
        bin,
        registry: "/tmp/registry-x",
      });
      expect(run.exitCode).toBe(0);
      expect(run.spawnError).toBeNull();
      const log = readFileSync(join(dir, "calls.log"), "utf8").trim();
      expect(log).toBe("status --json | LIME_PROJECTS=/tmp/registry-x");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("homeFocus parses both spawns", async () => {
    const dir = tmpDir("lime-proxy-ok-");
    const bin = fakeLime(dir, {
      status: { out: JSON.stringify(STATUS) },
      focus: { out: JSON.stringify(GOALS) },
    });
    try {
      const res = await homeFocus({ bin });
      expect(res.errors).toEqual([]);
      expect(res.report?.items).toEqual(STATUS.items);
      expect(res.goals).toEqual(GOALS);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("a failing status keeps the goals and gives one line", async () => {
    const dir = tmpDir("lime-proxy-statusfail-");
    const bin = fakeLime(dir, {
      status: { err: "boom\nmore", code: 1 },
      focus: { out: JSON.stringify(GOALS) },
    });
    try {
      const res = await homeFocus({ bin });
      expect(res.report).toBeNull();
      expect(res.goals).toEqual(GOALS);
      expect(res.errors).toEqual(["lime status --json failed (exit 1): boom"]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("non-JSON focus output is one line", async () => {
    const dir = tmpDir("lime-proxy-focusbad-");
    const bin = fakeLime(dir, {
      status: { out: JSON.stringify(STATUS) },
      focus: { out: "not json at all" },
    });
    try {
      const res = await homeFocus({ bin });
      expect(res.goals).toBeNull();
      expect(res.errors).toEqual(["lime focus list --json printed no JSON"]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("a slow spawn times out without waiting for its pipes", async () => {
    const dir = tmpDir("lime-proxy-slow-");
    const bin = fakeLime(dir, {
      status: { sleep: 5 },
      focus: { out: JSON.stringify(GOALS) },
    });
    try {
      const started = Date.now();
      const res = await homeFocus({ bin, statusTimeoutMs: 200 });
      const elapsed = Date.now() - started;
      expect(res.errors).toEqual(["lime status --json timed out after 0.2 s"]);
      expect(res.report).toBeNull();
      expect(res.goals).toEqual(GOALS);
      expect(elapsed).toBeLessThan(2000);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("an unspawnable binary is one line", async () => {
    const dir = tmpDir("lime-proxy-missing-");
    const bin = join(dir, "no-such-lime");
    try {
      const res = await homeFocus({ bin });
      expect(res.report).toBeNull();
      expect(res.errors[0]).toMatch(/^lime status --json (could not start|failed)/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("failureLine renders each failure shape", () => {
    const base = { stdout: "", stderr: "", timedOut: false, spawnError: null };
    expect(failureLine("x", { ...base, exitCode: 0 }, 1000)).toBeNull();
    expect(
      failureLine("x", { ...base, exitCode: null, spawnError: "nope" }, 1000),
    ).toBe("x could not start: nope");
    expect(
      failureLine("x", { ...base, exitCode: null, timedOut: true }, 1000),
    ).toBe("x timed out after 1 s");
    expect(
      failureLine("x", { ...base, exitCode: 3, stderr: "\n  hi \nthere" }, 1000),
    ).toBe("x failed (exit 3): hi");
    expect(failureLine("x", { ...base, exitCode: 3 }, 1000)).toBe(
      "x failed (exit 3)",
    );
  });
});

describe("homeVisit", () => {
  test("exit 0 reports ok", async () => {
    const dir = tmpDir("lime-visit-ok-");
    const bin = fakeLime(dir, {});
    try {
      expect(await homeVisit({ bin })).toEqual({
        ok: true,
        home_missing: false,
        message: null,
      });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("a missing home repo is flagged for init", async () => {
    const dir = tmpDir("lime-visit-missing-");
    const bin = fakeLime(dir, {
      visit: {
        code: 1,
        err: "dashboard: no home repo at /x — run lime dash init",
      },
    });
    try {
      const res = await homeVisit({ bin });
      expect(res.home_missing).toBe(true);
      expect(res.ok).toBe(false);
      expect(res.message).toContain("run lime dash init");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("a refusal is not a missing home", async () => {
    const dir = tmpDir("lime-visit-refused-");
    const bin = fakeLime(dir, {
      visit: {
        code: 2,
        err: "lime dash visit: owner-only; refused inside a Role session",
      },
    });
    try {
      const res = await homeVisit({ bin });
      expect(res.home_missing).toBe(false);
      expect(res.message).toBe(
        "lime dash visit failed (exit 2): lime dash visit: owner-only; refused inside a Role session",
      );
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("tickArgs", () => {
  test("maps the four tick commands exactly", () => {
    expect(tickArgs({ kind: "goal", id: "f1", done: true })).toEqual(["focus", "done", "f1"]);
    expect(tickArgs({ kind: "goal", id: "f1", done: false })).toEqual([
      "focus",
      "edit",
      "f1",
      "--reopen",
    ]);
    expect(tickArgs({ kind: "ticket", project: "alpha", number: "227", done: true })).toEqual([
      "ticket",
      "close",
      "alpha",
      "227",
    ]);
    expect(tickArgs({ kind: "ticket", project: "alpha", number: "227", done: false })).toEqual([
      "ticket",
      "reopen",
      "alpha",
      "227",
    ]);
  });

  test("returns null for every bad shape", () => {
    expect(tickArgs({ kind: "goal", id: "--help", done: true })).toBeNull();
    expect(tickArgs({ kind: "goal", id: "f1x", done: true })).toBeNull();
    expect(tickArgs({ kind: "ticket", project: "-x", number: "227", done: true })).toBeNull();
    expect(tickArgs({ kind: "ticket", project: "../a", number: "227", done: true })).toBeNull();
    expect(tickArgs({ kind: "ticket", project: "alpha", number: "12a", done: true })).toBeNull();
    expect(tickArgs({ kind: "goal", id: "f1", done: "yes" })).toBeNull();
    expect(tickArgs({ kind: "other", done: true })).toBeNull();
    expect(tickArgs("nope")).toBeNull();
    expect(tickArgs(null)).toBeNull();
  });
});

describe("homeTick", () => {
  test("exit 0 is ok and runs the registry-carrying command", async () => {
    const dir = tmpDir("lime-tick-ok-");
    const bin = fakeLime(dir, {});
    try {
      expect(await homeTick({ kind: "goal", id: "f1", done: true }, { bin, registry: "/tmp/reg" })).toEqual({
        ok: true,
        message: null,
      });
      const log = readFileSync(join(dir, "calls.log"), "utf8").trim();
      expect(log).toBe("focus done f1 | LIME_PROJECTS=/tmp/reg");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("a non-zero exit shows the stderr line", async () => {
    const dir = tmpDir("lime-tick-fail-");
    const bin = fakeLime(dir, { fedit: { code: 1, err: "lime focus: no row f9\n" } });
    try {
      expect(await homeTick({ kind: "goal", id: "f9", done: false }, { bin })).toEqual({
        ok: false,
        message: "lime focus: no row f9",
      });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  test("an unspawnable bin is caught, never thrown", async () => {
    const dir = tmpDir("lime-tick-missing-");
    const bin = join(dir, "no-such-lime");
    try {
      const res = await homeTick({ kind: "goal", id: "f1", done: true }, { bin });
      expect(res.ok).toBe(false);
      expect(res.message).toContain("could not start");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("home routes over HTTP", () => {
  let proc: Bun.Subprocess<"ignore", "ignore", "pipe">;
  let port = 0;
  let tmp = "";
  let fakeDir = "";

  beforeAll(async () => {
    tmp = mkdtempSync(join(tmpdir(), "lime-home-routes-"));
    fakeDir = join(tmp, "bin");
    mkdirSync(fakeDir, { recursive: true });
    fakeLime(fakeDir, {
      status: { out: JSON.stringify(STATUS) },
      focus: { out: JSON.stringify(GOALS) },
      fedit: { code: 1, err: "lime focus: no row f9\n" },
    });

    const registryFile = join(tmp, "projects");
    const project = join(tmp, "alpha");
    mkdirSync(project, { recursive: true });
    mkdirSync(join(project, ".lime", "tickets"), { recursive: true });
    writeFileSync(registryFile, `# repos registered by lime init\n${project}\n`);

    const probe = Bun.serve({
      port: 0,
      hostname: "127.0.0.1",
      fetch: () => new Response(),
    });
    if (probe.port === undefined) throw new Error("probe server bound no TCP port");
    port = probe.port;
    probe.stop(true);

    const env: Record<string, string | undefined> = {
      ...process.env,
      LIME_PORT: String(port),
      LIME_PROJECTS: registryFile,
      LIME_BIN: join(fakeDir, "lime"),
      HOME: tmp,
    };
    delete env.PORT;

    proc = Bun.spawn([process.execPath, "run", "server/index.ts"], {
      cwd: resolve(import.meta.dir, ".."),
      env,
      stdout: "ignore",
      stderr: "pipe",
    });

    const deadline = Date.now() + 10_000;
    for (;;) {
      try {
        const res = await fetch(`http://127.0.0.1:${port}/api/health`);
        if (res.ok) break;
      } catch {
        // not up yet
      }
      if (Date.now() > deadline) {
        const err = await new Response(proc.stderr).text();
        throw new Error(`dashboard server did not start:\n${err}`);
      }
      await Bun.sleep(100);
    }
  }, 15_000);

  afterAll(async () => {
    proc.kill();
    await proc.exited;
    rmSync(tmp, { recursive: true, force: true });
  });

  test("POST /api/home/visit records a visit", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/home/visit`, {
      method: "POST",
      headers: { origin: `http://127.0.0.1:${port}` },
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, home_missing: false, message: null });
  });

  test("GET /api/home/visit is 405", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/home/visit`);
    expect(res.status).toBe(405);
  });

  test("a foreign Origin on the visit POST is refused", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/home/visit`, {
      method: "POST",
      headers: { origin: "http://evil.example" },
    });
    expect(res.status).toBe(403);
  });

  test("GET /api/home/focus returns the report and goals", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/home/focus`);
    expect(res.status).toBe(200);
    const body = (await res.json()) as {
      report: StatusReport;
      goals: FocusGoal[];
      errors: string[];
    };
    expect(body.report.items).toEqual(STATUS.items);
    expect(body.goals).toEqual(GOALS);
    expect(body.errors).toEqual([]);
  });

  test("calls.log holds one visit plus the registry-carrying reads", () => {
    const lines = readFileSync(join(fakeDir, "calls.log"), "utf8")
      .split("\n")
      .filter((l) => l.length > 0);
    const visits = lines.filter((l) => l.includes("dash visit"));
    expect(visits).toHaveLength(1);

    const statusLine = lines.find((l) => l.includes("status --json"));
    const focusLine = lines.find((l) => l.includes("focus list --json"));
    expect(statusLine).toContain("LIME_PROJECTS=");
    expect(focusLine).toContain("LIME_PROJECTS=");
    // The server passes registry.registryPath, the resolved registry file.
    expect(statusLine).toContain(`LIME_PROJECTS=${join(tmp, "projects")}`);
    expect(focusLine).toContain(`LIME_PROJECTS=${join(tmp, "projects")}`);
  });

  test("GET /api/live opens an sse stream and sends running", async () => {
    const ctrl = new AbortController();
    const res = await fetch(`http://127.0.0.1:${port}/api/live`, {
      signal: ctrl.signal,
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("text/event-stream");
    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    const first = await reader.read();
    const firstText = decoder.decode(first.value);
    expect(firstText).toContain("retry:");
    const deadline = Date.now() + 3_000;
    let buffer = firstText;
    let found = buffer.includes("event: running");
    while (Date.now() < deadline && !found) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.includes("event: running")) found = true;
    }
    ctrl.abort();
    await reader.cancel().catch(() => {});
    expect(found).toBe(true);
  }, 8_000);

  test("a foreign Origin on /api/live is refused", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/live`, {
      headers: { origin: "http://evil.example" },
    });
    expect(res.status).toBe(403);
  });

  test("a foreign Host on /api/live is refused", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/live`, {
      headers: { host: `evil.example:${port}` },
    });
    expect(res.status).toBe(403);
  });

  test("POST /api/live is 405", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/live`, {
      method: "POST",
    });
    expect(res.status).toBe(405);
  });

  test("GET /api/home/stream is gone", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/home/stream`);
    expect(res.status).toBe(404);
  });

  test("a malformed run= on /api/live is a 400", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/live?run=alpha:x`);
    expect(res.status).toBe(400);
  });

  test(
    "a new ticket in a registered project reaches the open /api/live stream",
    async () => {
      const ctrl = new AbortController();
      const res = await fetch(`http://127.0.0.1:${port}/api/live`, {
        signal: ctrl.signal,
      });
      expect(res.status).toBe(200);
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();

      // The next read answers with an item for the new ticket.
      writeFileSync(
        join(fakeDir, "status.out"),
        JSON.stringify({
          ...STATUS,
          items: [
            {
              ...STATUS.items[0],
              ticket: "241-stream",
              title: "Stream",
              summary: null,
              summary_state: null,
            },
          ],
        }),
      );
      writeFileSync(
        join(tmp, "alpha", ".lime", "tickets", "241-stream.md"),
        "x",
      );

      const deadline = Date.now() + 4_500;
      let buffer = "";
      let found = false;
      while (Date.now() < deadline) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        if (/event: focus[\s\S]*241-stream/.test(buffer)) {
          found = true;
          break;
        }
      }
      ctrl.abort();
      await reader.cancel().catch(() => {});
      expect(found).toBe(true);
    },
    8_000,
  );

  test("POST /api/home/tick runs the goal done command", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/home/tick`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind: "goal", id: "f1", done: true }),
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true, message: null });
    const lines = readFileSync(join(fakeDir, "calls.log"), "utf8").split("\n");
    expect(lines.some((l) => l === "focus done f1 | LIME_PROJECTS=" + join(tmp, "projects"))).toBe(true);
  });

  test("POST /api/home/tick runs the ticket close command", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/home/tick`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind: "ticket", project: "alpha", number: "227", done: true }),
    });
    expect(res.status).toBe(200);
    const lines = readFileSync(join(fakeDir, "calls.log"), "utf8").split("\n");
    expect(lines.some((l) => l.startsWith("ticket close alpha 227 |"))).toBe(true);
  });

  test("a failed write shows the command's stderr line", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/home/tick`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind: "goal", id: "f9", done: false }),
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: false, message: "lime focus: no row f9" });
  });

  test("a malformed tick body is 400 and never spawns", async () => {
    const before = readFileSync(join(fakeDir, "calls.log"), "utf8").split("\n").length;
    const res = await fetch(`http://127.0.0.1:${port}/api/home/tick`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ kind: "goal", id: "--help", done: true }),
    });
    expect(res.status).toBe(400);
    const after = readFileSync(join(fakeDir, "calls.log"), "utf8").split("\n").length;
    expect(after).toBe(before);
  });

  test("GET /api/home/tick is 405", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/home/tick`);
    expect(res.status).toBe(405);
  });

  test("a foreign Origin on the tick POST is refused", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/home/tick`, {
      method: "POST",
      headers: { "content-type": "application/json", origin: "http://evil.example" },
      body: JSON.stringify({ kind: "goal", id: "f1", done: true }),
    });
    expect(res.status).toBe(403);
  });
});

describe("source guard", () => {
  test("no server module names the home repo internals", () => {
    const dir = import.meta.dir;
    const files = readdirSync(dir).filter(
      (f) => f.endsWith(".ts") && !f.endsWith(".test.ts"),
    );
    expect(files.length).toBeGreaterThan(0);
    for (const file of files) {
      const text = readFileSync(join(dir, file), "utf8");
      // focusWatch.ts watches the focus file and the dashboard home, so it may
      // name them; it still never names the home repo's visit or push internals.
      const forbidden =
        file === "focusWatch.ts"
          ? ["last-visit", "push-error"]
          : ["last-visit", "focus.md", "push-error", ".lime/dashboard"];
      for (const word of forbidden) {
        expect(text.includes(word)).toBe(false);
      }
    }
  });

  test("focusWatch.ts only watches", () => {
    const text = readFileSync(join(import.meta.dir, "focusWatch.ts"), "utf8");
    const never = [
      "readFile",
      "writeFile",
      "appendFile",
      "createReadStream",
      "createWriteStream",
      "openSync",
      "unlink",
      "rmSync",
      "Bun.file",
      "Bun.write",
      "Bun.spawn",
      "bun:sqlite",
      "./db.ts",
    ];
    for (const word of never) {
      expect(text.includes(word)).toBe(false);
    }
    expect(text.includes(`from "./limeProxy.ts"`)).toBe(true);
  });

  test("server never writes the home or tickets itself", () => {
    const dir = import.meta.dir;
    for (const file of ["index.ts", "limeProxy.ts"]) {
      const text = readFileSync(join(dir, file), "utf8");
      for (const word of ["writeFile", "Bun.write", "appendFile", "renameSync", "unlink"]) {
        expect(text.includes(word)).toBe(false);
      }
    }
    expect(readFileSync(join(dir, "limeProxy.ts"), "utf8").includes("runLime(args")).toBe(true);
  });
});
