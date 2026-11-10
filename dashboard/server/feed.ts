/**
 * The live feed's server side: liveness, run headers and event pages read
 * through `FeedSource`, so ticket f1's store can replace lime.db below it.
 *
 * `judge` is a line-for-line port of `lime/tracer.py` `liveness()` — the one
 * rule `lime status` and this feed share. `LimeFeedSource` reads it over every
 * registered project's lime.db through an injected clock and pid probe.
 */
import { existsSync, watch, type FSWatcher } from "node:fs";
import { join } from "node:path";
import type { LimeRegistry } from "./db.ts";
import type { LiveCandidate, ProcessRow } from "./db.ts";
import type {
  FeedEventsPage,
  FeedRun,
  HomeFocusResponse,
  LiveBatch,
  LiveKind,
  LiveRunning,
  QueuedChild,
  RunningRun,
} from "../shared/types.ts";

/** Three missed 10 s beats plus slack — `tracer.LIVENESS_S`. */
export const LIVENESS_MS = 45_000;

/** Trailing debounce for a burst of writes to a project's `lime.db-wal`. */
export const LIVE_DEBOUNCE_MS = 200;

/** The catch-up tick; also covers a project whose directory was missing. */
export const LIVE_TICK_MS = 5_000;

/** The SQLite write-ahead log whose appearance signals a project change. */
export const WAL_FILE = "lime.db-wal";

/**
 * Parse an ISO stamp; a naive one reads as UTC, None/garbage as null. The first
 * space becomes a `T`, and a `Z` is appended when there is no zone suffix.
 */
export function parseStamp(text: string | null): number | null {
  if (text === null) return null;
  const trimmed = text.trim();
  if (!trimmed) return null;
  let iso = trimmed.replace(" ", "T");
  if (!/(Z|[+-]\d{2}:\d{2})$/.test(iso)) iso += "Z";
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms;
}

/**
 * Best-effort liveness probe — a crashed Run's port must count as free. A
 * non-integral or non-positive pid is false (pid 0 would signal the group);
 * a successful signal is true; `EPERM` means the process exists.
 */
export function pidAlive(pid: number | null): boolean {
  if (pid === null || !Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

/**
 * A Run's ticket path to its `batch_children.ticket` stem: the last path
 * segment (split on `/` or `\`) with a trailing `.md` dropped; null stays null.
 */
export function ticketStem(ticket: string | null): string | null {
  if (ticket === null) return null;
  const segment = ticket.split(/[/\\]/).pop() ?? "";
  return segment.endsWith(".md") ? segment.slice(0, -3) : segment;
}

export type Verdict = "live" | "starting" | "dead";

/** The fields `judge` looks at on a process row. */
export type ProcessProbe = Pick<
  ProcessRow,
  "kind" | "pid" | "ended_at" | "heartbeat_at" | "started_at"
>;

/**
 * The one liveness rule, a port of `tracer.liveness()`. It looks at `kind`'s
 * rows only: a row with `ended_at` is skipped; a parsed beat younger than
 * `LIVENESS_MS` is live; a NULL/unparseable beat whose pid is alive is live;
 * rows of that kind exist with none live → dead; no rows and a
 * `runStartedAt` younger than `LIVENESS_MS` → starting; otherwise dead. `row`
 * is the deciding live row, null otherwise.
 */
export function judge<T extends ProcessProbe>(
  rows: T[],
  runStartedAt: string | null,
  now: number,
  kind: string,
  alive: (pid: number | null) => boolean,
): { verdict: Verdict; row: T | null } {
  const ofKind = rows.filter((row) => row.kind === kind);
  for (const row of ofKind) {
    if (row.ended_at) continue;
    const beat = parseStamp(row.heartbeat_at);
    if (beat !== null) {
      if (now - beat < LIVENESS_MS) return { verdict: "live", row };
    } else if (alive(row.pid)) {
      return { verdict: "live", row };
    }
  }
  if (ofKind.length > 0) return { verdict: "dead", row: null };
  const started = parseStamp(runStartedAt);
  if (started !== null && now - started < LIVENESS_MS) {
    return { verdict: "starting", row: null };
  }
  return { verdict: "dead", row: null };
}

/** A live-feed change signal; ticket 258's stream reacts to both kinds. */
export type FeedChange = { type: "tick" } | { type: "project"; project: string };

/**
 * The stream reads only through this seam, so ticket f1's store can replace
 * lime.db below it without touching the SSE body. Focus delivery is part of
 * the same seam: the SSE body never holds a second reader.
 */
export interface FeedSource {
  /**
   * The live `runs` list plus the queued `batch_children` rows and the live
   * `batches` headers; always all three lists, in that key order.
   */
  running(): Promise<Required<LiveRunning>>;
  runHeader(project: string, runId: string): Promise<FeedRun | null>;
  eventsAfter(
    project: string,
    runId: string,
    seq: number,
    limit: number,
  ): Promise<FeedEventsPage>;
  onChange(cb: (change: FeedChange) => void): () => void;
  /** The newest Focus read without starting one; null before the first. */
  focusLatest(): HomeFocusResponse | null;
  /** The coordinated Focus read (joins an in-flight read, reuses a fresh one). */
  focusRead(): Promise<HomeFocusResponse>;
  /** Register a Focus publish listener; returns its unsubscribe. */
  onFocus(cb: (res: HomeFocusResponse) => void): () => void;
}

export type FeedRegistry = Pick<LimeRegistry, "projects" | "get">;

/**
 * What `LimeFeedSource` needs of the Focus watcher to fold Focus into the one
 * `FeedSource` seam: the connect snapshot reads `latest()`, an uncached read
 * goes through `refresh()`, and each publish arrives through `subscribe`.
 * `FocusWatcher` satisfies it structurally.
 */
export interface FocusProvider {
  subscribe(fn: (res: HomeFocusResponse) => void): () => void;
  latest(): HomeFocusResponse | null;
  refresh(): Promise<HomeFocusResponse>;
}

/** An empty Focus snapshot, sent when no Focus provider was injected. */
const EMPTY_FOCUS: HomeFocusResponse = { report: null, goals: null, errors: [] };

export interface LimeFeedSourceOpts {
  pidAlive?: (pid: number | null) => boolean;
  now?: () => number;
  log?: (line: string) => void;
  debounceMs?: number;
  tickMs?: number;
  /** The Focus watcher whose reads ride this same FeedSource boundary. */
  focus?: FocusProvider;
}

/**
 * `FeedSource` over every registered project's lime.db. Reads never write; a
 * failed read propagates rather than returning a partial list, so the stream
 * logs it and retries on the next tick.
 */
export class LimeFeedSource implements FeedSource {
  private readonly registry: FeedRegistry;
  private readonly pidAlive: (pid: number | null) => boolean;
  private readonly now: () => number;
  private readonly log: (line: string) => void;
  private readonly debounceMs: number;
  private readonly tickMs: number;
  private readonly focus: FocusProvider | null;
  private readonly subscribers = new Set<(change: FeedChange) => void>();
  private watchers: FSWatcher[] = [];
  private readonly timers = new Map<string, ReturnType<typeof setTimeout>>();
  private interval: ReturnType<typeof setInterval> | null = null;

  constructor(registry: FeedRegistry, opts: LimeFeedSourceOpts = {}) {
    this.registry = registry;
    this.pidAlive = opts.pidAlive ?? pidAlive;
    this.now = opts.now ?? Date.now;
    this.log = opts.log ?? ((line) => console.error(line));
    this.debounceMs = opts.debounceMs ?? LIVE_DEBOUNCE_MS;
    this.tickMs = opts.tickMs ?? LIVE_TICK_MS;
    this.focus = opts.focus ?? null;
  }

  /** The newest Focus read without starting one; null before the first. */
  focusLatest(): HomeFocusResponse | null {
    return this.focus?.latest() ?? null;
  }

  /** The coordinated Focus read; an empty snapshot without a provider. */
  focusRead(): Promise<HomeFocusResponse> {
    return this.focus?.refresh() ?? Promise.resolve(EMPTY_FOCUS);
  }

  /** Register a Focus publish listener; a no-op without a provider. */
  onFocus(cb: (res: HomeFocusResponse) => void): () => void {
    return this.focus?.subscribe(cb) ?? (() => {});
  }

  /** Add a change subscriber and return its unsubscribe. */
  onChange(cb: (change: FeedChange) => void): () => void {
    this.subscribers.add(cb);
    return () => {
      this.subscribers.delete(cb);
    };
  }

  /** Notify every subscriber; a throw is logged and never stops the others. */
  private emit(change: FeedChange): void {
    for (const cb of this.subscribers) {
      try {
        cb(change);
      } catch (error) {
        this.log(`[live] subscriber threw: ${String(error)}`);
      }
    }
  }

  /**
   * Watch each registered project's `.lime/runs` directory and tick. A
   * missing directory is skipped silently (the tick covers it); a watcher
   * error or a `watch` throw is logged, never raised. Idempotent.
   */
  start(): void {
    if (this.interval !== null) return;
    for (const { name, path } of this.registry.projects()) {
      const runsDir = join(path, ".lime", "runs");
      if (!existsSync(runsDir)) continue;
      let watcher: FSWatcher;
      try {
        watcher = watch(runsDir, (_event, filename) => {
          if (filename !== null && filename.toString() !== WAL_FILE) return;
          this.schedule(name);
        });
      } catch (error) {
        this.log(`[live] watch failed: ${String(error)}`);
        continue;
      }
      watcher.on("error", (error) => {
        this.log(`[live] watcher error: ${String(error)}`);
      });
      this.watchers.push(watcher);
    }
    this.interval = setInterval(() => this.emit({ type: "tick" }), this.tickMs);
  }

  /** One trailing timer per project: reset it, then emit a project change. */
  private schedule(project: string): void {
    const existing = this.timers.get(project);
    if (existing !== undefined) clearTimeout(existing);
    this.timers.set(
      project,
      setTimeout(() => {
        this.timers.delete(project);
        this.emit({ type: "project", project });
      }, this.debounceMs),
    );
  }

  /** Clear the tick, every debounce timer and every watcher. Idempotent. */
  stop(): void {
    if (this.interval !== null) {
      clearInterval(this.interval);
      this.interval = null;
    }
    for (const timer of this.timers.values()) clearTimeout(timer);
    this.timers.clear();
    for (const watcher of this.watchers) watcher.close();
    this.watchers = [];
  }

  /**
   * The deciding live row of a candidate: its `run` row (kind `runs.kind`),
   * else a live `deploy` row (kind `deploy`), else none.
   */
  private liveOf(
    c: LiveCandidate,
    now: number,
  ): { kind: LiveKind; row: ProcessRow } | null {
    const run = judge(c.processes, c.started_at, now, "run", this.pidAlive);
    if (run.verdict === "live" && run.row) return { kind: c.kind, row: run.row };
    const deploy = judge(
      c.processes,
      c.started_at,
      now,
      "deploy",
      this.pidAlive,
    );
    if (deploy.verdict === "live" && deploy.row) {
      return { kind: "deploy", row: deploy.row };
    }
    return null;
  }

  /**
   * Every live Run across the registry plus the queued `batch_children` rows
   * and the live `batch` headers. Runs sort by project then run_id; queued by
   * project, batch_id, position; batches by project, batch_id. The three keys
   * are always present and in this order, so the frame's JSON is stable.
   */
  async running(): Promise<Required<LiveRunning>> {
    const now = this.now();
    const runs: RunningRun[] = [];
    const queued: QueuedChild[] = [];
    const batches: LiveBatch[] = [];
    for (const { name } of this.registry.projects()) {
      const db = this.registry.get(name);
      if (!db) continue;

      const live = db
        .batchRows()
        .filter(
          (b) =>
            judge([b.process], null, now, "batch", this.pidAlive).verdict ===
            "live",
        );
      const own = new Set(live.map((b) => b.batch_id));
      const retryIds = live
        .map((b) => b.retry_of)
        .filter((id): id is string => id !== null);
      const children = db.batchChildren([...own, ...retryIds]);

      const positions = new Map<string, number>();
      for (const child of children) {
        if (child.state === "queued" && own.has(child.batch_id)) {
          queued.push({
            project: name,
            batch_id: child.batch_id,
            position: child.position,
            ticket: child.ticket,
            reason: child.reason,
          });
        } else if (child.state === "started") {
          const key = `${child.batch_id}\n${child.ticket}`;
          if (!positions.has(key)) positions.set(key, child.position);
        }
      }

      for (const batch of live) {
        batches.push({
          project: name,
          batch_id: batch.batch_id,
          retry_of: batch.retry_of,
          died_at: batch.retry_of === null ? null : batch.died_at,
        });
      }

      for (const candidate of db.liveRows()) {
        const run = this.liveOf(candidate, now);
        if (!run) continue;
        const stem = ticketStem(candidate.ticket);
        runs.push({
          project: name,
          run_id: candidate.run_id,
          kind: run.kind,
          workflow: candidate.workflow,
          ticket: candidate.ticket,
          batch_id: candidate.batch_id,
          batch_position:
            candidate.batch_id === null
              ? null
              : (positions.get(`${candidate.batch_id}\n${stem}`) ?? null),
          attempt_started_at: run.row.started_at,
          heartbeat_at: run.row.heartbeat_at,
          ...db.runningFacts(candidate.run_id),
        });
      }
    }
    runs.sort(
      (a, b) =>
        a.project.localeCompare(b.project) || a.run_id.localeCompare(b.run_id),
    );
    queued.sort(
      (a, b) =>
        a.project.localeCompare(b.project) ||
        a.batch_id.localeCompare(b.batch_id) ||
        a.position - b.position,
    );
    batches.sort(
      (a, b) =>
        a.project.localeCompare(b.project) ||
        a.batch_id.localeCompare(b.batch_id),
    );
    return { runs, queued, batches };
  }

  /** A Run's detail plus its envelopes, gates and liveness. */
  async runHeader(project: string, runId: string): Promise<FeedRun | null> {
    const db = this.registry.get(project);
    if (!db) return null;
    const detail = db.runDetail(runId);
    if (!detail) return null;
    const candidate = db.liveRows(runId)[0];
    const live =
      candidate !== undefined && this.liveOf(candidate, this.now()) !== null;
    return {
      ...detail,
      envelopes: db.envelopes(runId),
      gates: db.gates(runId),
      live,
    };
  }

  /** One Run's events after `seq`, paged. Unknown project → an empty page. */
  async eventsAfter(
    project: string,
    runId: string,
    seq: number,
    limit: number,
  ): Promise<FeedEventsPage> {
    const db = this.registry.get(project);
    if (!db) return { events: [], next: seq, has_more: false };
    return db.eventsAfterSeq(runId, seq, limit);
  }
}

/** Keep-alive comment interval, so a quiet stream is not idle-closed. */
export const PING_MS = 15_000;

/** The most events one `events` frame carries. */
export const EVENTS_PAGE = 1000;

/** The client's reconnect hint, in milliseconds. */
export const RETRY_MS = 3000;

/** One Run's resume point on a live connection. */
export interface Cursor {
  project: string;
  run_id: string;
  seq: number;
}

/** A project/run_id path segment: no colon, no traversal. */
const CURSOR_SEGMENT = /^[A-Za-z0-9._-]+$/;

/** A seq is a bounded run of digits. */
const CURSOR_SEQ = /^\d{1,15}$/;

function goodSegment(part: string): boolean {
  return CURSOR_SEGMENT.test(part) && part !== "." && part !== "..";
}

/**
 * Parse the `run=<project>:<run_id>:<seq>` query values. Each value must split
 * into exactly three parts, with a safe project and run_id and a numeric seq;
 * any bad value makes the whole set null (the route answers 400). A duplicate
 * `project:run_id` keeps the last one.
 */
export function parseCursors(values: string[]): Cursor[] | null {
  const byKey = new Map<string, Cursor>();
  for (const value of values) {
    const parts = value.split(":");
    if (parts.length !== 3) return null;
    const [project, run_id, seqText] = parts;
    if (!goodSegment(project) || !goodSegment(run_id)) return null;
    if (!CURSOR_SEQ.test(seqText)) return null;
    byKey.set(`${project}:${run_id}`, {
      project,
      run_id,
      seq: Number(seqText),
    });
  }
  return [...byKey.values()];
}

export interface LiveStreamOpts {
  source: FeedSource;
  cursors: Cursor[];
  signal: AbortSignal;
  pingMs?: number;
  log?: (line: string) => void;
}

/**
 * One server-sent-events body carrying `focus` plus `running`, `run` and
 * `events`, every frame read through the one `FeedSource`. Every frame after
 * the open comment goes through one serialized job chain, so their order is
 * deterministic and none is written after cleanup. The connect snapshot is
 * Focus (awaited when uncached) then running, before any subscribed Run data.
 * A cursor advances only after its `events` frame is sent, so a reconnect
 * never repeats a line.
 */
export function liveStream(opts: LiveStreamOpts): Response {
  const { source, signal } = opts;
  const pingMs = opts.pingMs ?? PING_MS;
  const log = opts.log ?? ((line: string) => console.error(line));

  const encoder = new TextEncoder();
  const cursors = opts.cursors.map((cursor) => ({ ...cursor }));
  let lastRunning: string | null = null;
  const lastRun = new Map<string, string>();

  let closed = false;
  let unsubscribeFocus: (() => void) | null = null;
  let unsubscribeSource: (() => void) | null = null;
  let ping: ReturnType<typeof setInterval> | null = null;
  let chain: Promise<void> = Promise.resolve();

  const cleanup = (): void => {
    if (closed) return;
    closed = true;
    if (ping !== null) {
      clearInterval(ping);
      ping = null;
    }
    if (unsubscribeFocus !== null) {
      unsubscribeFocus();
      unsubscribeFocus = null;
    }
    if (unsubscribeSource !== null) {
      unsubscribeSource();
      unsubscribeSource = null;
    }
  };

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const enqueue = (chunk: string): void => {
        if (closed) return;
        try {
          controller.enqueue(encoder.encode(chunk));
        } catch (err) {
          cleanup();
          throw err;
        }
      };
      const send = (event: string, data: unknown): void => {
        enqueue(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
      };
      const queue = (job: () => void | Promise<void>): void => {
        chain = chain.then(job).catch((err) => {
          log(`[live] job failed: ${String(err)}`);
        });
      };

      const refreshRunning = async (): Promise<void> => {
        try {
          const frame = await source.running();
          const json = JSON.stringify(frame);
          if (json !== lastRunning) {
            send("running", frame);
            lastRunning = json;
          }
        } catch (err) {
          log(`[live] running read failed: ${String(err)}`);
        }
      };

      const refreshRun = async (cursor: Cursor): Promise<void> => {
        const key = `${cursor.project}:${cursor.run_id}`;
        try {
          const header = await source.runHeader(cursor.project, cursor.run_id);
          if (header !== null) {
            const json = JSON.stringify(header);
            if (json !== lastRun.get(key)) {
              send("run", header);
              lastRun.set(key, json);
            }
          }
        } catch (err) {
          log(`[live] run read failed: ${String(err)}`);
        }
        try {
          for (;;) {
            const page = await source.eventsAfter(
              cursor.project,
              cursor.run_id,
              cursor.seq,
              EVENTS_PAGE,
            );
            if (page.events.length === 0) break;
            send("events", {
              project: cursor.project,
              run_id: cursor.run_id,
              events: page.events,
              next: page.next,
            });
            cursor.seq = page.next;
            if (!page.has_more) break;
          }
        } catch (err) {
          log(`[live] events read failed: ${String(err)}`);
        }
      };

      const snapshot = async (): Promise<void> => {
        await refreshRunning();
        for (const cursor of cursors) await refreshRun(cursor);
      };
      const projectJob = async (project: string): Promise<void> => {
        await refreshRunning();
        for (const cursor of cursors) {
          if (cursor.project === project) await refreshRun(cursor);
        }
      };

      enqueue(`retry: ${RETRY_MS}\n: open\n\n`);

      // The connect snapshot is Focus first, then running and the subscribed
      // Runs. A cached read is sent at once; with none, the coordinated read
      // is awaited so Focus cannot trail the subscribed Run data. Only after
      // that first Focus frame is subscribed for later publishes, so the
      // initial read cannot double-send.
      const startFocus = async (): Promise<void> => {
        const cached = source.focusLatest();
        if (cached !== null) {
          send("focus", cached);
        } else {
          try {
            send("focus", await source.focusRead());
          } catch (err) {
            log(`[live] focus read failed: ${String(err)}`);
          }
        }
        if (closed) return;
        unsubscribeFocus = source.onFocus((res) => {
          queue(() => send("focus", res));
        });
      };
      queue(startFocus);
      queue(snapshot);
      unsubscribeSource = source.onChange((change) => {
        if (change.type === "tick") queue(snapshot);
        else queue(() => projectJob(change.project));
      });
      ping = setInterval(() => {
        queue(() => enqueue(": ping\n\n"));
      }, pingMs);
      signal.addEventListener("abort", cleanup, { once: true });
    },
    cancel() {
      cleanup();
    },
  });

  return new Response(stream, {
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}
