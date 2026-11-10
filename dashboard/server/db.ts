/**
 * SQLite reader over every registered project's lime.db.
 *
 * The registry (`~/.lime/projects`, written by `lime init`) is a plain-text
 * file: one absolute project path per line, `#` comments and blanks ignored.
 * Each project's trace db lives at `<project>/.lime/runs/lime.db`; a project
 * registered before its first Run has no db yet, so missing dbs are probed
 * again on every poll and picked up the moment they appear.
 *
 * EVERY connection is opened readonly and every query is a SELECT — the
 * writers are the tracers of running Spine processes, and WAL lets us read
 * straight through their inserts. Unlike a visualizer that archives, there is no
 * archive write here: `lime dash` never writes to any db (ticket 39).
 */
import { Database } from "bun:sqlite";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { homedir } from "node:os";
import type {
  AgentStartPayload,
  Envelope,
  Event,
  EventsPage,
  EventType,
  FeedEvent,
  FeedEventsPage,
  GateResult,
  LiveKind,
  Phase,
  PhaseStatus,
  ProjectInfo,
  ProviderSpend,
  Run,
  RunDetail,
  RunStatus,
  RunSummary,
  RunUsage,
  RunningRun,
  Session,
  SessionCost,
} from "../shared/types.ts";
import { isCashModel } from "../shared/types.ts";

const MAX_LIMIT = 1000;
const DEFAULT_LIMIT = 500;

/** Table names used by `hasColumn` — always internal constants, never user input. */
const TABLE_PHASES = "phases";
const TABLE_EVENTS = "events";
const TABLE_SESSIONS = "sessions";
const TABLE_PROCESSES = "processes";
const TABLE_RUNS = "runs";
const TABLE_BATCH_CHILDREN = "batch_children";

/** A row of the `processes` table, as the live feed reads it. */
export interface ProcessRow {
  id: number;
  run_id: string;
  kind: string | null;
  pid: number | null;
  started_at: string | null;
  ended_at: string | null;
  heartbeat_at: string | null;
}

/** A candidate live Run: its runs row plus every `run`/`deploy` process row. */
export interface LiveCandidate {
  run_id: string;
  kind: LiveKind;
  workflow: string | null;
  ticket: string | null;
  batch_id: string | null;
  started_at: string | null;
  processes: ProcessRow[];
}

/** One open `batch` process row beside the dead batch a retry retries. */
export interface BatchRow {
  batch_id: string;
  retry_of: string | null;
  died_at: string | null;
  process: ProcessRow;
}

/** One `batch_children` row, as the live feed reads it. */
export interface BatchChildRow {
  batch_id: string;
  position: number;
  ticket: string;
  state: string;
  reason: string | null;
}

/** A row of the `eventsAfterSeq` select, before mapping to a `FeedEvent`. */
interface EventSeqRow {
  seq: number;
  type: EventType | null;
  name: string | null;
  phase_id: string | null;
  parent_seq: number | null;
  payload_json: string | null;
  tokens: number | null;
  started_at: string | null;
}

/** Parse a `payload_json` at read time; null on a null or malformed value. */
function parsePayload(json: string | null): unknown {
  if (json === null) return null;
  try {
    return JSON.parse(json) as unknown;
  } catch {
    return null;
  }
}

export const DB_RELATIVE = ".lime/runs/lime.db";

/** Resolve the registry path: --registry arg wins, then LIME_PROJECTS, then ~/.lime/projects. */
export function resolveRegistryPath(argv: string[] = Bun.argv): string {
  const flagIndex = argv.indexOf("--registry");
  const inline = argv.find((a) => a.startsWith("--registry="));
  const raw =
    (flagIndex !== -1 ? argv[flagIndex + 1] : undefined) ??
    inline?.slice("--registry=".length) ??
    process.env.LIME_PROJECTS ??
    join(homedir(), ".lime", "projects");
  return isAbsolute(raw) ? raw : resolve(process.cwd(), raw);
}

export interface RegistryEntry {
  name: string;
  path: string;
  dbPath: string;
}

/** Parse the registry file. Duplicate repo names would collide in the API's
 *  project routes, so later entries lose, with a warning. */
export function readRegistry(registryPath: string): RegistryEntry[] {
  if (!existsSync(registryPath)) {
    throw new Error(
      `no project registry at ${registryPath}\n` +
        `\`lime init\` registers a repo there; LIME_PROJECTS=<file> overrides the location`,
    );
  }
  const entries: RegistryEntry[] = [];
  const seen = new Set<string>();
  for (const line of readFileSync(registryPath, "utf8").split("\n")) {
    const path = line.trim();
    if (!path || path.startsWith("#")) continue;
    const name = basename(path);
    if (seen.has(name)) {
      console.warn(
        `[db] registry: duplicate repo name "${name}" — skipping ${path}`,
      );
      continue;
    }
    seen.add(name);
    entries.push({ name, path, dbPath: join(path, DB_RELATIVE) });
  }
  return entries;
}

/** Readonly reader over ONE project's lime.db. */
export class LimeDb {
  readonly project: string;
  readonly path: string;
  /** Where the per-Run folders live: `.lime/runs/<run_id>/<seq>-<role>/…` —
   *  the db's own directory, since both sit in `.lime/runs/`. */
  readonly runsDir: string;
  readonly journalMode: string;
  /** True while reading an idle db through an immutable snapshot (see below). */
  immutable: boolean;
  /** db-file mtime at open — a change while immutable means the snapshot is stale. */
  private readonly openedMtimeMs: number;
  private readonly db: Database;
  /** `true` results of `hasColumn`, so a migrated-in column stays cached; a
   *  `false` is re-checked each time, so a writer's later ALTER is picked up. */
  private readonly columnCache = new Map<string, boolean>();

  constructor(project: string, path: string) {
    if (!existsSync(path)) {
      throw new Error(`lime.db not found at ${path}`);
    }
    this.project = project;
    this.path = path;
    this.runsDir = dirname(path);
    this.openedMtimeMs = statSync(path).mtimeMs;

    // bun's bundled SQLite cannot READ a WAL db over a plain readonly
    // connection while its -shm/-wal sidecars are absent — exactly the state a
    // cleanly closed tracer leaves behind. The failure is lazy (the open call
    // can succeed and the first real read throws "unable to open database
    // file"), so the probe query here is what actually decides. An idle db IS
    // immutable, so a snapshot connection is correct there; the registry's
    // probe() swaps it for a live readonly connection the moment a writer's
    // sidecars (or a new mtime) appear. Never open rw here: lime dash makes
    // zero writes to any db.
    let live: Database | null = null;
    try {
      live = new Database(path, { readonly: true, create: false });
      live.query("SELECT 1 FROM sqlite_schema LIMIT 1").get();
      this.db = live;
      this.immutable = false;
    } catch {
      live?.close();
      this.db = new Database(`file:${path}?mode=ro&immutable=1`, {
        readonly: true,
      });
      this.immutable = true;
    }

    // WAL is set by the tracer when it creates the db; a readonly connection
    // cannot change it, so we assert rather than set, and always take the
    // busy_timeout so a concurrent writer never turns into a failed request.
    this.db.exec("PRAGMA busy_timeout = 5000");
    this.db.exec("PRAGMA synchronous = NORMAL");
    const mode = this.db
      .query<{ journal_mode: string }, []>("PRAGMA journal_mode")
      .get();
    this.journalMode = mode?.journal_mode ?? "unknown";
    if (!this.immutable && this.journalMode.toLowerCase() !== "wal") {
      console.warn(
        `[db] ${project}: journal_mode is "${this.journalMode}", expected "wal" — ` +
          `live reads during agent writes may block`,
      );
    }
  }

  /** An immutable snapshot no longer tells the truth once the file moves on. */
  stale(): boolean {
    if (!this.immutable) return false;
    return (
      existsSync(`${this.path}-wal`) ||
      statSync(this.path).mtimeMs !== this.openedMtimeMs
    );
  }

  close(): void {
    this.db.close();
  }

  /**
   * One row per Session of every run that STARTED in the window and has an
   * outcome (status != running), merged across runs.
   *
   * Key decisions (shared/types.ts doc):
   * 2. api_cost = turn costs on a cash model (`isCashModel`), 0 otherwise.
   * 4. notional_cost = agent_end cost on claude_code (first Session; null without
   *    agent_end), turn costs on any other non-cash model, 0 on cash.
   * 5. tokens = input+output+cache_read+cache_write across every turn.
   * 8. agent_end cost is the notional figure for claude_code.
   * 9. duration_ms = agent_start→agent_end wall-clock, first Session only, null without agent_end.
   */
  sessionCosts(sinceIso: string): Omit<SessionCost, "project">[] {
    return this.db
      .query<
        {
          run_id: string;
          run_status: string | null;
          role: string;
          runtime: string | null;
          model: string;
          turn_cost: number;
          end_cost: number | null;
          tokens: number;
          duration_ms: number | null;
        },
        [string]
      >(
        `WITH turns AS (
  SELECT name AS session_id,
         SUM(json_extract(payload_json, '$.cost')) AS cost,
         SUM(COALESCE(json_extract(payload_json, '$.input'), 0)
           + COALESCE(json_extract(payload_json, '$.output'), 0)
           + COALESCE(json_extract(payload_json, '$.cache_read'), 0)
           + COALESCE(json_extract(payload_json, '$.cache_write'), 0)) AS tokens
    FROM events WHERE type = 'turn' GROUP BY name
), starts AS (
  SELECT run_id, phase_id, MIN(started_at) AS started_at
    FROM events WHERE type = 'agent_start' GROUP BY run_id, phase_id
), ends AS (
  SELECT run_id, phase_id, SUM(json_extract(payload_json, '$.cost')) AS cost,
         MAX(started_at) AS ended_at
    FROM events WHERE type = 'agent_end' GROUP BY run_id, phase_id
)
SELECT s.run_id, r.status AS run_status, s.role, s.runtime, s.model,
       COALESCE(t.cost, 0) AS turn_cost,
       CASE WHEN COALESCE(s.continuation, 0) = 0 THEN e.cost ELSE 0 END AS end_cost,
       COALESCE(t.tokens, 0) AS tokens,
       CASE WHEN COALESCE(s.continuation, 0) = 0 AND st.started_at IS NOT NULL AND e.ended_at IS NOT NULL
            THEN CAST(ROUND((julianday(e.ended_at) - julianday(st.started_at)) * 86400000) AS INTEGER)
            ELSE NULL END AS duration_ms
  FROM sessions s
  JOIN runs r ON r.run_id = s.run_id
  LEFT JOIN turns t ON t.session_id = s.session_id
  LEFT JOIN ends e ON e.run_id = s.run_id AND e.phase_id = s.phase_id
  LEFT JOIN starts st ON st.run_id = s.run_id AND st.phase_id = s.phase_id
 WHERE r.started_at >= ? AND r.status != 'running'
 ORDER BY r.started_at, s.created_at`,
      )
      .all(sinceIso)
      .map((row) => {
        const cash = isCashModel(row.model);
        return {
          run_id: row.run_id,
          run_status: row.run_status as SessionCost["run_status"],
          role: row.role,
          runtime: row.runtime,
          model: row.model,
          api_cost: cash ? row.turn_cost : 0,
          notional_cost: cash
            ? 0
            : row.runtime === "claude_code"
              ? row.end_cost
              : row.turn_cost,
          tokens: row.tokens,
          duration_ms: row.duration_ms,
        };
      });
  }

  private tag<T extends Omit<Run, "project">>(row: T): T & { project: string } {
    return Object.assign(row, { project: this.project });
  }

  /**
   * Runs, most recent first, only the fields the Runs list and the sidebar
   * render: the list never draws phases or sessions, so they are not shipped
   * (ticket 215). `cash` and `sub` fold the Run's turns under the one cash
   * rule; `last_event_at` is the newest event, for the liveness tick.
   */
  runs(limit = 200): RunSummary[] {
    const rows = this.db
      .query<
        {
          run_id: string;
          workflow: string | null;
          request: string | null;
          status: RunStatus | null;
          ticket: string | null;
          started_at: string | null;
          ended_at: string | null;
          total_cost: number | null;
        },
        [number]
      >(
        `SELECT run_id, workflow, request, status, ticket, started_at, ended_at,
                total_cost
           FROM runs
          WHERE COALESCE(archived, 0) = 0
          ORDER BY started_at DESC, rowid DESC
          LIMIT ?`,
      )
      .all(clamp(limit, 1, MAX_LIMIT));

    if (rows.length === 0) return [];

    const ids = rows.map((row) => row.run_id);
    const placeholders = ids.map(() => "?").join(", ");

    // budget_warn count per run — the L1 row's ▲ badge.
    const warnRows = this.db
      .query<{ run_id: string; n: number }, string[]>(
        `SELECT run_id, COUNT(*) AS n FROM events
          WHERE type = 'budget_warn' AND run_id IN (${placeholders})
          GROUP BY run_id`,
      )
      .all(...ids);
    const warnsByRun = new Map(warnRows.map((w) => [w.run_id, w.n]));

    // Newest event per run — the idle clock reads it.
    const eventRows = this.db
      .query<{ run_id: string; last_event_at: string | null }, string[]>(
        `SELECT run_id, MAX(started_at) AS last_event_at FROM events
          WHERE run_id IN (${placeholders}) GROUP BY run_id`,
      )
      .all(...ids);
    const lastEventByRun = new Map(
      eventRows.map((e) => [e.run_id, e.last_event_at]),
    );

    // Turn costs grouped by the Session's model (a turn's name is a
    // session_id). LEFT JOIN keeps an orphan turn — no sessions row — with a
    // null model, which is not cash, so it flips `sub` instead of pricing.
    const turnRows = this.db
      .query<
        { run_id: string; model: string | null; cost: number | null },
        string[]
      >(
        `SELECT e.run_id, s.model, SUM(json_extract(e.payload_json, '$.cost')) AS cost
           FROM events e LEFT JOIN sessions s ON s.session_id = e.name
          WHERE e.type = 'turn' AND e.run_id IN (${placeholders})
          GROUP BY e.run_id, s.model`,
      )
      .all(...ids);

    // Every Session model of the run, so a subscription with no turn still
    // flips `sub`.
    const sessionRows = this.db
      .query<{ run_id: string; model: string }, string[]>(
        `SELECT DISTINCT run_id, model FROM sessions WHERE run_id IN (${placeholders})`,
      )
      .all(...ids);

    const cashByRun = new Map<string, number>();
    const subByRun = new Map<string, boolean>();
    for (const t of turnRows) {
      if (isCashModel(t.model)) {
        cashByRun.set(t.run_id, (cashByRun.get(t.run_id) ?? 0) + (t.cost ?? 0));
      } else {
        subByRun.set(t.run_id, true);
      }
    }
    for (const s of sessionRows) {
      if (!isCashModel(s.model)) subByRun.set(s.run_id, true);
    }

    return rows.map((run) => ({
      run_id: run.run_id,
      project: this.project,
      workflow: run.workflow,
      request: run.request,
      status: run.status,
      ticket: run.ticket,
      started_at: run.started_at,
      ended_at: run.ended_at,
      total_cost: run.total_cost,
      warns: warnsByRun.get(run.run_id) ?? 0,
      cash: Math.round((cashByRun.get(run.run_id) ?? 0) * 100) / 100,
      sub: subByRun.get(run.run_id) ?? false,
      last_event_at: lastEventByRun.get(run.run_id) ?? null,
    }));
  }

  /**
   * Turn-event spend since `sinceIso`, per (runtime, model) — the raw rows
   * the registry folds into providers. Joins `events.name = session_id`,
   * the tracer contract `lime cost` also relies on.
   */
  spendRows(
    sinceIso: string,
  ): { provider: string; cost: number; subscription: boolean }[] {
    return this.db
      .query<
        { runtime: string | null; model: string | null; cost: number | null },
        [string]
      >(
        `SELECT s.runtime, s.model,
                SUM(json_extract(e.payload_json, '$.cost')) AS cost
           FROM events e JOIN sessions s ON s.session_id = e.name
          WHERE e.type = 'turn' AND e.started_at >= ?
          GROUP BY s.runtime, s.model`,
      )
      .all(sinceIso)
      .map((r) => ({
        // No price table: provider = model prefix (or "unknown" when the model
        // is null or empty), so spend is never dropped. claude_code keeps its own
        // notional bucket (spec §11).
        provider:
          r.runtime === "claude_code"
            ? "claude_code"
            : r.model
              ? r.model.split("/")[0]
              : "unknown",
        cost: r.cost ?? 0,
        subscription: !isCashModel(r.model),
      }));
  }

  /**
   * Whether `table` has `column`, via `PRAGMA table_info`. Both names come
   * only from internal constants, so the pragma's interpolation is safe. A
   * `true` is cached; a `false` is re-checked on later calls so a writer's
   * later migration is picked up without reopening the connection.
   */
  private hasColumn(table: string, column: string): boolean {
    const key = `${table}.${column}`;
    if (this.columnCache.get(key)) return true;
    const present = this.db
      .query<{ name: string }, []>(`PRAGMA table_info(${table})`)
      .all()
      .some((row) => row.name === column);
    if (present) this.columnCache.set(key, true);
    return present;
  }

  run(runId: string): Run | null {
    const row = this.db
      .query<Omit<Run, "project">, [string]>(
        `SELECT run_id, workflow, request, status, engineer, started_at, ended_at,
                total_tokens, total_cost, archived,
                ticket, branch, worktree, port, db_name, profile, kind
           FROM runs WHERE run_id = ?`,
      )
      .get(runId);
    return row ? this.tag(row) : null;
  }

  phases(runId: string): Phase[] {
    const stage = this.hasColumn(TABLE_PHASES, "stage")
      ? "stage"
      : "NULL AS stage";
    return this.db
      .query<Phase, [string]>(
        `SELECT phase_id, run_id, seq, name, kind, owner, description, status,
                attempt, retries, error, started_at, ended_at, ${stage}
           FROM phases WHERE run_id = ? ORDER BY seq, rowid`,
      )
      .all(runId);
  }

  sessions(runId: string): Session[] {
    return this.sessionsFor([runId]).get(runId) ?? [];
  }

  /**
   * Sessions per run, for a set of ids at once: the sessions rows plus
   * anything that has started but not finished.
   *
   * The Spine writes the sessions row when the Session is minted, but a lane
   * whose role has not run yet may still be live — its model, color and
   * session_id are already on the agent_start event, so a lane is labelled
   * and colored from the moment the agent spawns.
   */
  private sessionsFor(runIds: string[]): Map<string, Session[]> {
    const byRun = new Map<string, Session[]>();
    if (runIds.length === 0) return byRun;
    const placeholders = runIds.map(() => "?").join(", ");

    const append = (runId: string, session: Session) => {
      const list = byRun.get(runId);
      if (list) list.push(session);
      else byRun.set(runId, [session]);
    };

    const budget = this.hasColumn(TABLE_SESSIONS, "budget_warn")
      ? "budget_warn, budget_stop"
      : "NULL AS budget_warn, NULL AS budget_stop";
    const completed = this.db
      .query<Session, string[]>(
        `SELECT session_id, run_id, phase_id, role, continuation, runtime, model,
                color, context_tokens, context_window, ${budget}, created_at,
                last_used_at
           FROM sessions WHERE run_id IN (${placeholders})
          ORDER BY created_at, role`,
      )
      .all(...runIds);
    for (const row of completed) append(row.run_id, row);

    const started = this.db
      .query<
        {
          run_id: string;
          phase_id: string | null;
          role: string | null;
          payload_json: string | null;
          started_at: string | null;
        },
        string[]
      >(
        `SELECT e.run_id, e.phase_id, p.owner AS role, e.payload_json, e.started_at
           FROM events e JOIN phases p ON p.phase_id = e.phase_id
          WHERE e.run_id IN (${placeholders}) AND e.type = 'agent_start'
          ORDER BY e.rowid`,
      )
      .all(...runIds);

    for (const row of started) {
      if (!row.role) continue;
      // A minted row is authoritative; only fill genuine gaps.
      if (byRun.get(row.run_id)?.some((s) => s.role === row.role)) continue;
      let payload: AgentStartPayload = {};
      try {
        payload = JSON.parse(row.payload_json ?? "{}") as AgentStartPayload;
      } catch {
        // A malformed payload just means no label — never a failed request.
      }
      append(row.run_id, {
        session_id: payload.session_id ?? `${row.run_id}:${row.role}`,
        run_id: row.run_id,
        phase_id: row.phase_id ?? "",
        role: row.role,
        continuation: 0,
        runtime: payload.runtime ?? null,
        model: payload.model ?? null,
        color: payload.color ?? null,
        // Occupancy is only known once the Session's turn closes.
        context_tokens: null,
        context_window: null,
        // The resolved budget is not on the agent_start payload.
        budget_warn: null,
        budget_stop: null,
        created_at: row.started_at,
        last_used_at: row.started_at,
      });
    }
    return byRun;
  }

  /** Run + phases + sessions in one shot — L2 needs all three to draw lanes. */
  runDetail(runId: string): RunDetail | null {
    const run = this.run(runId);
    if (!run) return null;

    return {
      run,
      usage: this.usage(runId),
      phases: this.phases(runId),
      sessions: this.sessions(runId),
    };
  }

  /**
   * Raw tokens read and written, beside the billed headline.
   *
   * Derived from the `agent_end` payloads rather than stored, so every run
   * already in the db gets the split without a migration or a re-run.
   *
   * `total_tokens` is a SPEND number: every turn re-sends the whole
   * conversation, so an 86k conversation over 49 turns bills millions. These
   * two say what actually moved — material read for the first time, and
   * material generated. The gap between them and the headline is cached
   * re-reads, which is usually most of it.
   */
  usage(runId: string): RunUsage {
    const rows = this.db
      .query<{ payload_json: string | null }, [string]>(
        "SELECT payload_json FROM events WHERE run_id = ? AND type = 'agent_end'",
      )
      .all(runId);

    let read = 0;
    let written = 0;
    for (const row of rows) {
      if (!row.payload_json) continue;
      try {
        const u = (
          JSON.parse(row.payload_json) as { usage?: Record<string, number> }
        ).usage;
        if (!u) continue;
        // RAW reads only: material entering the context for the first time,
        // billed either as uncached input or as a cache write. Cache reads are
        // the same tokens served again on later turns — counting them here
        // would rebuild the very inflation this split exists to expose.
        read += (u.input ?? 0) + (u.cache_write ?? 0);
        written += u.output ?? 0;
      } catch {
        /* a malformed payload simply contributes nothing */
      }
    }
    return { read, written };
  }

  /**
   * The polling query. Rowid cursor, insertion order, bounded page — the same
   * mechanism serves the live tail and lazy-paged history.
   */
  events(runId: string, after = 0, limit = DEFAULT_LIMIT): EventsPage {
    const cappedLimit = clamp(limit, 1, MAX_LIMIT);
    const events = this.db
      .query<Event, [string, number, number]>(
        `SELECT rowid AS rowid, event_id, run_id, phase_id, parent_id, type, name,
                payload_json, tokens, started_at, ended_at
           FROM events
          WHERE run_id = ? AND rowid > ?
          ORDER BY rowid
          LIMIT ?`,
      )
      .all(runId, Math.max(0, after), cappedLimit);

    return {
      events,
      cursor:
        events.length > 0
          ? events[events.length - 1]!.rowid
          : Math.max(0, after),
      has_more: events.length === cappedLimit,
    };
  }

  /**
   * The live feed's cursor: events ordered by `seq`, paged. Forward-only, so
   * `next` feeds straight back in. A ledger without `events.seq` numbers each
   * Run's events `ROW_NUMBER() OVER (ORDER BY event_id)` — the same numbers
   * the migration later stamps. The rowid `events()` stays for RunTrace
   * until ticket 259.
   */
  eventsAfterSeq(runId: string, after = 0, limit = MAX_LIMIT): FeedEventsPage {
    const cappedLimit = clamp(limit, 1, MAX_LIMIT);
    const floorAfter = Math.max(0, Math.trunc(after));

    const rows = this.hasColumn(TABLE_EVENTS, "seq")
      ? this.db
          .query<EventSeqRow, [string, number, number]>(
            `SELECT e.seq, e.type, e.name, e.phase_id,
                    (SELECT p.seq FROM events p WHERE p.event_id = e.parent_id) AS parent_seq,
                    e.payload_json, e.tokens, e.started_at
               FROM events e
              WHERE e.run_id = ? AND e.seq > ?
              ORDER BY e.seq
              LIMIT ?`,
          )
          .all(runId, floorAfter, cappedLimit)
      : this.db
          .query<EventSeqRow, [string, number, number]>(
            `WITH n AS (
               SELECT event_id, phase_id, parent_id, type, name, payload_json,
                      tokens, started_at,
                      ROW_NUMBER() OVER (ORDER BY event_id) AS seq
                 FROM events WHERE run_id = ?
             )
             SELECT e.seq, e.type, e.name, e.phase_id,
                    (SELECT p.seq FROM n p WHERE p.event_id = e.parent_id) AS parent_seq,
                    e.payload_json, e.tokens, e.started_at
               FROM n e
              WHERE e.seq > ?
              ORDER BY e.seq
              LIMIT ?`,
          )
          .all(runId, floorAfter, cappedLimit);

    const events: FeedEvent[] = rows.map((row) => ({
      seq: row.seq,
      type: row.type,
      name: row.name,
      phase_id: row.phase_id,
      parent_seq: row.parent_seq,
      payload: parsePayload(row.payload_json),
      tokens: row.tokens,
      started_at: row.started_at,
    }));

    return {
      events,
      next: events.length > 0 ? events[events.length - 1]!.seq : floorAfter,
      has_more: events.length === cappedLimit,
    };
  }

  /**
   * Candidate live Runs: a Run whose kind is `run`/`research`/`prototype` and
   * which has an OPEN `run` or `deploy` process row. When `runId` is given,
   * only that Run is considered. Every `run`/`deploy` row of the candidates is
   * returned (open AND closed, `ORDER BY id`) so `judge` can decide liveness.
   * `heartbeat_at` is read through `hasColumn`, so a pre-251 ledger reads NULL;
   * `batch_id` likewise, so a pre-254 ledger reads NULL.
   */
  liveRows(runId?: string): LiveCandidate[] {
    const batch = this.hasColumn(TABLE_RUNS, "batch_id")
      ? "batch_id"
      : "NULL AS batch_id";
    const candidates = this.db
      .query<
        {
          run_id: string;
          kind: LiveKind;
          workflow: string | null;
          ticket: string | null;
          batch_id: string | null;
          started_at: string | null;
        },
        string[]
      >(
        `SELECT run_id, kind, workflow, ticket, ${batch}, started_at
           FROM runs
          WHERE kind IN ('run', 'research', 'prototype')
            AND EXISTS (SELECT 1 FROM processes p
                         WHERE p.run_id = runs.run_id
                           AND p.ended_at IS NULL
                           AND p.kind IN ('run', 'deploy'))
            ${runId === undefined ? "" : "AND runs.run_id = ?"}
          ORDER BY started_at, run_id`,
      )
      .all(...(runId === undefined ? [] : [runId]));

    if (candidates.length === 0) return [];

    const ids = candidates.map((c) => c.run_id);
    const placeholders = ids.map(() => "?").join(", ");
    const beat = this.hasColumn(TABLE_PROCESSES, "heartbeat_at")
      ? "heartbeat_at"
      : "NULL AS heartbeat_at";
    const rows = this.db
      .query<ProcessRow, string[]>(
        `SELECT id, run_id, kind, pid, started_at, ended_at, ${beat}
           FROM processes
          WHERE run_id IN (${placeholders}) AND kind IN ('run', 'deploy')
          ORDER BY id`,
      )
      .all(...ids);

    const byRun = new Map<string, ProcessRow[]>();
    for (const row of rows) {
      const list = byRun.get(row.run_id);
      if (list) list.push(row);
      else byRun.set(row.run_id, [row]);
    }
    return candidates.map((c) => ({
      ...c,
      processes: byRun.get(c.run_id) ?? [],
    }));
  }

  /**
   * Every OPEN `batch` process row (one SELECT), each with the dead batch it
   * retries (via `processes.retry_of`) and that dead batch's last beat
   * (`died_at`). `heartbeat_at` and `retry_of` are read through `hasColumn`,
   * so a pre-251/255 ledger reads NULL.
   */
  batchRows(): BatchRow[] {
    const beat = this.hasColumn(TABLE_PROCESSES, "heartbeat_at")
      ? "p.heartbeat_at"
      : "NULL AS heartbeat_at";
    const retry = this.hasColumn(TABLE_PROCESSES, "retry_of")
      ? `p.retry_of, (SELECT d.heartbeat_at FROM processes d
                       WHERE d.run_id = p.retry_of AND d.kind = 'batch'
                       ORDER BY d.id DESC LIMIT 1) AS died_at`
      : "NULL AS retry_of, NULL AS died_at";
    const rows = this.db
      .query<
        {
          id: number;
          run_id: string;
          kind: string | null;
          pid: number | null;
          started_at: string | null;
          ended_at: string | null;
          heartbeat_at: string | null;
          retry_of: string | null;
          died_at: string | null;
        },
        []
      >(
        `SELECT p.id, p.run_id, p.kind, p.pid, p.started_at, p.ended_at, ${beat}, ${retry}
           FROM processes p
          WHERE p.kind = 'batch' AND p.ended_at IS NULL
          ORDER BY p.id`,
      )
      .all();
    return rows.map((row) => ({
      batch_id: row.run_id,
      retry_of: row.retry_of,
      died_at: row.died_at,
      process: {
        id: row.id,
        run_id: row.run_id,
        kind: row.kind,
        pid: row.pid,
        started_at: row.started_at,
        ended_at: row.ended_at,
        heartbeat_at: row.heartbeat_at,
      },
    }));
  }

  /**
   * The `batch_children` rows of `batchIds`, in `(batch_id, position)` order.
   * Empty when no ids are given or the ledger has no `batch_children` table
   * (pre-254); `hasColumn` returns no rows for a missing table.
   */
  batchChildren(batchIds: string[]): BatchChildRow[] {
    if (batchIds.length === 0) return [];
    if (!this.hasColumn(TABLE_BATCH_CHILDREN, "state")) return [];
    const placeholders = batchIds.map(() => "?").join(", ");
    return this.db
      .query<BatchChildRow, string[]>(
        `SELECT batch_id, position, ticket, state, reason
           FROM batch_children
          WHERE batch_id IN (${placeholders})
          ORDER BY batch_id, position`,
      )
      .all(...batchIds);
  }

  /**
   * The per-Run facts the `running` frame carries beside its process row: the
   * latest `status='running'` phase, the Session whose role is that phase's
   * owner, the Session's context use, and the newest event's `started_at`.
   * Without a running phase the phase, session and context are all null.
   */
  runningFacts(
    runId: string,
  ): Pick<RunningRun, "phase" | "session" | "context" | "last_event_at"> {
    const stage = this.hasColumn(TABLE_PHASES, "stage")
      ? "stage"
      : "NULL AS stage";
    const phase = this.db
      .query<
        {
          seq: number | null;
          name: string | null;
          stage: string | null;
          status: PhaseStatus | null;
          owner: string | null;
        },
        [string]
      >(
        `SELECT seq, name, status, owner, ${stage}
           FROM phases
          WHERE run_id = ? AND status = 'running'
          ORDER BY seq DESC, rowid DESC
          LIMIT 1`,
      )
      .get(runId);

    const lastEvent = this.db
      .query<{ last_event_at: string | null }, [string]>(
        "SELECT MAX(started_at) AS last_event_at FROM events WHERE run_id = ?",
      )
      .get(runId);
    const last_event_at = lastEvent?.last_event_at ?? null;

    if (!phase) {
      return { phase: null, session: null, context: null, last_event_at };
    }

    const session =
      this.sessions(runId).findLast((s) => s.role === phase.owner) ?? null;

    let context: RunningRun["context"] = null;
    if (session) {
      // The latest turn's `tokens` is the live context figure; a Session with
      // no turn yet falls back to its stored `context_tokens`.
      const turn = this.db
        .query<{ tokens: number | null }, [string, string]>(
          `SELECT tokens FROM events
            WHERE run_id = ? AND type = 'turn' AND name = ?
            ORDER BY event_id DESC LIMIT 1`,
        )
        .get(runId, session.session_id);
      context = {
        tokens: turn?.tokens ?? session.context_tokens ?? null,
        warn: session.budget_warn ?? null,
        stop: session.budget_stop ?? null,
      };
    }

    return {
      phase: {
        seq: phase.seq,
        name: phase.name,
        stage: phase.stage,
        status: phase.status,
      },
      session: session ? { role: session.role, model: session.model } : null,
      context,
      last_event_at,
    };
  }

  envelopes(runId: string): Envelope[] {
    return this.db
      .query<Envelope, [string]>(
        `SELECT envelope_id, run_id, phase_id, agent, output_type, payload_json,
                valid, attempt, created_at
           FROM envelopes WHERE run_id = ? ORDER BY created_at, rowid`,
      )
      .all(runId);
  }

  gates(runId: string): GateResult[] {
    return this.db
      .query<GateResult, [string]>(
        `SELECT id, run_id, phase_id, attempt, gate, passed, violations_json,
                checks_json, created_at
           FROM gate_results WHERE run_id = ? ORDER BY id`,
      )
      .all(runId);
  }

  /** Distinct (runtime, model) pairs this project has ever run — the raw rows
   *  of the model catalog's ledger fallback. Pure read. */
  ledgerModelRows(): { runtime: string; model: string }[] {
    return this.db
      .query<{ runtime: string; model: string }, []>(
        "SELECT DISTINCT runtime, model FROM sessions",
      )
      .all();
  }

  runCount(): number {
    const row = this.db
      .query<{ n: number }, []>("SELECT COUNT(*) AS n FROM runs")
      .get();
    return row?.n ?? 0;
  }

  /**
   * The Session dir a role's compiled prompts live in:
   * `.lime/runs/<run_id>/<seq>-<role>[.<continuation>]/prompts/`.
   *
   * The API addresses a role, not a seq, so pick the LAST matching dir
   * (highest seq, then continuation) — the prompts of the role's most recent
   * Session, which is the one an engineer debugging a run wants.
   */
  promptsDir(runId: string, role: string): string | null {
    const runDir = join(this.runsDir, runId);
    if (!existsSync(runDir)) return null;
    const pattern = new RegExp(`^\\d+-${escapeRegExp(role)}(\\.\\d+)?$`);
    const dirs = readdirSync(runDir)
      .filter((name) => pattern.test(name))
      .toSorted();
    const last = dirs.at(-1);
    return last ? join(runDir, last, "prompts") : null;
  }
}

function escapeRegExp(s: string): string {
  return s.replaceAll(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Every registered project's reader, opened lazily and re-probed on demand. */
export class LimeRegistry {
  readonly registryPath: string;
  private readonly entries: RegistryEntry[];
  private readonly dbs = new Map<string, LimeDb>();

  constructor(registryPath: string) {
    this.registryPath = registryPath;
    this.entries = readRegistry(registryPath);
    if (this.entries.length === 0) {
      throw new Error(
        `project registry ${registryPath} is empty — \`lime init\` a project first`,
      );
    }
    this.probe();
  }

  /** Open any db that has appeared since the last look, and swap any immutable
   *  snapshot whose file has moved on (a Run started writing). Cheap: a couple
   *  of existsSync/stat calls per project; a live db is never re-opened. */
  private probe(): void {
    for (const entry of this.entries) {
      const open = this.dbs.get(entry.name);
      if (open && !open.stale()) continue;
      if (!existsSync(entry.dbPath)) continue;
      try {
        const fresh = new LimeDb(entry.name, entry.dbPath);
        open?.close();
        this.dbs.set(entry.name, fresh);
        console.log(
          `[db] ${entry.name}: reading ${entry.dbPath}${fresh.immutable ? " (idle snapshot)" : ""}`,
        );
      } catch (error) {
        console.warn(`[db] ${entry.name}: ${(error as Error).message}`);
      }
    }
  }

  projects(): ProjectInfo[] {
    return this.entries.map((e) => ({
      name: e.name,
      path: e.path,
      db_found: this.dbs.has(e.name),
    }));
  }

  get(project: string): LimeDb | null {
    this.probe();
    return this.dbs.get(project) ?? null;
  }

  /** The registry's filesystem path for a project name, or null when unknown. */
  projectPath(project: string): string | null {
    return this.entries.find((e) => e.name === project)?.path ?? null;
  }

  /**
   * Resolve requested project names to registry paths, preserving order and
   * deduplicating — unknown names get null path.  Uses the registry entries
   * directly, never opens a db.
   */
  resolveProjects(names: string[]): { name: string; path: string | null }[] {
    const seen = new Set<string>();
    const results: { name: string; path: string | null }[] = [];
    for (const name of names) {
      if (seen.has(name)) continue;
      seen.add(name);
      const entry = this.entries.find((e) => e.name === name);
      results.push({ name, path: entry?.path ?? null });
    }
    return results;
  }

  /** Distinct (runtime, model) pairs across every registered project — the
   *  fallback source of the model catalog. Pure reads; projects whose lime.db
   *  has not appeared yet are skipped (they have no sessions to read). */
  ledgerModels(): { runtime: string; model: string }[] {
    this.probe();
    const seen = new Set<string>();
    const merged: { runtime: string; model: string }[] = [];
    for (const db of this.dbs.values()) {
      for (const row of db.ledgerModelRows()) {
        const key = `${row.runtime}\u0000${row.model}`;
        if (seen.has(key)) continue;
        seen.add(key);
        merged.push(row);
      }
    }
    return merged;
  }

  /** Every project's runs, merged newest-first. The one cross-project query. */
  runs(limit = 200): RunSummary[] {
    this.probe();
    const merged: RunSummary[] = [];
    for (const db of this.dbs.values()) merged.push(...db.runs(limit));
    merged.sort((a, b) =>
      (b.started_at ?? "").localeCompare(a.started_at ?? ""),
    );
    return merged.slice(0, clamp(limit, 1, MAX_LIMIT));
  }

  runCount(): number {
    let n = 0;
    for (const db of this.dbs.values()) n += db.runCount();
    return n;
  }

  /** Spend per provider since `sinceIso`, merged across projects — priced
   *  providers by cost descending, the notional subscription bucket last. */
  spend(sinceIso: string): ProviderSpend[] {
    this.probe();
    const buckets = new Map<
      string,
      { provider: string; subscription: boolean; cost: number }
    >();
    for (const db of this.dbs.values()) {
      for (const row of db.spendRows(sinceIso)) {
        // A bucket never mixes cash and subscription, even for one provider.
        const key = `${row.provider}\u0000${row.subscription}`;
        const bucket = buckets.get(key);
        if (bucket) bucket.cost += row.cost;
        else
          buckets.set(key, {
            provider: row.provider,
            subscription: row.subscription,
            cost: row.cost,
          });
      }
    }
    return [...buckets.values()]
      .map((b) => ({
        provider: b.provider,
        // Two projects' float sums drift (0.05 + 0.05 + …); cents are plenty
        // for a sidebar figure and keep the JSON stable.
        cost: Math.round(b.cost * 100) / 100,
        subscription: b.subscription,
      }))
      .toSorted(
        (a, b) =>
          Number(a.subscription) - Number(b.subscription) || b.cost - a.cost,
      );
  }

  /**
   * One row per Session of every finished run started in the window, tagged
   * with its project. No rounding, no sort — the client groups.
   */
  sessionCosts(sinceIso: string): SessionCost[] {
    this.probe();
    const merged: SessionCost[] = [];
    for (const db of this.dbs.values()) {
      for (const row of db.sessionCosts(sinceIso)) {
        merged.push({ ...row, project: db.project });
      }
    }
    return merged;
  }

  close(): void {
    for (const db of this.dbs.values()) db.close();
    this.dbs.clear();
  }
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.trunc(value)));
}
