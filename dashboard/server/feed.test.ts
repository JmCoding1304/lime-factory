/**
 * The live feed's tests. Fixture dbs use the current `lime/tracer.py` `_SCHEMA`
 * (the schema's single source of truth); keep them in sync. `LEGACY_SCHEMA` is
 * the shape written before tickets 251/252 — no `events.seq`, `phases.stage`,
 * `processes.heartbeat_at` or `sessions.budget_*` — so a read-only connection
 * must fall back to NULL rather than fail.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { LimeDb, LimeRegistry } from "./db.ts";
import { FocusWatcher } from "./focusWatch.ts";
import {
  type FeedChange,
  type FeedSource,
  EVENTS_PAGE,
  judge,
  LimeFeedSource,
  LIVE_DEBOUNCE_MS,
  LIVE_TICK_MS,
  LIVENESS_MS,
  liveStream,
  parseCursors,
  parseStamp,
  pidAlive,
  PING_MS,
  RETRY_MS,
} from "./feed.ts";
import type {
  FeedEvent,
  FeedEventsPage,
  FeedRun,
  HomeFocusResponse,
  LiveBatch,
  LiveRunning,
  QueuedChild,
  RunningRun,
} from "../shared/types.ts";

/** The current tracer.py schema (tables runs, phases, events, envelopes,
 *  gate_results, processes and sessions). */
const CURRENT_SCHEMA = `
CREATE TABLE runs (
    run_id       TEXT PRIMARY KEY,
    workflow     TEXT NOT NULL,
    request      TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    engineer     TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost   REAL NOT NULL DEFAULT 0,
    archived     INTEGER NOT NULL DEFAULT 0,
    ticket       TEXT,
    branch       TEXT,
    worktree     TEXT,
    port         INTEGER,
    db_name      TEXT,
    profile      TEXT,
    base_sha     TEXT,
    pr_number    INTEGER,
    pr_url       TEXT,
    kind         TEXT NOT NULL DEFAULT 'run',
    batch_id     TEXT
);
CREATE TABLE phases (
    phase_id    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    owner       TEXT,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    attempt     INTEGER NOT NULL DEFAULT 1,
    retries     INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    started_at  TEXT,
    ended_at    TEXT,
    stage       TEXT
);
CREATE TABLE events (
    event_id     INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL,
    phase_id     TEXT,
    parent_id    INTEGER,
    type         TEXT NOT NULL,
    name         TEXT,
    payload_json TEXT,
    tokens       INTEGER,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    seq          INTEGER
);
CREATE TABLE envelopes (
    envelope_id  INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL,
    phase_id     TEXT,
    agent        TEXT,
    output_type  TEXT,
    payload_json TEXT,
    valid        INTEGER,
    attempt      INTEGER,
    created_at   TEXT NOT NULL
);
CREATE TABLE gate_results (
    id              INTEGER PRIMARY KEY,
    run_id          TEXT NOT NULL,
    phase_id        TEXT,
    attempt         INTEGER,
    gate            TEXT NOT NULL,
    passed          INTEGER NOT NULL,
    violations_json TEXT,
    checks_json     TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE processes (
    id         INTEGER PRIMARY KEY,
    run_id     TEXT NOT NULL,
    kind       TEXT,
    name       TEXT,
    pid        INTEGER,
    command    TEXT,
    started_at TEXT,
    ended_at   TEXT,
    heartbeat_at TEXT,
    retried_at TEXT,
    retry_of   TEXT
);
CREATE TABLE sessions (
    session_id     TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    phase_id       TEXT NOT NULL,
    role           TEXT NOT NULL,
    continuation   INTEGER NOT NULL DEFAULT 0,
    runtime        TEXT NOT NULL,
    model          TEXT NOT NULL,
    color          TEXT,
    context_tokens INTEGER,
    context_window INTEGER,
    created_at     TEXT NOT NULL,
    last_used_at   TEXT,
    budget_warn    INTEGER,
    budget_stop    INTEGER
);
CREATE TABLE batch_children (
    batch_id   TEXT NOT NULL,
    position   INTEGER NOT NULL,
    ticket     TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'queued',
    reason     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, position)
);
`;

/** The pre-251/252 shape (as in `db.test.ts`). */
const LEGACY_SCHEMA = `
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

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "lime-feed-"));
});

afterAll(() => {
  rmSync(root, { recursive: true, force: true });
});

/** Create `<root>/<name>/.lime/runs/lime.db` in WAL and return its path plus
 *  the open writer handle (kept alive by the caller). */
function makeProject(
  name: string,
  schema: string,
): { path: string; db: Database } {
  const runsDir = join(root, name, ".lime", "runs");
  mkdirSync(runsDir, { recursive: true });
  const path = join(runsDir, "lime.db");
  const db = new Database(path);
  db.exec("PRAGMA journal_mode=WAL");
  db.exec(schema);
  return { path, db };
}

function columns(db: Database, table: string): Set<string> {
  return new Set(
    db
      .query<{ name: string }, []>(`PRAGMA table_info(${table})`)
      .all()
      .map((row) => row.name),
  );
}

type Bind = string | number | null;

/** Insert `row`, dropping any key whose column the schema lacks (so one helper
 *  serves the current and the legacy schema). */
function insert(db: Database, table: string, row: Record<string, Bind>): void {
  const cols = columns(db, table);
  const entries = Object.entries(row).filter(([k]) => cols.has(k));
  const names = entries.map(([k]) => k).join(", ");
  const marks = entries.map(() => "?").join(", ");
  db.query(`INSERT INTO ${table} (${names}) VALUES (${marks})`).run(
    ...(entries.map(([, v]) => v) as Bind[]),
  );
}

function addRun(
  db: Database,
  r: {
    run_id: string;
    started_at: string;
    workflow?: string;
    request?: string | null;
    status?: string;
    kind?: string;
    ticket?: string | null;
    batch_id?: string | null;
  },
): void {
  insert(db, "runs", {
    run_id: r.run_id,
    workflow: r.workflow ?? "feature",
    request: r.request ?? null,
    status: r.status ?? "running",
    ticket: r.ticket ?? null,
    started_at: r.started_at,
    kind: r.kind ?? "run",
    batch_id: r.batch_id ?? null,
  });
}

function addPhase(
  db: Database,
  p: {
    phase_id: string;
    run_id: string;
    seq: number;
    name?: string;
    kind?: string;
    owner?: string | null;
    status?: string;
    stage?: string | null;
    started_at?: string | null;
  },
): void {
  insert(db, "phases", {
    phase_id: p.phase_id,
    run_id: p.run_id,
    seq: p.seq,
    name: p.name ?? "build",
    kind: p.kind ?? "agent",
    owner: p.owner ?? null,
    status: p.status ?? "running",
    started_at: p.started_at ?? null,
    stage: p.stage ?? null,
  });
}

function addSession(
  db: Database,
  s: {
    session_id: string;
    run_id: string;
    phase_id: string;
    role: string;
    runtime?: string;
    model?: string;
    context_tokens?: number | null;
    context_window?: number | null;
    created_at?: string;
    budget_warn?: number | null;
    budget_stop?: number | null;
  },
): void {
  insert(db, "sessions", {
    session_id: s.session_id,
    run_id: s.run_id,
    phase_id: s.phase_id,
    role: s.role,
    runtime: s.runtime ?? "pi",
    model: s.model ?? "x-ai/grok-4.6",
    context_tokens: s.context_tokens ?? null,
    context_window: s.context_window ?? null,
    created_at: s.created_at ?? "2026-01-01T00:00:00Z",
    budget_warn: s.budget_warn ?? null,
    budget_stop: s.budget_stop ?? null,
  });
}

function addEvent(
  db: Database,
  e: {
    run_id: string;
    type?: string;
    name?: string | null;
    phase_id?: string | null;
    parent_id?: number | null;
    payload_json?: string | null;
    tokens?: number | null;
    started_at?: string;
    seq?: number | null;
  },
): void {
  insert(db, "events", {
    run_id: e.run_id,
    type: e.type ?? "log",
    name: e.name ?? null,
    phase_id: e.phase_id ?? null,
    parent_id: e.parent_id ?? null,
    payload_json: e.payload_json ?? null,
    tokens: e.tokens ?? null,
    started_at: e.started_at ?? "2026-01-01T00:00:00Z",
    seq: e.seq ?? null,
  });
}

function addProc(
  db: Database,
  p: {
    run_id: string;
    kind: string;
    name?: string | null;
    pid?: number | null;
    started_at?: string | null;
    ended_at?: string | null;
    heartbeat_at?: string | null;
    retry_of?: string | null;
    retried_at?: string | null;
  },
): void {
  insert(db, "processes", {
    run_id: p.run_id,
    kind: p.kind,
    name: p.name ?? null,
    pid: p.pid ?? null,
    started_at: p.started_at ?? null,
    ended_at: p.ended_at ?? null,
    heartbeat_at: p.heartbeat_at ?? null,
    retry_of: p.retry_of ?? null,
    retried_at: p.retried_at ?? null,
  });
}

function addChild(
  db: Database,
  c: {
    batch_id: string;
    position: number;
    ticket: string;
    state?: string;
    reason?: string | null;
    updated_at?: string;
  },
): void {
  insert(db, "batch_children", {
    batch_id: c.batch_id,
    position: c.position,
    ticket: c.ticket,
    state: c.state ?? "queued",
    reason: c.reason ?? null,
    updated_at: c.updated_at ?? "2026-11-06T11:59:00Z",
  });
}

describe("feed reads", () => {
  test("run detail phases carry stage and sessions carry the resolved budget", () => {
    const { path, db } = makeProject("stage-budget", CURRENT_SCHEMA);
    addRun(db, { run_id: "r1", started_at: "2026-01-01T00:00:00Z" });
    addPhase(db, {
      phase_id: "r1:01",
      run_id: "r1",
      seq: 1,
      owner: "builder",
      stage: "Build",
    });
    addSession(db, {
      session_id: "r1:01-builder",
      run_id: "r1",
      phase_id: "r1:01",
      role: "builder",
      budget_warn: 150000,
      budget_stop: 250000,
    });
    addProc(db, { run_id: "r1", kind: "run", pid: 4242 });

    const lime = new LimeDb("stage-budget", path);
    const detail = lime.runDetail("r1");
    expect(detail?.phases[0]?.stage).toBe("Build");
    expect(detail?.sessions[0]?.budget_warn).toBe(150000);
    expect(detail?.sessions[0]?.budget_stop).toBe(250000);
    lime.close();
  });

  test("a ledger from before 251 reads stage and budget as null", () => {
    const { path, db } = makeProject("legacy-null", LEGACY_SCHEMA);
    addRun(db, { run_id: "r1", started_at: "2026-01-01T00:00:00Z" });
    addPhase(db, { phase_id: "r1:01", run_id: "r1", seq: 1 });
    addSession(db, {
      session_id: "r1:01-builder",
      run_id: "r1",
      phase_id: "r1:01",
      role: "builder",
    });

    const lime = new LimeDb("legacy-null", path);
    const detail = lime.runDetail("r1");
    expect(detail?.phases[0]?.stage).toBeNull();
    expect(detail?.sessions[0]?.budget_warn).toBeNull();
    expect(detail?.sessions[0]?.budget_stop).toBeNull();
    lime.close();
  });

  test("eventsAfterSeq pages by seq in pages of at most 1000", () => {
    const { path, db } = makeProject("seq-pages", CURRENT_SCHEMA);
    addRun(db, { run_id: "r1", started_at: "2026-01-01T00:00:00Z" });
    for (let i = 1; i <= 2500; i++) addEvent(db, { run_id: "r1", seq: i });

    const lime = new LimeDb("seq-pages", path);
    const p1 = lime.eventsAfterSeq("r1");
    expect([p1.events.length, p1.next, p1.has_more]).toEqual([1000, 1000, true]);
    const p2 = lime.eventsAfterSeq("r1", p1.next);
    expect([p2.events.length, p2.next, p2.has_more]).toEqual([1000, 2000, true]);
    const p3 = lime.eventsAfterSeq("r1", p2.next);
    expect([p3.events.length, p3.next, p3.has_more]).toEqual([500, 2500, false]);

    const empty = lime.eventsAfterSeq("r1", 2500);
    expect(empty.events).toEqual([]);
    expect(empty.next).toBe(2500);
    expect(empty.has_more).toBe(false);

    const clamped = lime.eventsAfterSeq("r1", 0, 5000);
    expect(clamped.events.length).toBe(1000);
    expect(clamped.has_more).toBe(true);
    lime.close();
  });

  test("eventsAfterSeq links parents by seq and parses the payload", () => {
    const { path, db } = makeProject("seq-parent", CURRENT_SCHEMA);
    addRun(db, { run_id: "r1", started_at: "2026-01-01T00:00:00Z" });
    addEvent(db, { run_id: "r1", seq: 1, type: "agent_start", name: "builder" });
    addEvent(db, {
      run_id: "r1",
      seq: 2,
      type: "tool_call",
      parent_id: 1,
      payload_json: JSON.stringify({ tool: "bash" }),
    });
    addEvent(db, { run_id: "r1", seq: 3, type: "tool_call", payload_json: null });

    const lime = new LimeDb("seq-parent", path);
    const page = lime.eventsAfterSeq("r1");
    expect(page.events[1]?.parent_seq).toBe(1);
    expect(page.events[1]?.payload).toEqual({ tool: "bash" });
    expect(page.events[2]?.payload).toBeNull();
    lime.close();
  });

  test("a ledger without events.seq numbers each Run's events in event_id order", () => {
    const { path, db } = makeProject("legacy-seq", LEGACY_SCHEMA);
    addRun(db, { run_id: "r1", started_at: "2026-01-01T00:00:00Z" });
    addRun(db, { run_id: "r2", started_at: "2026-01-01T00:00:00Z" });
    addEvent(db, { run_id: "r1", name: "a" });
    addEvent(db, { run_id: "r2", name: "b" });
    addEvent(db, { run_id: "r1", name: "c" });
    addEvent(db, { run_id: "r2", name: "d" });

    const lime = new LimeDb("legacy-seq", path);
    const r1 = lime.eventsAfterSeq("r1");
    expect(r1.events.map((e) => [e.seq, e.name])).toEqual([
      [1, "a"],
      [2, "c"],
    ]);
    const r2 = lime.eventsAfterSeq("r2");
    expect(r2.events.map((e) => [e.seq, e.name])).toEqual([
      [1, "b"],
      [2, "d"],
    ]);
    const skip = lime.eventsAfterSeq("r1", 1);
    expect(skip.events.map((e) => e.name)).toEqual(["c"]);
    expect(skip.next).toBe(2);
    lime.close();
  });
});

interface LivenessCase {
  name: string;
  now: string;
  kind: string;
  run: { run_id: string; started_at: string };
  processes: {
    kind: string;
    pid: number | null;
    started_at: string | null;
    ended_at: string | null;
    heartbeat_at: string | null;
  }[];
  alive_pids: number[];
  verdict: "live" | "starting" | "dead";
}

const LIVENESS_FIXTURE = JSON.parse(
  readFileSync(
    resolve(import.meta.dir, "..", "..", "tests", "fixtures", "liveness.json"),
    "utf8",
  ),
) as { about: string; cases: LivenessCase[] };

describe("liveness", () => {
  for (const c of LIVENESS_FIXTURE.cases) {
    test(`liveness fixture: ${c.name}`, () => {
      const result = judge(
        c.processes,
        c.run.started_at,
        Date.parse(c.now),
        c.kind,
        (pid) => c.alive_pids.includes(pid ?? -1),
      );
      expect(result.verdict).toBe(c.verdict);
    });
  }

  test("the fixture holds every verdict and the 45 s window", () => {
    const verdicts = new Set(LIVENESS_FIXTURE.cases.map((c) => c.verdict));
    for (const v of ["live", "starting", "dead"] as const) {
      expect(verdicts).toContain(v);
    }
    expect(LIVENESS_MS).toBe(45_000);
  });

  test("parseStamp reads a naive stamp as UTC and garbage as null", () => {
    expect(parseStamp("2026-11-06T11:59:50")).toBe(
      Date.parse("2026-11-06T11:59:50Z"),
    );
    expect(parseStamp("2026-11-06 11:59:50")).toBe(
      Date.parse("2026-11-06T11:59:50Z"),
    );
    expect(parseStamp("2026-11-06T11:59:50+00:00")).toBe(
      Date.parse("2026-11-06T11:59:50+00:00"),
    );
    expect(parseStamp("garbage")).toBeNull();
    expect(parseStamp("")).toBeNull();
    expect(parseStamp(null)).toBeNull();
  });

  test("pidAlive: this process is alive, a non-positive pid is not", () => {
    expect(pidAlive(process.pid)).toBe(true);
    expect(pidAlive(0)).toBe(false);
    expect(pidAlive(-1)).toBe(false);
    expect(pidAlive(null)).toBe(false);
  });
});

const NOW = Date.parse("2026-11-06T12:00:00Z");

/** Write a registry file over `paths` and open a real registry over it. */
function registryFor(paths: string[]): LimeRegistry {
  const file = join(root, `registry-${Math.random().toString(36).slice(2)}.txt`);
  writeFileSync(file, paths.join("\n") + "\n");
  return new LimeRegistry(file);
}

describe("LimeFeedSource reads", () => {
  test("running lists a beating Run with the asset's fields", async () => {
    const name = "live-run";
    const { db } = makeProject(name, CURRENT_SCHEMA);
    addRun(db, {
      run_id: "r1",
      started_at: "2026-11-06T08:00:00Z",
      workflow: "feature",
      ticket: "42",
    });
    addPhase(db, {
      phase_id: "r1:01",
      run_id: "r1",
      seq: 1,
      name: "build",
      owner: "builder",
      status: "running",
      stage: "Build",
    });
    addSession(db, {
      session_id: "r1:01-builder",
      run_id: "r1",
      phase_id: "r1:01",
      role: "builder",
      budget_warn: 150000,
      budget_stop: 250000,
    });
    addEvent(db, {
      run_id: "r1",
      type: "turn",
      name: "r1:01-builder",
      tokens: 1000,
      started_at: "2026-11-06T11:59:30Z",
    });
    addEvent(db, {
      run_id: "r1",
      type: "turn",
      name: "r1:01-builder",
      tokens: 42000,
      started_at: "2026-11-06T11:59:40Z",
    });
    // A resumed Run: the old row is open but stale, the new row beats.
    addProc(db, {
      run_id: "r1",
      kind: "run",
      pid: 1111,
      started_at: "2026-11-06T08:00:00Z",
      heartbeat_at: "2026-11-06T11:50:00Z",
    });
    addProc(db, {
      run_id: "r1",
      kind: "run",
      pid: 2222,
      started_at: "2026-11-06T11:00:00Z",
      heartbeat_at: "2026-11-06T11:59:50Z",
    });
    addProc(db, {
      run_id: "r1",
      kind: "agent",
      pid: 3333,
      started_at: "2026-11-06T11:59:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });

    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, {
      now: () => NOW,
      pidAlive: () => false,
    });
    expect(await source.running()).toEqual({
      runs: [
        {
          project: name,
          run_id: "r1",
          kind: "run",
          workflow: "feature",
          ticket: "42",
          batch_id: null,
          batch_position: null,
          attempt_started_at: "2026-11-06T11:00:00Z",
          heartbeat_at: "2026-11-06T11:59:50Z",
          phase: { seq: 1, name: "build", stage: "Build", status: "running" },
          session: { role: "builder", model: "x-ai/grok-4.6" },
          context: { tokens: 42000, warn: 150000, stop: 250000 },
          last_event_at: "2026-11-06T11:59:40Z",
        },
      ],
      queued: [],
      batches: [],
    });
    registry.close();
    db.close();
  });

  test("running drops stale, closed, agent-only and scout Runs", async () => {
    const name = "live-drop";
    const { db } = makeProject(name, CURRENT_SCHEMA);
    addRun(db, { run_id: "stale", started_at: "2026-11-06T08:00:00Z" });
    addProc(db, {
      run_id: "stale",
      kind: "run",
      pid: 1,
      started_at: "2026-11-06T08:00:00Z",
      heartbeat_at: "2026-11-06T11:50:00Z",
    });
    addRun(db, { run_id: "closed", started_at: "2026-11-06T08:00:00Z" });
    addProc(db, {
      run_id: "closed",
      kind: "run",
      pid: 2,
      started_at: "2026-11-06T11:00:00Z",
      ended_at: "2026-11-06T11:30:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });
    addRun(db, { run_id: "agent-only", started_at: "2026-11-06T08:00:00Z" });
    addProc(db, {
      run_id: "agent-only",
      kind: "agent",
      pid: 3,
      started_at: "2026-11-06T11:59:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });
    addRun(db, {
      run_id: "scout",
      started_at: "2026-11-06T08:00:00Z",
      kind: "scout",
    });
    addProc(db, {
      run_id: "scout",
      kind: "run",
      pid: 4,
      started_at: "2026-11-06T11:00:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });

    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, {
      now: () => NOW,
      pidAlive: () => false,
    });
    expect((await source.running()).runs).toEqual([]);
    registry.close();
    db.close();
  });

  test("running lists a beating deploy row as kind deploy and research/prototype by their kind", async () => {
    const name = "live-kinds";
    const { db } = makeProject(name, CURRENT_SCHEMA);
    addRun(db, { run_id: "dep", started_at: "2026-11-06T08:00:00Z" });
    addProc(db, {
      run_id: "dep",
      kind: "run",
      pid: 10,
      started_at: "2026-11-06T08:00:00Z",
      ended_at: "2026-11-06T09:00:00Z",
      heartbeat_at: "2026-11-06T09:00:00Z",
    });
    addProc(db, {
      run_id: "dep",
      kind: "deploy",
      pid: 11,
      started_at: "2026-11-06T11:59:50Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });
    addRun(db, {
      run_id: "res",
      started_at: "2026-11-06T08:00:00Z",
      kind: "research",
    });
    addProc(db, {
      run_id: "res",
      kind: "run",
      pid: 12,
      started_at: "2026-11-06T11:00:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });
    addRun(db, {
      run_id: "pro",
      started_at: "2026-11-06T08:00:00Z",
      kind: "prototype",
    });
    addProc(db, {
      run_id: "pro",
      kind: "run",
      pid: 13,
      started_at: "2026-11-06T11:00:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });

    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, {
      now: () => NOW,
      pidAlive: () => false,
    });
    expect((await source.running()).runs.map((r) => [r.run_id, r.kind])).toEqual([
      ["dep", "deploy"],
      ["pro", "prototype"],
      ["res", "research"],
    ]);
    registry.close();
    db.close();
  });

  test("a ledger without heartbeat_at falls back to the pid probe", async () => {
    const name = "live-legacy";
    const { db } = makeProject(name, LEGACY_SCHEMA);
    addRun(db, { run_id: "r1", started_at: "2026-11-06T08:00:00Z" });
    addProc(db, {
      run_id: "r1",
      kind: "run",
      pid: 4242,
      started_at: "2026-11-06T11:00:00Z",
    });

    const registry = registryFor([join(root, name)]);
    const alive = new LimeFeedSource(registry, {
      now: () => NOW,
      pidAlive: () => true,
    });
    expect((await alive.running()).runs.map((r) => r.run_id)).toEqual(["r1"]);
    const dead = new LimeFeedSource(registry, {
      now: () => NOW,
      pidAlive: () => false,
    });
    expect((await dead.running()).runs).toEqual([]);
    registry.close();
    db.close();
  });

  test("running lists a live batch's queued children and each Run's batch_id and position", async () => {
    const name = "live-batch";
    const { db } = makeProject(name, CURRENT_SCHEMA);
    addProc(db, {
      run_id: "b1",
      kind: "batch",
      pid: 50,
      started_at: "2026-11-06T11:59:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });
    addChild(db, { batch_id: "b1", position: 0, ticket: "240-a", state: "started" });
    addChild(db, { batch_id: "b1", position: 1, ticket: "241-b", state: "queued", reason: "slot" });
    addChild(db, { batch_id: "b1", position: 2, ticket: "242-c", state: "queued", reason: null });
    addChild(db, { batch_id: "b1", position: 3, ticket: "243-d", state: "dropped", reason: "no ticket file" });
    addRun(db, {
      run_id: "r1",
      started_at: "2026-11-06T11:00:00Z",
      batch_id: "b1",
      ticket: "/x/.lime/tickets/240-a.md",
    });
    addProc(db, {
      run_id: "r1",
      kind: "run",
      pid: 1,
      started_at: "2026-11-06T11:00:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });
    addRun(db, { run_id: "r2", started_at: "2026-11-06T11:00:00Z" });
    addProc(db, {
      run_id: "r2",
      kind: "run",
      pid: 2,
      started_at: "2026-11-06T11:00:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });

    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, {
      now: () => NOW,
      pidAlive: () => false,
    });
    const result = await source.running();
    expect(result.queued).toEqual([
      { project: name, batch_id: "b1", position: 1, ticket: "241-b", reason: "slot" },
      { project: name, batch_id: "b1", position: 2, ticket: "242-c", reason: null },
    ]);
    expect(result.batches).toEqual([
      { project: name, batch_id: "b1", retry_of: null, died_at: null },
    ]);
    const r1 = result.runs.find((r) => r.run_id === "r1")!;
    expect([r1.batch_id, r1.batch_position]).toEqual(["b1", 0]);
    const r2 = result.runs.find((r) => r.run_id === "r2")!;
    expect([r2.batch_id, r2.batch_position]).toEqual([null, null]);
    registry.close();
    db.close();
  });

  test("a batch closed by SIGTERM hides its queued rows at once", async () => {
    const name = "batch-sigterm";
    const { db } = makeProject(name, CURRENT_SCHEMA);
    addProc(db, {
      run_id: "b1",
      kind: "batch",
      pid: 50,
      started_at: "2026-11-06T11:59:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });
    addChild(db, { batch_id: "b1", position: 0, ticket: "241-b", state: "queued", reason: "slot" });

    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, {
      now: () => NOW,
      pidAlive: () => false,
    });
    expect((await source.running()).queued).toEqual([
      { project: name, batch_id: "b1", position: 0, ticket: "241-b", reason: "slot" },
    ]);

    db.query("UPDATE processes SET ended_at = ? WHERE run_id = 'b1'").run(
      "2026-11-06T11:59:56Z",
    );
    const after = await source.running();
    expect(after.queued).toEqual([]);
    expect(after.batches).toEqual([]);
    registry.close();
    db.close();
  });

  test("a silently killed batch hides its queued rows once its beat is 45 s old, whatever the pid", async () => {
    const name = "batch-stale";
    const { db } = makeProject(name, CURRENT_SCHEMA);
    addProc(db, {
      run_id: "b1",
      kind: "batch",
      pid: 50,
      started_at: "2026-11-06T11:59:00Z",
      heartbeat_at: "2026-11-06T11:59:15.001Z",
    });
    addChild(db, { batch_id: "b1", position: 0, ticket: "241-b", state: "queued" });

    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, {
      now: () => NOW,
      pidAlive: () => true,
    });
    expect((await source.running()).batches).toEqual([
      { project: name, batch_id: "b1", retry_of: null, died_at: null },
    ]);

    db.query("UPDATE processes SET heartbeat_at = ? WHERE run_id = 'b1'").run(
      "2026-11-06T11:59:15.000Z",
    );
    const after = await source.running();
    expect(after.queued).toEqual([]);
    expect(after.batches).toEqual([]);
    registry.close();
    db.close();
  });

  test("a retry batch's header carries the dead batch's last beat and adopts its orphaned Runs", async () => {
    const name = "batch-retry";
    const { db } = makeProject(name, CURRENT_SCHEMA);
    addProc(db, {
      run_id: "d1",
      kind: "batch",
      pid: 40,
      started_at: "2026-11-06T11:00:00Z",
      heartbeat_at: "2026-11-06T11:02:00.123456+00:00",
      retried_at: "2026-11-06T11:05:00Z",
    });
    addProc(db, {
      run_id: "t1",
      kind: "batch",
      pid: 41,
      started_at: "2026-11-06T11:59:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
      retry_of: "d1",
    });
    addChild(db, { batch_id: "d1", position: 0, ticket: "245-x", state: "started" });
    addChild(db, { batch_id: "d1", position: 1, ticket: "246-y", state: "queued" });
    addChild(db, { batch_id: "t1", position: 0, ticket: "246-y", state: "queued", reason: "blocked 245" });
    addRun(db, {
      run_id: "o1",
      started_at: "2026-11-06T11:00:00Z",
      batch_id: "d1",
      ticket: ".lime/tickets/245-x.md",
    });
    addProc(db, {
      run_id: "o1",
      kind: "run",
      pid: 42,
      started_at: "2026-11-06T11:00:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });

    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, {
      now: () => NOW,
      pidAlive: () => false,
    });
    const result = await source.running();
    expect(result.batches).toEqual([
      {
        project: name,
        batch_id: "t1",
        retry_of: "d1",
        died_at: "2026-11-06T11:02:00.123456+00:00",
      },
    ]);
    expect(result.queued).toEqual([
      { project: name, batch_id: "t1", position: 0, ticket: "246-y", reason: "blocked 245" },
    ]);
    const o1 = result.runs.find((r) => r.run_id === "o1")!;
    expect([o1.batch_id, o1.batch_position]).toEqual(["d1", 0]);
    registry.close();
    db.close();
  });

  test("a ledger without batch tables reads no batches", async () => {
    const name = "batch-legacy";
    const { db } = makeProject(name, LEGACY_SCHEMA);
    addRun(db, { run_id: "r1", started_at: "2026-11-06T08:00:00Z" });
    addProc(db, {
      run_id: "r1",
      kind: "run",
      pid: 4242,
      started_at: "2026-11-06T11:00:00Z",
    });

    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, {
      now: () => NOW,
      pidAlive: () => true,
    });
    const result = await source.running();
    expect(result.queued).toEqual([]);
    expect(result.batches).toEqual([]);
    expect(result.runs[0]?.batch_id).toBeNull();
    registry.close();
    db.close();
  });

  test("runHeader is the run detail plus envelopes, gates and live", async () => {
    const name = "live-header";
    const { db } = makeProject(name, CURRENT_SCHEMA);
    addRun(db, {
      run_id: "r1",
      started_at: "2026-11-06T08:00:00Z",
      ticket: "42",
    });
    addPhase(db, {
      phase_id: "r1:01",
      run_id: "r1",
      seq: 1,
      name: "build",
      owner: "builder",
      status: "running",
      stage: "Build",
    });
    addProc(db, {
      run_id: "r1",
      kind: "run",
      pid: 1,
      started_at: "2026-11-06T11:00:00Z",
      heartbeat_at: "2026-11-06T11:59:55Z",
    });
    insert(db, "envelopes", {
      envelope_id: 1,
      run_id: "r1",
      phase_id: "r1:01",
      agent: "builder",
      output_type: "Build",
      payload_json: "{}",
      valid: 1,
      attempt: 1,
      created_at: "2026-11-06T11:30:00Z",
    });
    insert(db, "gate_results", {
      id: 1,
      run_id: "r1",
      phase_id: "r1:01",
      attempt: 1,
      gate: "tests",
      passed: 1,
      violations_json: "[]",
      checks_json: null,
      created_at: "2026-11-06T11:40:00Z",
    });

    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, {
      now: () => NOW,
      pidAlive: () => false,
    });

    const live = await source.runHeader(name, "r1");
    expect(live?.live).toBe(true);
    expect(live?.run.run_id).toBe("r1");
    expect(live?.phases[0]?.stage).toBe("Build");
    expect(live?.envelopes.length).toBe(1);
    expect(live?.gates.length).toBe(1);

    db.query("UPDATE processes SET ended_at = ? WHERE run_id = ?").run(
      "2026-11-06T12:00:00Z",
      "r1",
    );
    const closed = await source.runHeader(name, "r1");
    expect(closed?.live).toBe(false);

    expect(await source.runHeader(name, "nope")).toBeNull();
    expect(await source.runHeader("unknown", "r1")).toBeNull();

    registry.close();
    db.close();
  });

  test("eventsAfter of an unknown project is an empty page at the given seq", async () => {
    const name = "live-events";
    const { db } = makeProject(name, CURRENT_SCHEMA);
    addRun(db, { run_id: "r1", started_at: "2026-11-06T08:00:00Z" });
    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, { now: () => NOW });
    expect(await source.eventsAfter("unknown", "r1", 7, 100)).toEqual({
      events: [],
      next: 7,
      has_more: false,
    });
    registry.close();
    db.close();
  });
});

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

describe("LimeFeedSource change signal", () => {
  test("the debounce is 200 ms and the tick 5 s", () => {
    expect(LIVE_DEBOUNCE_MS).toBe(200);
    expect(LIVE_TICK_MS).toBe(5_000);
  });

  test("a burst of lime.db-wal writes yields one project change after the debounce", async () => {
    const name = "watch-burst";
    const { path, db } = makeProject(name, CURRENT_SCHEMA);
    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, { tickMs: 60_000 });
    const changes: FeedChange[] = [];
    source.onChange((c) => changes.push(c));
    try {
      source.start();
      for (let i = 0; i < 3; i++) {
        writeFileSync(`${path}-wal`, `write ${i}`);
        await sleep(30);
      }
      expect(changes).toEqual([]);
      await sleep(700);
      expect(changes).toEqual([{ type: "project", project: name }]);
    } finally {
      source.stop();
      registry.close();
      db.close();
    }
  });

  test("the tick fires every tickMs and stop() ends it", async () => {
    const name = "watch-tick";
    const { db } = makeProject(name, CURRENT_SCHEMA);
    const registry = registryFor([join(root, name)]);
    const source = new LimeFeedSource(registry, { tickMs: 25 });
    const changes: FeedChange[] = [];
    source.onChange((c) => changes.push(c));
    try {
      source.start();
      source.start();
      await sleep(150);
      expect(changes.filter((c) => c.type === "tick").length).toBeGreaterThanOrEqual(2);
      source.stop();
      source.stop();
      const after = changes.length;
      await sleep(100);
      expect(changes.length).toBe(after);
    } finally {
      source.stop();
      registry.close();
      db.close();
    }
  });

  test("a throwing subscriber is logged and the others still get the change", async () => {
    const name = "watch-throw";
    const { db } = makeProject(name, CURRENT_SCHEMA);
    const registry = registryFor([join(root, name)]);
    const lines: string[] = [];
    const source = new LimeFeedSource(registry, {
      tickMs: 25,
      log: (line) => lines.push(line),
    });
    const changes: FeedChange[] = [];
    source.onChange(() => {
      throw new Error("boom");
    });
    source.onChange((c) => changes.push(c));
    try {
      source.start();
      await sleep(80);
      expect(changes.some((c) => c.type === "tick")).toBe(true);
      expect(lines.some((l) => l.includes("[live] subscriber threw:"))).toBe(true);
    } finally {
      source.stop();
      registry.close();
      db.close();
    }
  });

  test("a project without .lime/runs is skipped and the tick still runs", async () => {
    const name = "watch-missing";
    const dir = join(root, name, "project");
    mkdirSync(dir, { recursive: true });
    const registry = registryFor([dir]);
    const lines: string[] = [];
    const source = new LimeFeedSource(registry, {
      tickMs: 25,
      log: (line) => lines.push(line),
    });
    const changes: FeedChange[] = [];
    source.onChange((c) => changes.push(c));
    try {
      source.start();
      await sleep(80);
      expect(changes.some((c) => c.type === "tick")).toBe(true);
      expect(lines).toEqual([]);
    } finally {
      source.stop();
      registry.close();
    }
  });
});

const HOME: HomeFocusResponse = { report: null, goals: null, errors: [] };

/** A minimal `RunDetail` stand-in; `marker` distinguishes two headers. */
function feedRun(project: string, runId: string, marker = 0): FeedRun {
  return {
    run: { run_id: runId, workflow: "feature", project },
    usage: {},
    phases: [],
    sessions: [],
    envelopes: [],
    gates: [],
    live: true,
    marker,
  } as unknown as FeedRun;
}

function running(project: string, runId: string): RunningRun {
  return {
    project,
    run_id: runId,
    kind: "run",
    workflow: "feature",
    ticket: null,
    batch_id: null,
    batch_position: null,
    attempt_started_at: null,
    heartbeat_at: null,
    phase: null,
    session: null,
    context: null,
    last_event_at: null,
  };
}

function eventsSeq(runId: string, from: number, to: number): FeedEvent[] {
  const out: FeedEvent[] = [];
  for (let i = from; i <= to; i++) {
    out.push({
      seq: i,
      type: null,
      name: runId,
      phase_id: null,
      parent_seq: null,
      payload: null,
      tokens: null,
      started_at: null,
    });
  }
  return out;
}

class FakeSource implements FeedSource {
  runs: RunningRun[] = [];
  queued: QueuedChild[] = [];
  batches: LiveBatch[] = [];
  readonly headers = new Map<string, FeedRun | null>();
  readonly events = new Map<string, FeedEvent[]>();
  readonly fail = new Set<"running" | "runHeader" | "eventsAfter">();
  readonly eventsCalls: Array<{ project: string; run_id: string; seq: number }> = [];
  private readonly subs = new Set<(change: FeedChange) => void>();
  focusValue: HomeFocusResponse | null = null;
  refreshCalls = 0;
  private readonly focusSubs = new Set<(res: HomeFocusResponse) => void>();

  private maybeFail(kind: "running" | "runHeader" | "eventsAfter"): void {
    if (this.fail.has(kind)) {
      this.fail.delete(kind);
      throw new Error(`${kind} failed`);
    }
  }

  async running(): Promise<Required<LiveRunning>> {
    this.maybeFail("running");
    return { runs: this.runs, queued: this.queued, batches: this.batches };
  }

  async runHeader(project: string, runId: string): Promise<FeedRun | null> {
    this.maybeFail("runHeader");
    return this.headers.get(`${project}:${runId}`) ?? null;
  }

  async eventsAfter(
    project: string,
    runId: string,
    seq: number,
    limit: number,
  ): Promise<FeedEventsPage> {
    this.eventsCalls.push({ project, run_id: runId, seq });
    this.maybeFail("eventsAfter");
    const all = this.events.get(`${project}:${runId}`) ?? [];
    const page = all.filter((e) => e.seq > seq).slice(0, limit);
    const next = page.length > 0 ? page[page.length - 1].seq : seq;
    return { events: page, next, has_more: page.length === limit };
  }

  onChange(cb: (change: FeedChange) => void): () => void {
    this.subs.add(cb);
    return () => {
      this.subs.delete(cb);
    };
  }

  emit(change: FeedChange): void {
    for (const cb of this.subs) cb(change);
  }

  get subscriberCount(): number {
    return this.subs.size;
  }

  focusLatest(): HomeFocusResponse | null {
    return this.focusValue;
  }

  async focusRead(): Promise<HomeFocusResponse> {
    this.refreshCalls++;
    return this.focusValue ?? HOME;
  }

  onFocus(fn: (res: HomeFocusResponse) => void): () => void {
    this.focusSubs.add(fn);
    return () => {
      this.focusSubs.delete(fn);
    };
  }

  publishFocus(res: HomeFocusResponse): void {
    this.focusValue = res;
    for (const fn of this.focusSubs) fn(res);
  }

  get focusSubscriberCount(): number {
    return this.focusSubs.size;
  }
}

interface Frame {
  event: string;
  data: string;
}

/** A stateful SSE reader: it buffers partial chunks and hands back the frames
 *  since the last call, ignoring `:` comments. `until` throws on timeout. */
function frameReader(res: Response): {
  until(pred: (f: Frame) => boolean, ms?: number): Promise<Frame[]>;
} {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let pending: ReturnType<typeof reader.read> | null = null;
  const queue: Frame[] = [];

  const parse = (): void => {
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let event = "";
      const dataLines: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith(":")) continue;
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      if (event !== "" || dataLines.length > 0) {
        queue.push({ event, data: dataLines.join("\n") });
      }
    }
  };

  const fill = async (deadline: number): Promise<void> => {
    if (pending === null) pending = reader.read();
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error("frameReader timeout");
    let timer: ReturnType<typeof setTimeout> | null = null;
    const timeout = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), remaining);
    });
    let result: Awaited<ReturnType<typeof reader.read>> | null;
    try {
      result = await Promise.race([pending, timeout]);
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
    if (result === null) throw new Error("frameReader timeout");
    pending = null;
    if (result.done) throw new Error("stream ended");
    buffer += decoder.decode(result.value, { stream: true });
    parse();
  };

  const until = async (
    pred: (f: Frame) => boolean,
    ms = 2000,
  ): Promise<Frame[]> => {
    const deadline = Date.now() + ms;
    const collected: Frame[] = [];
    for (;;) {
      while (queue.length > 0) {
        const f = queue.shift()!;
        collected.push(f);
        if (pred(f)) return collected;
      }
      await fill(deadline);
    }
  };

  return { until };
}

describe("liveStream", () => {
  test("opens with a retry hint and the sse headers", async () => {
    const controller = new AbortController();
    const res = liveStream({
      source: new FakeSource(),
      cursors: [],
      signal: controller.signal,
    });
    expect(res.headers.get("content-type")).toBe(
      "text/event-stream; charset=utf-8",
    );
    expect(res.headers.get("cache-control")).toBe("no-store");
    const reader = res.body!.getReader();
    const first = await reader.read();
    const text = new TextDecoder().decode(first.value);
    expect(text).toContain("retry: 3000");
    expect(text).toContain(": open");
    controller.abort();
  });

  test("a focus publish yields exactly one focus event", async () => {
    const source = new FakeSource();
    source.focusValue = HOME;
    const controller = new AbortController();
    const res = liveStream({
      source,
      cursors: [],
      signal: controller.signal,
    });
    const frames = await frameReader(res).until(
      (f) => f.event === "focus" && source.focusSubscriberCount === 1,
    );
    expect(frames.filter((f) => f.event === "focus").length).toBe(1);
    controller.abort();
  });

  test("a keep-alive ping is sent every pingMs", async () => {
    const source = new FakeSource();
    source.focusValue = HOME;
    const controller = new AbortController();
    const res = liveStream({
      source,
      cursors: [],
      signal: controller.signal,
      pingMs: 50,
    });
    const reader = res.body!.getReader();
    let text = "";
    const deadline = Date.now() + 2000;
    while (!text.includes(": ping")) {
      if (Date.now() > deadline) throw new Error("no ping");
      const r = await reader.read();
      if (r.done) break;
      text += new TextDecoder().decode(r.value);
    }
    expect(text).toContain(": ping");
    controller.abort();
  });

  test("aborting the signal unsubscribes focus and the source and mutes later publishes", async () => {
    const source = new FakeSource();
    source.focusValue = HOME;
    const controller = new AbortController();
    const res = liveStream({
      source,
      cursors: [],
      signal: controller.signal,
    });
    const reader = frameReader(res);
    await reader.until(
      (f) => f.event === "focus" && source.focusSubscriberCount === 1,
    );
    expect(source.focusSubscriberCount).toBe(1);
    expect(source.subscriberCount).toBe(1);
    controller.abort();
    expect(source.focusSubscriberCount).toBe(0);
    expect(source.subscriberCount).toBe(0);
    source.publishFocus(HOME);
    source.emit({ type: "tick" });
    await expect(
      reader.until((f) => f.event === "focus", 150),
    ).rejects.toThrow();
  });

  test("the snapshot is focus, running, then each Run's run and its events in pages of 1000", async () => {
    const source = new FakeSource();
    source.focusValue = HOME;
    source.runs = [running("p", "r1")];
    source.headers.set("p:r1", feedRun("p", "r1"));
    source.headers.set("p:r2", feedRun("p", "r2"));
    source.events.set("p:r1", eventsSeq("r1", 1, 2500));
    source.events.set("p:r2", eventsSeq("r2", 1, 2));
    const controller = new AbortController();
    const res = liveStream({
      source,
      cursors: [
        { project: "p", run_id: "r1", seq: 0 },
        { project: "p", run_id: "r2", seq: 0 },
      ],
      signal: controller.signal,
    });
    const frames = await frameReader(res).until(
      (f) => f.event === "events" && JSON.parse(f.data).run_id === "r2",
    );
    expect(frames.map((f) => f.event)).toEqual([
      "focus",
      "running",
      "run",
      "events",
      "events",
      "events",
      "run",
      "events",
    ]);
    const eventsFrames = frames.filter((f) => f.event === "events");
    expect(eventsFrames.slice(0, 3).map((f) => JSON.parse(f.data).events.length)).toEqual([
      1000, 1000, 500,
    ]);
    expect(JSON.parse(eventsFrames[2].data).next).toBe(2500);
    expect(JSON.parse(eventsFrames[3].data).events.length).toBe(2);
    controller.abort();
  });

  test("with no cached focus the stream reads it before any subscribed Run data", async () => {
    const source = new FakeSource();
    source.runs = [running("p", "r1")];
    source.headers.set("p:r1", feedRun("p", "r1"));
    source.events.set("p:r1", []);
    const controller = new AbortController();
    const res = liveStream({
      source,
      cursors: [{ project: "p", run_id: "r1", seq: 0 }],
      signal: controller.signal,
    });
    const reader = frameReader(res);
    const frames = await reader.until((f) => f.event === "run");
    expect(source.refreshCalls).toBe(1);
    const events = frames.map((f) => f.event);
    expect(events.indexOf("focus")).toBeLessThan(events.indexOf("running"));
    expect(events.indexOf("focus")).toBeLessThan(events.indexOf("run"));
    controller.abort();
  });

  test("a reconnect with its cursors never repeats a line", async () => {
    const source = new FakeSource();
    source.focusValue = HOME;
    source.runs = [running("p", "r1")];
    source.headers.set("p:r1", feedRun("p", "r1"));
    source.events.set("p:r1", eventsSeq("r1", 1, 2500));
    const controller = new AbortController();
    const res = liveStream({
      source,
      cursors: [{ project: "p", run_id: "r1", seq: 2500 }],
      signal: controller.signal,
    });
    const reader = frameReader(res);
    const snap = await reader.until((f) => f.event === "run");
    expect(snap.some((f) => f.event === "events")).toBe(false);

    source.events.set("p:r1", eventsSeq("r1", 1, 2502));
    source.emit({ type: "project", project: "p" });
    const projectFrames = await reader.until((f) => f.event === "events");
    const eventsFrames = projectFrames.filter((f) => f.event === "events");
    expect(eventsFrames.length).toBe(1);
    expect(JSON.parse(eventsFrames[0].data).events.map((e: FeedEvent) => e.seq)).toEqual([
      2501, 2502,
    ]);

    source.emit({ type: "tick" });
    source.publishFocus(HOME);
    const after = await reader.until((f) => f.event === "focus");
    expect(after.some((f) => f.event === "events")).toBe(false);
    controller.abort();
  });

  test("running and run are pushed only when they changed, and a project change refreshes only that project", async () => {
    const source = new FakeSource();
    source.focusValue = HOME;
    source.runs = [running("p", "r1")];
    source.headers.set("p:r1", feedRun("p", "r1"));
    source.headers.set("q:r2", feedRun("q", "r2"));
    source.events.set("p:r1", []);
    source.events.set("q:r2", []);
    const controller = new AbortController();
    const res = liveStream({
      source,
      cursors: [
        { project: "p", run_id: "r1", seq: 0 },
        { project: "q", run_id: "r2", seq: 0 },
      ],
      signal: controller.signal,
    });
    const reader = frameReader(res);
    await reader.until(
      (f) => f.event === "run" && JSON.parse(f.data).run.run_id === "r2",
    );

    source.emit({ type: "tick" });
    source.publishFocus(HOME);
    let frames = await reader.until((f) => f.event === "focus");
    expect(
      frames.some((f) => f.event === "running" || f.event === "run"),
    ).toBe(false);

    source.runs = [running("p", "r1"), running("p", "r9")];
    source.headers.set("p:r1", feedRun("p", "r1", 1));
    source.emit({ type: "tick" });
    source.publishFocus(HOME);
    frames = await reader.until((f) => f.event === "focus");
    expect(frames.filter((f) => f.event === "running").length).toBe(1);
    expect(frames.filter((f) => f.event === "run").length).toBe(1);

    source.eventsCalls.length = 0;
    source.emit({ type: "project", project: "q" });
    source.publishFocus(HOME);
    await reader.until((f) => f.event === "focus");
    expect(source.eventsCalls.some((c) => c.project === "q")).toBe(true);
    expect(source.eventsCalls.every((c) => c.project === "q")).toBe(true);
    controller.abort();
  });

  test("a failed read in the tick or the debounce is logged, retried on the next tick, and the stream stays open", async () => {
    const source = new FakeSource();
    const log: string[] = [];
    source.focusValue = HOME;
    source.runs = [running("p", "r1")];
    source.headers.set("p:r1", feedRun("p", "r1"));
    source.events.set("p:r1", []);
    const controller = new AbortController();
    const res = liveStream({
      source,
      cursors: [{ project: "p", run_id: "r1", seq: 0 }],
      signal: controller.signal,
      log: (line) => log.push(line),
    });
    const reader = frameReader(res);
    await reader.until((f) => f.event === "run");

    source.runs = [running("p", "r1"), running("p", "r9")];
    source.headers.set("p:r1", feedRun("p", "r1", 1));
    source.events.set("p:r1", eventsSeq("r1", 1, 3));
    source.fail.add("running");
    source.fail.add("runHeader");
    source.fail.add("eventsAfter");
    source.emit({ type: "project", project: "p" });
    source.publishFocus(HOME);
    let frames = await reader.until((f) => f.event === "focus");
    expect(
      frames.some((f) => ["running", "run", "events"].includes(f.event)),
    ).toBe(false);
    expect(log.some((l) => l.includes("running read failed"))).toBe(true);
    expect(log.some((l) => l.includes("events read failed"))).toBe(true);

    source.emit({ type: "tick" });
    frames = await reader.until((f) => f.event === "events");
    expect(frames.some((f) => f.event === "running")).toBe(true);
    expect(frames.some((f) => f.event === "run")).toBe(true);
    const ev = frames.find((f) => f.event === "events")!;
    expect(JSON.parse(ev.data).events.map((e: FeedEvent) => e.seq)).toEqual([
      1, 2, 3,
    ]);

    source.publishFocus(HOME);
    const later = await reader.until((f) => f.event === "focus");
    expect(later.some((f) => f.event === "focus")).toBe(true);
    controller.abort();
  });

  test("a queued-only change pushes a new running frame", async () => {
    const source = new FakeSource();
    source.focusValue = HOME;
    source.runs = [running("p", "r1")];
    source.queued = [
      { project: "p", batch_id: "b1", position: 0, ticket: "245-x", reason: "slot" },
    ];
    const controller = new AbortController();
    const res = liveStream({
      source,
      cursors: [],
      signal: controller.signal,
    });
    const reader = frameReader(res);
    const first = await reader.until((f) => f.event === "running");
    expect(JSON.parse(first.find((f) => f.event === "running")!.data).queued).toEqual([
      { project: "p", batch_id: "b1", position: 0, ticket: "245-x", reason: "slot" },
    ]);

    source.queued = [];
    source.emit({ type: "tick" });
    source.publishFocus(HOME);
    const frames = await reader.until((f) => f.event === "focus");
    const runningFrames = frames.filter((f) => f.event === "running");
    expect(runningFrames.length).toBe(1);
    expect(JSON.parse(runningFrames[0]!.data).queued).toEqual([]);
    controller.abort();
  });

  test("parseCursors reads project:run_id:seq and refuses anything else", () => {
    expect(parseCursors(["p:r:0", "q:r2:12"])).toEqual([
      { project: "p", run_id: "r", seq: 0 },
      { project: "q", run_id: "r2", seq: 12 },
    ]);
    expect(parseCursors(["p:r"])).toBeNull();
    expect(parseCursors(["p:r:x"])).toBeNull();
    expect(parseCursors(["../x:r:0"])).toBeNull();
    expect(parseCursors(["p:r:1:2"])).toBeNull();
    expect(parseCursors(["p:r:0", "p:r:5"])).toEqual([
      { project: "p", run_id: "r", seq: 5 },
    ]);
    expect(parseCursors(["p:..:0"])).toBeNull();
  });

  test("FocusWatcher.latest() is null before the first read and the read's result after", async () => {
    const home: HomeFocusResponse = { report: null, goals: null, errors: [] };
    const watcher = new FocusWatcher({
      projects: [],
      homeDir: join(root, "home-latest"),
      read: async () => home,
    });
    expect(watcher.latest()).toBeNull();
    const res = await watcher.refresh();
    expect(res).toBe(home);
    expect(watcher.latest()).toBe(home);
  });

  test("the constants match the spec", () => {
    expect(PING_MS).toBe(15_000);
    expect(EVENTS_PAGE).toBe(1000);
    expect(RETRY_MS).toBe(3000);
  });
});
