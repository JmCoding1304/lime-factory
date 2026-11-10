/**
 * The multi-project seam: registry parsing, merged reads over two projects'
 * lime.dbs, and the ticket's hard rule — zero writes to any db.
 *
 * Fixture dbs are written here with the same schema tracer.py creates
 * (lime/tracer.py is the schema's source of truth; keep them in sync).
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  LimeDb,
  LimeRegistry,
  readRegistry,
  resolveRegistryPath,
} from "./db.ts";

const SCHEMA = `
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, workflow TEXT NOT NULL, request TEXT,
    status TEXT NOT NULL DEFAULT 'running', engineer TEXT,
    started_at TEXT NOT NULL, ended_at TEXT,
    total_tokens INTEGER NOT NULL DEFAULT 0, total_cost REAL NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0, ticket TEXT, branch TEXT, worktree TEXT,
    port INTEGER, db_name TEXT, profile TEXT, kind TEXT NOT NULL DEFAULT 'run'
);
CREATE TABLE phases (
    phase_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
    name TEXT NOT NULL, kind TEXT NOT NULL, owner TEXT, description TEXT,
    status TEXT NOT NULL DEFAULT 'queued', attempt INTEGER NOT NULL DEFAULT 1,
    retries INTEGER NOT NULL DEFAULT 0, error TEXT, started_at TEXT, ended_at TEXT
);
CREATE TABLE events (
    event_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, phase_id TEXT,
    parent_id INTEGER, type TEXT NOT NULL, name TEXT, payload_json TEXT,
    tokens INTEGER, started_at TEXT NOT NULL, ended_at TEXT
);
CREATE TABLE envelopes (
    envelope_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, phase_id TEXT,
    agent TEXT, output_type TEXT, payload_json TEXT, valid INTEGER,
    attempt INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE gate_results (
    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, phase_id TEXT, attempt INTEGER,
    gate TEXT NOT NULL, passed INTEGER NOT NULL, violations_json TEXT,
    checks_json TEXT, created_at TEXT NOT NULL
);
CREATE TABLE processes (
    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT, name TEXT,
    pid INTEGER, command TEXT, started_at TEXT, ended_at TEXT
);
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, phase_id TEXT NOT NULL,
    role TEXT NOT NULL, continuation INTEGER NOT NULL DEFAULT 0,
    runtime TEXT NOT NULL, model TEXT NOT NULL, color TEXT,
    context_tokens INTEGER, context_window INTEGER,
    created_at TEXT NOT NULL, last_used_at TEXT
);
`;

let root: string;
let registryFile: string;

function makeProject(name: string, runId: string, startedAt: string): string {
  const project = join(root, name);
  const runsDir = join(project, ".lime", "runs");
  mkdirSync(runsDir, { recursive: true });
  const db = new Database(join(runsDir, "lime.db"));
  db.exec("PRAGMA journal_mode=WAL");
  db.exec(SCHEMA);
  db.query(
    `INSERT INTO runs (run_id, workflow, request, status, started_at, ticket, branch)
     VALUES (?, 'feature', ?, 'success', ?, '25', 'lime/feature/25-trace')`,
  ).run(runId, `request in ${name}`, startedAt);
  db.query(
    `INSERT INTO phases (phase_id, run_id, seq, name, kind, owner, status, started_at)
     VALUES (?, ?, 1, 'plan', 'agent', 'planner', 'success', ?)`,
  ).run(`${runId}:01`, runId, startedAt);
  db.query(
    `INSERT INTO phases (phase_id, run_id, seq, name, kind, owner, status, started_at)
     VALUES (?, ?, 2, 'build', 'agent', 'builder', 'success', ?)`,
  ).run(`${runId}:02`, runId, startedAt);
  db.query(
    `INSERT INTO sessions (session_id, run_id, phase_id, role, runtime, model,
                           context_tokens, context_window, created_at)
     VALUES (?, ?, ?, 'planner', 'pi', 'x-ai/grok-4.6', 42000, 256000, ?)`,
  ).run(`${runId}:01-planner`, runId, `${runId}:01`, startedAt);
  db.query(
    `INSERT INTO sessions (session_id, run_id, phase_id, role, runtime, model,
                           context_tokens, context_window, created_at)
     VALUES (?, ?, ?, 'builder', 'claude_code', 'claude-fable-5', 12000, 200000, ?)`,
  ).run(`${runId}:02-builder`, runId, `${runId}:02`, startedAt);
  db.query(
    `INSERT INTO events (run_id, phase_id, type, name, payload_json, started_at)
     VALUES (?, ?, 'agent_end', 'planner', ?, ?)`,
  ).run(
    runId,
    `${runId}:01`,
    JSON.stringify({
      cost: 0.25,
      usage: {
        input: 9000,
        output: 1000,
        cache_read: 0,
        cache_write: 500,
        total: 10500,
      },
    }),
    startedAt,
  );
  // Turn events are the spend feed (`name` holds the session_id — the
  // tracer contract lime cost joins on; the spend endpoint does the same).
  const turn = db.query(
    `INSERT INTO events (run_id, phase_id, type, name, payload_json, started_at)
     VALUES (?, ?, 'turn', ?, ?, ?)`,
  );
  turn.run(
    runId,
    `${runId}:01`,
    `${runId}:01-planner`,
    JSON.stringify({ cost: 0.05, context_tokens: 42000 }),
    startedAt,
  );
  turn.run(
    runId,
    `${runId}:02`,
    `${runId}:02-builder`,
    JSON.stringify({ cost: 0.0, context_tokens: 12000 }),
    startedAt,
  );
  // Old spend must fall outside a 7-day window.
  turn.run(
    runId,
    `${runId}:01`,
    `${runId}:01-planner`,
    JSON.stringify({ cost: 99.0 }),
    "2026-08-01T00:00:00+00:00",
  );
  db.query(
    `INSERT INTO events (run_id, phase_id, type, name, started_at)
     VALUES (?, ?, 'budget_warn', 'builder', ?)`,
  ).run(runId, `${runId}:02`, startedAt);
  db.close();

  const prompts = join(runsDir, runId, "01-planner", "prompts");
  mkdirSync(prompts, { recursive: true });
  writeFileSync(join(prompts, "system.md"), "# planner system\n");
  writeFileSync(join(prompts, "user.md"), "# planner user\n");
  return project;
}

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "lime-dash-test-"));
  const alpha = makeProject("alpha", "R-alpha", "2026-08-31T10:00:00+00:00");
  const beta = makeProject("beta", "R-beta", "2026-08-31T11:00:00+00:00");
  // A registered project with no lime.db yet must not break anything.
  const bare = join(root, "bare");
  mkdirSync(bare, { recursive: true });
  registryFile = join(root, "projects");
  writeFileSync(
    registryFile,
    `# repos registered by lime init\n${alpha}\n${beta}\n${bare}\n\n`,
  );
});

afterAll(() => {
  rmSync(root, { recursive: true, force: true });
});

describe("registry", () => {
  test("parses paths, skips comments and blanks", () => {
    const entries = readRegistry(registryFile);
    expect(entries.map((e) => e.name)).toEqual(["alpha", "beta", "bare"]);
  });

  test("a missing registry is a clear error", () => {
    expect(() => readRegistry(join(root, "nope"))).toThrow(
      /no project registry/,
    );
  });

  test("an empty registry is refused with a lime init hint", () => {
    const empty = join(root, "empty-projects");
    writeFileSync(empty, "# repos registered by lime init\n\n");
    expect(() => new LimeRegistry(empty)).toThrow(
      `project registry ${empty} is empty`,
    );
    expect(() => new LimeRegistry(empty)).toThrow(/`lime init` a project first/);
  });

  test("LIME_PROJECTS overrides the default location", () => {
    process.env.LIME_PROJECTS = registryFile;
    expect(resolveRegistryPath([])).toBe(registryFile);
    delete process.env.LIME_PROJECTS;
  });

  test("resolveProjects preserves order, deduplicates, and returns null path for unknown names", () => {
    const reg = new LimeRegistry(registryFile);
    const resolved = reg.resolveProjects(["beta", "unknown", "alpha", "beta"]);
    expect(resolved).toEqual([
      { name: "beta", path: expect.stringContaining("beta") },
      { name: "unknown", path: null },
      { name: "alpha", path: expect.stringContaining("alpha") },
    ]);
    reg.close();
  });
});

describe("multi-project reads", () => {
  test("runs are merged newest-first, each tagged with its project", () => {
    const reg = new LimeRegistry(registryFile);
    const runs = reg.runs();
    expect(runs.map((r) => [r.project, r.run_id])).toEqual([
      ["beta", "R-beta"],
      ["alpha", "R-alpha"],
    ]);
    // The slim payload carries nothing the list does not render: exactly the
    // 13 RunSummary keys.
    expect(Object.keys(runs[0]!).toSorted()).toEqual([
      "cash",
      "ended_at",
      "last_event_at",
      "project",
      "request",
      "run_id",
      "started_at",
      "status",
      "sub",
      "ticket",
      "total_cost",
      "warns",
      "workflow",
    ]);
    reg.close();
  });

  test("run summaries count budget_warn events for the L1 badge", () => {
    const reg = new LimeRegistry(registryFile);
    expect(reg.runs()[0]!.warns).toBe(1);
    reg.close();
  });

  test("spend sums turn costs per provider over the window — only openrouter is cash", () => {
    const reg = new LimeRegistry(registryFile);
    const spend = reg.spend("2026-08-25T00:00:00+00:00");
    // Two projects × $0.05 of x-ai turns each; the $99 turn is older than the
    // window. x-ai is not openrouter, so it rides the subscription too —
    // flagged, listed with the subscription tail.
    expect(spend).toEqual([
      { provider: "x-ai", cost: 0.1, subscription: true },
      { provider: "claude_code", cost: 0, subscription: true },
    ]);
    reg.close();
  });

  test("needs_rescope remains visible in run list and detail", () => {
    // ticket 130: a measured overflow's terminal status is just another status
    // row to this reader — no special-casing needed, but a regression is cheap.
    // ticket 131: the handoff and the recorded recovery decision must reach the
    // existing events reader too, so the dashboard can show the rescope notice.
    const project = join(root, "rescope");
    mkdirSync(join(project, ".lime", "runs"), { recursive: true });
    const dbPath = join(project, ".lime", "runs", "lime.db");
    const raw = new Database(dbPath);
    raw.exec("PRAGMA journal_mode=WAL");
    raw.exec(SCHEMA);
    raw
      .query(
        `INSERT INTO runs (run_id, workflow, request, status, started_at, ended_at, ticket, branch, worktree)
         VALUES ('R-rescope', 'feature', 'oversized diff', 'needs_rescope',
                 '2026-08-31T15:00:00+00:00', '2026-08-31T15:05:00+00:00', '130', 'lime/feature/130', '/tmp/wt')`,
      )
      .run();
    raw
      .query(
        `INSERT INTO events (run_id, phase_id, type, name, payload_json, started_at)
         VALUES ('R-rescope', NULL, 'handoff', 'needs_rescope', ?, '2026-08-31T15:05:00+00:00')`,
      )
      .run(JSON.stringify({ path: "/tmp/handoff.json" }));
    const handoffId = (
      raw.query("SELECT last_insert_rowid() AS id").get() as { id: number }
    ).id;
    raw
      .query(
        `INSERT INTO events (run_id, phase_id, parent_id, type, name, payload_json, started_at)
         VALUES ('R-rescope', NULL, ?, 'log', 'rescope_recovery_decision', ?, '2026-08-31T15:06:00+00:00')`,
      )
      .run(
        handoffId,
        JSON.stringify({
          action: "rescope_and_resume",
          decision: "scope cut to fit",
          engineer: "dev",
          handoff_path: "/tmp/handoff.json",
        }),
      );
    raw.close();

    const db = new LimeDb("rescope", dbPath);
    const summary = db.runs().find((r) => r.run_id === "R-rescope");
    expect(summary?.status).toBe("needs_rescope");
    expect(summary?.ended_at).toBe("2026-08-31T15:05:00+00:00");
    const detail = db.runDetail("R-rescope");
    expect(detail?.run.status).toBe("needs_rescope");
    expect(detail?.run.worktree).toBe("/tmp/wt");

    // The handoff and the decision bound to it page through the events reader.
    const page = db.events("R-rescope", 0, 10);
    const handoff = page.events.find(
      (e) => e.type === "handoff" && e.name === "needs_rescope",
    );
    expect(handoff?.payload_json).toContain("/tmp/handoff.json");
    const decision = page.events.find(
      (e) => e.name === "rescope_recovery_decision",
    );
    expect(decision?.type).toBe("log");
    expect(decision?.parent_id).toBe(handoff?.event_id);
    expect(decision?.payload_json).toContain("scope cut to fit");
    db.close();
  });

  test("a project without a db is listed but not read", () => {
    const reg = new LimeRegistry(registryFile);
    const bare = reg.projects().find((p) => p.name === "bare");
    expect(bare?.db_found).toBe(false);
    expect(reg.get("bare")).toBeNull();
    reg.close();
  });

  test("run detail carries usage derived from agent_end payloads", () => {
    const reg = new LimeRegistry(registryFile);
    const detail = reg.get("alpha")!.runDetail("R-alpha")!;
    expect(detail.usage).toEqual({ read: 9500, written: 1000 });
    expect(
      detail.sessions.find((s) => s.role === "planner")!.context_tokens,
    ).toBe(42000);
    reg.close();
  });

  test("events page by rowid cursor", () => {
    const reg = new LimeRegistry(registryFile);
    const db = reg.get("beta")!;
    const page = db.events("R-beta", 0, 10);
    expect(page.events).toHaveLength(5);
    expect(db.events("R-beta", page.cursor, 10).events).toHaveLength(0);
    reg.close();
  });

  test("prompts resolve through the <seq>-<role> session dir", () => {
    const reg = new LimeRegistry(registryFile);
    const dir = reg.get("alpha")!.promptsDir("R-alpha", "planner");
    expect(dir).toEndWith(join("R-alpha", "01-planner", "prompts"));
    expect(reg.get("alpha")!.promptsDir("R-alpha", "builder")).toBeNull();
    reg.close();
  });
});

describe("idle dbs", () => {
  test("a cleanly checkpointed WAL db reads via snapshot, swapped when a writer appears", () => {
    const project = makeProject(
      "gamma",
      "R-gamma",
      "2026-08-31T12:00:00+00:00",
    );
    const dbPath = join(project, ".lime", "runs", "lime.db");
    // A tracer that closed cleanly leaves no -wal/-shm — the state bun's
    // sqlite cannot open with a plain readonly flag.
    rmSync(`${dbPath}-wal`, { force: true });
    rmSync(`${dbPath}-shm`, { force: true });

    const db = new LimeDb("gamma", dbPath);
    expect(db.immutable).toBe(true);
    expect(db.runs()[0]!.run_id).toBe("R-gamma");
    expect(db.stale()).toBe(false);

    // A writer shows up: sidecars reappear, the snapshot is stale.
    const writer = new Database(dbPath);
    writer
      .query("UPDATE runs SET status = 'fail' WHERE run_id = ?")
      .run("R-gamma");
    expect(db.stale()).toBe(true);
    db.close();

    // A fresh open (what probe() does) sees the new state, live this time.
    const fresh = new LimeDb("gamma", dbPath);
    expect(fresh.immutable).toBe(false);
    expect(fresh.runs()[0]!.status).toBe("fail");
    fresh.close();
    writer.close();
  });
});

describe("read-only", () => {
  test("the connection refuses writes — lime dash never writes to any db", () => {
    const dbPath = join(root, "alpha", ".lime", "runs", "lime.db");
    const db = new LimeDb("alpha", dbPath);
    const raw = (db as unknown as { db: Database }).db;
    expect(() => raw.exec("UPDATE runs SET archived = 1")).toThrow(/readonly/i);
    db.close();
  });
});

/**
 * Build a project directory with the specific runs, phases, sessions, turn and
 * agent_end events the cost tests assert against. Reuses SCHEMA and root.
 *
 * R-win   deployed    2026-08-31T10:00:00+00:00  planner(claude_code) + builder(pi)
 * R-loss  fail        2026-08-31T12:00:00+00:00  builder(pi) only
 * R-live  running     2026-08-31T13:00:00+00:00  builder(pi) only
 * R-old   deployed    2026-08-01T10:00:00+00:00  builder(pi) only, old start
 * R-killed budget_exhausted 2026-08-31T14:00:00+00:00  planner(claude_code) only, NO agent_end
 */
function makeCostProject(name: string): string {
  const project = join(root, name);
  const runsDir = join(project, ".lime", "runs");
  mkdirSync(runsDir, { recursive: true });
  const db = new Database(join(runsDir, "lime.db"));
  db.exec("PRAGMA journal_mode=WAL");
  db.exec(SCHEMA);

  const insertRun = db.prepare(
    `INSERT INTO runs (run_id, workflow, request, status, started_at, ticket, branch)
     VALUES (?, 'feature', ?, ?, ?, '25', 'lime/feature/25-trace')`,
  );
  const insertPhase = db.prepare(
    `INSERT INTO phases (phase_id, run_id, seq, name, kind, owner, status, started_at)
     VALUES (?, ?, ?, ?, 'agent', ?, 'success', ?)`,
  );
  const insertSession = db.prepare(
    `INSERT INTO sessions (session_id, run_id, phase_id, role, continuation, runtime, model,
                           context_tokens, context_window, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, 0, 200000, ?)`,
  );
  const insertStart = db.prepare(
    `INSERT INTO events (run_id, phase_id, type, name, payload_json, started_at)
     VALUES (?, ?, 'agent_start', ?, ?, ?)`,
  );
  const insertTurn = db.prepare(
    `INSERT INTO events (run_id, phase_id, type, name, payload_json, started_at)
     VALUES (?, ?, 'turn', ?, ?, ?)`,
  );
  const insertEnd = db.prepare(
    `INSERT INTO events (run_id, phase_id, type, name, payload_json, started_at)
     VALUES (?, ?, 'agent_end', ?, ?, ?)`,
  );

  // ── R-win: deployed, started 2026-08-31T10:00:00+00:00 ──────────────────
  const rw = "R-win";
  const rwStart = "2026-08-31T10:00:00+00:00";
  insertRun.run(rw, `request in ${name}`, "deployed", rwStart);
  insertPhase.run(`${rw}:01`, rw, 1, "plan", "planner", rwStart);
  insertPhase.run(`${rw}:02`, rw, 2, "build", "builder", rwStart);
  // planner session: claude_code
  insertSession.run(`${rw}:01-planner`, rw, `${rw}:01`, "planner", 0, "claude_code", "claude-fable-5-1", rwStart);
  // planner agent_start at run start
  insertStart.run(rw, `${rw}:01`, `${rw}:01-planner`, JSON.stringify({ session_id: `${rw}:01-planner`, model: "claude-fable-5-1", runtime: "claude_code" }), rwStart);
  // builder session: pi
  insertSession.run(`${rw}:02-builder`, rw, `${rw}:02`, "builder", 0, "pi", "openrouter/deepseek/deepseek-v4-flash", rwStart);
  // builder agent_start at 10:05:00
  insertStart.run(rw, `${rw}:02`, `${rw}:02-builder`, JSON.stringify({ session_id: `${rw}:02-builder`, model: "openrouter/deepseek/deepseek-v4-flash", runtime: "pi" }), "2026-08-31T10:05:00+00:00");
  // planner turns
  insertTurn.run(rw, `${rw}:01`, `${rw}:01-planner`, JSON.stringify({ cost: 0.0, input: 56, output: 2, cache_read: 8000, cache_write: 7000 }), rwStart);
  insertTurn.run(rw, `${rw}:01`, `${rw}:01-planner`, JSON.stringify({ cost: 0.11, input: 10, output: 5, cache_read: 0, cache_write: 0 }), "2026-08-31T10:30:00+00:00");
  // planner agent_end at 10:30:00
  insertEnd.run(rw, `${rw}:01`, "planner", JSON.stringify({ cost: 1.25 }), "2026-08-31T10:30:00+00:00");
  // builder turns
  insertTurn.run(rw, `${rw}:02`, `${rw}:02-builder`, JSON.stringify({ cost: 0.02, input: 26000, output: 4000, cache_read: 0, cache_write: 0 }), rwStart);
  insertTurn.run(rw, `${rw}:02`, `${rw}:02-builder`, JSON.stringify({ cost: 0.03, input: 1000, output: 500, cache_read: 0, cache_write: 0 }), "2026-08-01T00:00:00+00:00");
  // builder agent_end at 10:17:30
  insertEnd.run(rw, `${rw}:02`, "builder", JSON.stringify({ cost: 0.05 }), "2026-08-31T10:17:30+00:00");

  // ── R-loss: fail, started 2026-08-31T12:00:00+00:00 ─────────────────────
  const rl = "R-loss";
  const rlStart = "2026-08-31T12:00:00+00:00";
  insertRun.run(rl, `request in ${name}`, "fail", rlStart);
  insertPhase.run(`${rl}:02`, rl, 1, "build", "builder", rlStart);
  // first (non-continuation) session
  insertSession.run(`${rl}:02-builder`, rl, `${rl}:02`, "builder", 0, "pi", "openrouter/deepseek/deepseek-v4-flash", rlStart);
  // builder agent_start at run start
  insertStart.run(rl, `${rl}:02`, `${rl}:02-builder`, JSON.stringify({ session_id: `${rl}:02-builder`, model: "openrouter/deepseek/deepseek-v4-flash", runtime: "pi" }), rlStart);
  // continuation session (created at 12:01:00, same phase/model/runtime)
  insertSession.run(`${rl}:02-builder.1`, rl, `${rl}:02`, "builder", 1, "pi", "openrouter/deepseek/deepseek-v4-flash", "2026-08-31T12:01:00+00:00");
  // first session turns
  insertTurn.run(rl, `${rl}:02`, `${rl}:02-builder`, JSON.stringify({ cost: 0.04, input: 100, output: 10, cache_read: 0, cache_write: 0 }), rlStart);
  // first session agent_end at 12:03:00
  insertEnd.run(rl, `${rl}:02`, "builder", JSON.stringify({ cost: 0.04 }), "2026-08-31T12:03:00+00:00");

  // ── R-live: running, started 2026-08-31T13:00:00+00:00 ──────────────────
  const rv = "R-live";
  const rvStart = "2026-08-31T13:00:00+00:00";
  insertRun.run(rv, `request in ${name}`, "running", rvStart);
  insertPhase.run(`${rv}:02`, rv, 1, "build", "builder", rvStart);
  insertSession.run(`${rv}:02-builder`, rv, `${rv}:02`, "builder", 0, "pi", "openrouter/deepseek/deepseek-v4-flash", rvStart);
  // builder agent_start at run start
  insertStart.run(rv, `${rv}:02`, `${rv}:02-builder`, JSON.stringify({ session_id: `${rv}:02-builder`, model: "openrouter/deepseek/deepseek-v4-flash", runtime: "pi" }), rvStart);
  insertTurn.run(rv, `${rv}:02`, `${rv}:02-builder`, JSON.stringify({ cost: 0.5, input: 100, output: 10, cache_read: 0, cache_write: 0 }), rvStart);
  // NO agent_end for R-live (running)

  // ── R-old: deployed, started 2026-08-01T10:00:00+00:00 ──────────────────
  const ro = "R-old";
  const roStart = "2026-08-01T10:00:00+00:00";
  insertRun.run(ro, `request in ${name}`, "deployed", roStart);
  insertPhase.run(`${ro}:02`, ro, 1, "build", "builder", roStart);
  insertSession.run(`${ro}:02-builder`, ro, `${ro}:02`, "builder", 0, "pi", "openrouter/deepseek/deepseek-v4-flash", roStart);
  // builder agent_start at run start
  insertStart.run(ro, `${ro}:02`, `${ro}:02-builder`, JSON.stringify({ session_id: `${ro}:02-builder`, model: "openrouter/deepseek/deepseek-v4-flash", runtime: "pi" }), roStart);
  insertTurn.run(ro, `${ro}:02`, `${ro}:02-builder`, JSON.stringify({ cost: 9.0, input: 100, output: 10, cache_read: 0, cache_write: 0 }), "2026-08-31T10:00:00+00:00");
  // NO agent_end for R-old

  // ── R-killed: budget_exhausted, started 2026-08-31T14:00:00+00:00 ──────
  const rk = "R-killed";
  const rkStart = "2026-08-31T14:00:00+00:00";
  insertRun.run(rk, `request in ${name}`, "budget_exhausted", rkStart);
  insertPhase.run(`${rk}:01`, rk, 1, "plan", "planner", rkStart);
  insertSession.run(`${rk}:01-planner`, rk, `${rk}:01`, "planner", 0, "claude_code", "claude-fable-5-1", rkStart);
  // planner agent_start at run start
  insertStart.run(rk, `${rk}:01`, `${rk}:01-planner`, JSON.stringify({ session_id: `${rk}:01-planner`, model: "claude-fable-5-1", runtime: "claude_code" }), rkStart);
  insertTurn.run(rk, `${rk}:01`, `${rk}:01-planner`, JSON.stringify({ cost: 0.0, input: 50, output: 5, cache_read: 100, cache_write: 100 }), rkStart);
  // NO agent_end for R-killed planner

  db.close();
  return project;
}

/**
 * Build a project whose runs exercise the one cash rule: `openrouter/` bills
 * API dollars, everything else is notional. Reuses SCHEMA and root.
 *
 * R-cash       success  2026-08-31T09:00:00+00:00
 *   builder(pi, openrouter) turns 0.02 + 0.03
 *   reviewer(pi, openai-codex) turn 0.40
 *   planner(claude_code, claude-opus-5-5[1m]) turn 0.0 + agent_end 1.20
 *   documenter(pi, model '') turn 0.10
 *   latest event 2026-08-31T09:30:00+00:00
 * R-cash-only  success  only the openrouter builder, turn 0.07
 * R-quiet      success  no events
 */
function makeCashProject(name: string): string {
  const project = join(root, name);
  const runsDir = join(project, ".lime", "runs");
  mkdirSync(runsDir, { recursive: true });
  const db = new Database(join(runsDir, "lime.db"));
  db.exec("PRAGMA journal_mode=WAL");
  db.exec(SCHEMA);

  const insertRun = db.prepare(
    `INSERT INTO runs (run_id, workflow, request, status, started_at, ticket, branch)
     VALUES (?, 'feature', ?, ?, ?, '25', 'lime/feature/25-trace')`,
  );
  const insertPhase = db.prepare(
    `INSERT INTO phases (phase_id, run_id, seq, name, kind, owner, status, started_at)
     VALUES (?, ?, ?, ?, 'agent', ?, 'success', ?)`,
  );
  const insertSession = db.prepare(
    `INSERT INTO sessions (session_id, run_id, phase_id, role, continuation, runtime, model,
                           context_tokens, context_window, created_at)
     VALUES (?, ?, ?, ?, 0, ?, ?, 0, 200000, ?)`,
  );
  const insertEvent = db.prepare(
    `INSERT INTO events (run_id, phase_id, type, name, payload_json, started_at)
     VALUES (?, ?, ?, ?, ?, ?)`,
  );

  // ── R-cash: success, started 2026-08-31T09:00:00+00:00 ──────────────────
  const rc = "R-cash";
  const rcStart = "2026-08-31T09:00:00+00:00";
  insertRun.run(rc, `request in ${name}`, "success", rcStart);
  insertPhase.run(`${rc}:01`, rc, 1, "plan", "planner", rcStart);
  insertPhase.run(`${rc}:02`, rc, 2, "build", "builder", rcStart);
  insertPhase.run(`${rc}:03`, rc, 3, "review", "reviewer", rcStart);
  insertPhase.run(`${rc}:04`, rc, 4, "document", "documenter", rcStart);

  // builder: cash openrouter, turns 0.02 + 0.03
  insertSession.run(
    `${rc}:02-builder`,
    rc,
    `${rc}:02`,
    "builder",
    "pi",
    "openrouter/deepseek/deepseek-v4.1-flash",
    rcStart,
  );
  insertEvent.run(rc, `${rc}:02`, "turn", `${rc}:02-builder`, JSON.stringify({ cost: 0.02 }), "2026-08-31T09:01:00+00:00");
  insertEvent.run(rc, `${rc}:02`, "turn", `${rc}:02-builder`, JSON.stringify({ cost: 0.03 }), "2026-08-31T09:02:00+00:00");
  // reviewer: openai-codex subscription, turn 0.40
  insertSession.run(`${rc}:03-reviewer`, rc, `${rc}:03`, "reviewer", "pi", "openai-codex/gpt-6-astra", rcStart);
  insertEvent.run(rc, `${rc}:03`, "turn", `${rc}:03-reviewer`, JSON.stringify({ cost: 0.4 }), "2026-08-31T09:03:00+00:00");
  // planner: claude_code subscription, turn 0.0, agent_end 1.20
  insertSession.run(`${rc}:01-planner`, rc, `${rc}:01`, "planner", "claude_code", "claude-opus-5-5[1m]", rcStart);
  insertEvent.run(rc, `${rc}:01`, "turn", `${rc}:01-planner`, JSON.stringify({ cost: 0.0 }), "2026-08-31T09:04:00+00:00");
  insertEvent.run(rc, `${rc}:01`, "agent_end", `${rc}:01-planner`, JSON.stringify({ cost: 1.2 }), "2026-08-31T09:05:00+00:00");
  // documenter: pi with an empty model — unknown, so subscription
  insertSession.run(`${rc}:04-documenter`, rc, `${rc}:04`, "documenter", "pi", "", rcStart);
  insertEvent.run(rc, `${rc}:04`, "turn", `${rc}:04-documenter`, JSON.stringify({ cost: 0.1 }), "2026-08-31T09:06:00+00:00");
  // a known latest event, later than every turn
  insertEvent.run(rc, `${rc}:04`, "log", "documenter", JSON.stringify({}), "2026-08-31T09:30:00+00:00");

  // ── R-cash-only: only the openrouter builder, turn 0.07 ────────────────
  const rco = "R-cash-only";
  const rcoStart = "2026-08-31T09:10:00+00:00";
  insertRun.run(rco, `request in ${name}`, "success", rcoStart);
  insertPhase.run(`${rco}:02`, rco, 1, "build", "builder", rcoStart);
  insertSession.run(`${rco}:02-builder`, rco, `${rco}:02`, "builder", "pi", "openrouter/deepseek/deepseek-v4.1-flash", rcoStart);
  insertEvent.run(rco, `${rco}:02`, "turn", `${rco}:02-builder`, JSON.stringify({ cost: 0.07 }), "2026-08-31T09:11:00+00:00");

  // ── R-quiet: no events ─────────────────────────────────────────────────
  insertRun.run("R-quiet", `request in ${name}`, "success", "2026-08-31T09:20:00+00:00");

  db.close();
  return project;
}

describe("session costs", () => {
  test("one row per Session of a finished run started in the window, tagged with its project", () => {
    const delta = makeCostProject("delta");
    const epsilon = makeCostProject("epsilon");
    const regFile = join(root, "cost-projects");
    writeFileSync(regFile, `${delta}\n${epsilon}\n`);
    const reg = new LimeRegistry(regFile);
    const rows = reg.sessionCosts("2026-08-25T00:00:00+00:00");

    // 10 rows: 2 projects × (R-win/planner, R-win/builder, R-loss/builder, R-loss/builder(continuation), R-killed/planner)
    expect(rows).toHaveLength(10);
    for (const row of rows) {
      expect(["delta", "epsilon"]).toContain(row.project);
    }

    // delta's rows: ordered by r.started_at, s.created_at
    const deltaPairs = rows
      .filter((r) => r.project === "delta")
      .map((r) => [r.run_id, r.role]);
    expect(deltaPairs).toEqual([
      ["R-win", "planner"],
      ["R-win", "builder"],
      ["R-loss", "builder"],
      ["R-loss", "builder"],
      ["R-killed", "planner"],
    ]);

    // Nothing from R-live (running) or R-old (started before window)
    const outOfWindow = rows.filter(
      (r) => r.run_id === "R-live" || r.run_id === "R-old",
    );
    expect(outOfWindow).toHaveLength(0);

    reg.close();
  });

  test("API dollars are whole-run turn sums: an old turn of an in-window run counts", () => {
    const reg = new LimeRegistry(
      (() => {
        const d = makeCostProject("d");
        const f = join(root, "cost-projects-2");
        writeFileSync(f, `${d}\n`);
        return f;
      })(),
    );
    const rows = reg.sessionCosts("2026-08-25T00:00:00+00:00");
    const rWinBuilder = rows.find(
      (r) => r.run_id === "R-win" && r.role === "builder",
    )!;
    // 0.02 + 0.03 = 0.05 (the old turn from 2026-08-01 is still within the run)
    expect(rWinBuilder.api_cost).toBeCloseTo(0.05);
    expect(rWinBuilder.notional_cost).toBe(0);
    // tokens: 26000 + 4000 + 0 + 0 + 1000 + 500 + 0 + 0 = 31500
    expect(rWinBuilder.tokens).toBe(31500);
    expect(rWinBuilder.run_status).toBe("deployed");
    expect(rWinBuilder.runtime).toBe("pi");
    expect(rWinBuilder.model).toBe("openrouter/deepseek/deepseek-v4-flash");
    reg.close();
  });

  test("claude_code never bills API dollars; its notional is the agent_end cost", () => {
    const reg = new LimeRegistry(
      (() => {
        const d = makeCostProject("c");
        const f = join(root, "cost-projects-3");
        writeFileSync(f, `${d}\n`);
        return f;
      })(),
    );
    const rows = reg.sessionCosts("2026-08-25T00:00:00+00:00");
    const rWinPlanner = rows.find(
      (r) => r.run_id === "R-win" && r.role === "planner",
    )!;
    // api_cost is 0 despite the 0.11 turn
    expect(rWinPlanner.api_cost).toBe(0);
    // notional_cost is the agent_end cost
    expect(rWinPlanner.notional_cost).toBeCloseTo(1.25);
    // tokens: 56+2+8000+7000 + 10+5+0+0 = 15073
    expect(rWinPlanner.tokens).toBe(15073);
    expect(rWinPlanner.run_status).toBe("deployed");
    expect(rWinPlanner.runtime).toBe("claude_code");
    expect(rWinPlanner.model).toBe("claude-fable-5-1");
    reg.close();
  });

  test("a Session without an agent_end has an unknown notional, not $0", () => {
    const reg = new LimeRegistry(
      (() => {
        const d = makeCostProject("e");
        const f = join(root, "cost-projects-4");
        writeFileSync(f, `${d}\n`);
        return f;
      })(),
    );
    const rows = reg.sessionCosts("2026-08-25T00:00:00+00:00");

    // R-killed planner: claude_code, has turns but NO agent_end → notional_cost null
    const killedPlanner = rows.find(
      (r) => r.run_id === "R-killed" && r.role === "planner",
    )!;
    expect(killedPlanner.notional_cost).toBeNull();
    expect(killedPlanner.api_cost).toBe(0);
    expect(killedPlanner.run_status).toBe("budget_exhausted");

    // R-loss builder: has agent_end → notional_cost 0 (api runtime, not claude_code)
    const lossBuilder = rows.find(
      (r) => r.run_id === "R-loss" && r.role === "builder",
    )!;
    expect(lossBuilder.notional_cost).toBe(0);
    expect(lossBuilder.api_cost).toBeCloseTo(0.04);
    expect(lossBuilder.run_status).toBe("fail");

    reg.close();
  });

  test("duration_ms is agent_start->agent_end wall-clock on the first Session", () => {
    const reg = new LimeRegistry(
      (() => {
        const d = makeCostProject("f");
        const f = join(root, "cost-projects-5");
        writeFileSync(f, `${d}
`);
        return f;
      })(),
    );
    const rows = reg.sessionCosts("2026-08-25T00:00:00+00:00");

    const rWinPlanner = rows.find(
      (r) => r.run_id === "R-win" && r.role === "planner",
    )!;
    expect(rWinPlanner.duration_ms).toBe(1_800_000);
    expect(Number.isInteger(rWinPlanner.duration_ms)).toBe(true);

    const rWinBuilder = rows.find(
      (r) => r.run_id === "R-win" && r.role === "builder",
    )!;
    expect(rWinBuilder.duration_ms).toBe(750_000);
    expect(Number.isInteger(rWinBuilder.duration_ms)).toBe(true);

    const lossBuilders = rows.filter(
      (r) => r.run_id === "R-loss" && r.role === "builder",
    );
    expect(lossBuilders[0].duration_ms).toBe(180_000);
    expect(Number.isInteger(lossBuilders[0].duration_ms)).toBe(true);

    reg.close();
  });

  test("duration_ms is null without an agent_end and on a continuation row", () => {
    const reg = new LimeRegistry(
      (() => {
        const d = makeCostProject("g");
        const f = join(root, "cost-projects-6");
        writeFileSync(f, `${d}
`);
        return f;
      })(),
    );
    const rows = reg.sessionCosts("2026-08-25T00:00:00+00:00");

    const killedPlanner = rows.find(
      (r) => r.run_id === "R-killed" && r.role === "planner",
    )!;
    expect(killedPlanner.duration_ms).toBeNull();

    const lossBuilders = rows.filter(
      (r) => r.run_id === "R-loss" && r.role === "builder",
    );
    expect(lossBuilders[1].duration_ms).toBeNull();

    expect(killedPlanner.notional_cost).toBeNull();
    expect(killedPlanner.api_cost).toBe(0);
    expect(lossBuilders[0].notional_cost).toBe(0);
    expect(lossBuilders[0].api_cost).toBeCloseTo(0.04);

    reg.close();
  });
});
describe("cash rule", () => {
  test("spend puts openrouter cash first and every other bucket in the subscription tail", () => {
    const cash = makeCashProject("cash");
    const regFile = join(root, "cash-projects");
    writeFileSync(regFile, `${cash}\n`);
    const reg = new LimeRegistry(regFile);
    const spend = reg.spend("2026-08-25T00:00:00+00:00");
    // openrouter: builder 0.02+0.03 + R-cash-only builder 0.07 = 0.12, cash.
    // openai-codex 0.40, unknown (empty model) 0.10, claude_code 0 — all notional.
    expect(spend).toEqual([
      { provider: "openrouter", cost: 0.12, subscription: false },
      { provider: "openai-codex", cost: 0.4, subscription: true },
      { provider: "unknown", cost: 0.1, subscription: true },
      { provider: "claude_code", cost: 0, subscription: true },
    ]);
    reg.close();
  });

  test("sessionCosts split cash turn costs from notional subscription costs", () => {
    const cash = makeCashProject("cash-sessions");
    const regFile = join(root, "cash-sessions-projects");
    writeFileSync(regFile, `${cash}\n`);
    const reg = new LimeRegistry(regFile);
    const rows = reg
      .sessionCosts("2026-08-25T00:00:00+00:00")
      .filter((r) => r.run_id === "R-cash");

    const builder = rows.find((r) => r.role === "builder")!;
    expect(builder.api_cost).toBeCloseTo(0.05);
    expect(builder.notional_cost).toBe(0);

    const reviewer = rows.find((r) => r.role === "reviewer")!;
    expect(reviewer.api_cost).toBe(0);
    expect(reviewer.notional_cost).toBeCloseTo(0.4);

    const planner = rows.find((r) => r.role === "planner")!;
    expect(planner.api_cost).toBe(0);
    expect(planner.notional_cost).toBeCloseTo(1.2);

    const documenter = rows.find((r) => r.role === "documenter")!;
    expect(documenter.api_cost).toBe(0);
    expect(documenter.notional_cost).toBeCloseTo(0.1);

    reg.close();
  });
});

describe("run summaries", () => {
  test("cash, sub and last_event_at fold per run", () => {
    const project = makeCashProject("cash-summaries");
    const regFile = join(root, "cash-summaries-projects");
    writeFileSync(regFile, `${project}\n`);
    const reg = new LimeRegistry(regFile);
    const runs = reg.runs();

    const cash = runs.find((r) => r.run_id === "R-cash")!;
    expect(cash.cash).toBeCloseTo(0.05);
    expect(cash.sub).toBe(true);
    expect(cash.last_event_at).toBe("2026-08-31T09:30:00+00:00");

    const only = runs.find((r) => r.run_id === "R-cash-only")!;
    expect(only.cash).toBeCloseTo(0.07);
    expect(only.sub).toBe(false);

    const quiet = runs.find((r) => r.run_id === "R-quiet")!;
    expect(quiet.cash).toBe(0);
    expect(quiet.sub).toBe(false);
    expect(quiet.last_event_at).toBeNull();

    reg.close();
  });

  test("an orphan turn is not cash and flips sub, leaving cash alone", () => {
    const project = makeCashProject("cash-orphan");
    const dbPath = join(project, ".lime", "runs", "lime.db");
    const raw = new Database(dbPath);
    raw
      .query(
        `INSERT INTO events (run_id, phase_id, type, name, payload_json, started_at)
         VALUES ('R-cash-only', NULL, 'turn', 'ghost-session', ?, '2026-08-31T09:12:00+00:00')`,
      )
      .run(JSON.stringify({ cost: 0.5 }));
    raw.close();

    const regFile = join(root, "cash-orphan-projects");
    writeFileSync(regFile, `${project}\n`);
    const reg = new LimeRegistry(regFile);
    const run = reg.runs().find((r) => r.run_id === "R-cash-only")!;
    expect(run.cash).toBeCloseTo(0.07);
    expect(run.sub).toBe(true);
    reg.close();
  });
});
