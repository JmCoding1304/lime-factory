// The live feed: one server-sent-events connection per browser tab. The
// server keeps no subscription state; the client owns each Run's cursor and
// sends it back on every (re)connect, so a reconnect never repeats a line.
import { shallowRef, type ShallowRef } from "vue";
import type {
  FeedEvent,
  FeedEvents,
  FeedRun,
  HomeFocusResponse,
  LiveRunning,
} from "./types";

/** The one feed route. */
export const LIVE_URL = "/api/live";

/** How long to wait before reopening a dropped stream. */
export const REOPEN_MS = 3_000;

/** The EventSource surface `createLive` drives; a fake stands in for tests. */
export type StreamLike = Pick<
  EventSource,
  "onerror" | "addEventListener" | "close"
>;

/** One Run the feed is subscribed to, structurally equal to `editorTabs.RunRef`. */
export interface LiveRunRef {
  project: string;
  runId: string;
}

/** One subscribed Run's state: its header, its events and the client's cursor. */
export interface RunFeed {
  project: string;
  runId: string;
  run: FeedRun | null;
  events: FeedEvent[];
  cursor: number;
}

/** The map key of a subscribed Run. */
export function runKey(project: string, runId: string): string {
  return `${project}:${runId}`;
}

/**
 * The feed URL for the current Runs: the bare route with none, else one
 * `run=<project>:<run_id>:<seq>` per Run, in iteration order.
 */
export function liveUrl(
  runs: Iterable<Pick<RunFeed, "project" | "runId" | "cursor">>,
): string {
  const params = new URLSearchParams();
  for (const r of runs) params.append("run", `${r.project}:${r.runId}:${r.cursor}`);
  const query = params.toString();
  return query ? `${LIVE_URL}?${query}` : LIVE_URL;
}

/** What `createLive` needs: a source factory and the reopen delay. */
export interface LiveOptions {
  open: (url: string) => StreamLike;
  reopenMs?: number;
}

/** The app's one live-feed client, exposed as shallow refs. */
export interface LiveFeed {
  focus: ShallowRef<HomeFocusResponse | null>;
  running: ShallowRef<LiveRunning | null>;
  runs: ShallowRef<ReadonlyMap<string, RunFeed>>;
  /** True from a dropped stream until the next frame that parses. */
  down: ShallowRef<boolean>;
  start(): void;
  stop(): void;
  setRuns(refs: readonly LiveRunRef[]): void;
  runFeed(project: string, runId: string): RunFeed | undefined;
}

/**
 * A live-feed client. Nothing opens before `start()`; `start()` is idempotent
 * and opens one source on `liveUrl(runs.value)`. Every error closes the source
 * and reopens once after `reopenMs` with the current cursors. `running` holds
 * the whole frame (`{ runs }`) so 262's `queued` reaches readers here; `focus`
 * and `running` are null until their first frame. `down` is set by a dropped
 * stream and cleared by the next frame that parses, so a malformed frame never
 * clears it and `stop()` leaves it as it is.
 */
export function createLive(opts: LiveOptions): LiveFeed {
  const reopenMs = opts.reopenMs ?? REOPEN_MS;
  const focus = shallowRef<HomeFocusResponse | null>(null);
  const running = shallowRef<LiveRunning | null>(null);
  const runs = shallowRef<ReadonlyMap<string, RunFeed>>(new Map());
  const down = shallowRef(false);

  let started = false;
  let source: StreamLike | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const connect = (): void => {
    const src = opts.open(liveUrl(runs.value.values()));
    source = src;

    const on = (type: string, fn: (data: unknown) => void): void => {
      src.addEventListener(type, (ev) => {
        // A superseded or closed source is ignored.
        if (src !== source) return;
        let data: unknown;
        try {
          data = JSON.parse((ev as MessageEvent).data);
        } catch {
          return; // malformed frame data is ignored, never thrown
        }
        down.value = false;
        fn(data);
      });
    };

    on("focus", (data) => {
      focus.value = data as HomeFocusResponse;
    });

    on("running", (data) => {
      running.value = data as LiveRunning;
    });

    on("run", (data) => {
      const frame = data as FeedRun;
      const key = runKey(frame.run.project, frame.run.run_id);
      const entry = runs.value.get(key);
      if (!entry) return; // an unsubscribed Run is ignored
      const next = new Map(runs.value);
      next.set(key, { ...entry, run: frame });
      runs.value = next;
    });

    on("events", (data) => {
      const frame = data as FeedEvents;
      const key = runKey(frame.project, frame.run_id);
      const entry = runs.value.get(key);
      if (!entry) return; // an unsubscribed Run is ignored
      let cursor = entry.cursor;
      const kept: FeedEvent[] = [];
      for (const e of frame.events) {
        // Drop a replay at or below the cursor and a repeat inside one frame.
        if (e.seq <= cursor) continue;
        cursor = e.seq;
        kept.push(e);
      }
      if (kept.length === 0) return;
      const next = new Map(runs.value);
      next.set(key, { ...entry, events: [...entry.events, ...kept], cursor });
      runs.value = next;
    });

    src.onerror = () => {
      if (src !== source) return;
      down.value = true;
      src.close();
      source = null;
      if (started && timer === null) {
        // The URL is built at reconnect time, so it carries the current cursors.
        timer = setTimeout(() => {
          timer = null;
          if (started) connect();
        }, reopenMs);
      }
    };
  };

  const start = (): void => {
    if (started) return;
    started = true;
    connect();
  };

  const stop = (): void => {
    started = false;
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    source?.close();
    source = null;
  };

  const setRuns = (refs: readonly LiveRunRef[]): void => {
    const wanted = new Map<string, LiveRunRef>();
    for (const ref of refs) wanted.set(runKey(ref.project, ref.runId), ref);

    const current = runs.value;
    const sameSet =
      wanted.size === current.size &&
      [...wanted.keys()].every((key) => current.has(key));
    if (sameSet) return;

    const next = new Map<string, RunFeed>();
    for (const [key, ref] of wanted) {
      next.set(
        key,
        current.get(key) ?? {
          project: ref.project,
          runId: ref.runId,
          run: null,
          events: [],
          cursor: 0,
        },
      );
    }
    runs.value = next;

    if (!started) return;
    // A pending reopen opens with the new set; only an open source reconnects now.
    if (timer !== null) return;
    if (source !== null) {
      source.close();
      source = null;
      connect();
    }
  };

  const runFeed = (project: string, runId: string): RunFeed | undefined =>
    runs.value.get(runKey(project, runId));

  return { focus, running, runs, down, start, stop, setRuns, runFeed };
}

/**
 * The app's one live-feed client. This is the only `new EventSource(` in
 * `dashboard/src`; nothing connects until `App.vue` calls `live.start()`.
 */
export const live: LiveFeed = createLive({ open: (url) => new EventSource(url) });
