import { test, expect, describe } from "bun:test";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import {
  createLive,
  liveUrl,
  LIVE_URL,
  REOPEN_MS,
  type StreamLike,
} from "./live";
import type {
  FeedEvents,
  FeedRun,
  HomeFocusResponse,
  LiveRunning,
} from "./types";

/** One fake EventSource: records its URL, listeners and close, drives emits. */
class FakeSource {
  closed = false;
  onerror: (() => void) | null = null;
  readonly listeners: Record<string, ((ev: MessageEvent) => void)[]> = {};
  constructor(readonly url: string) {}
  addEventListener(type: string, fn: (ev: MessageEvent) => void) {
    (this.listeners[type] ??= []).push(fn);
  }
  close() {
    this.closed = true;
  }
  emit(type: string, data: unknown) {
    const ev = {
      data: typeof data === "string" ? data : JSON.stringify(data),
    } as MessageEvent;
    for (const fn of this.listeners[type] ?? []) fn(ev);
  }
  fail() {
    this.onerror?.();
  }
}

function harness(reopenMs = 10) {
  const sources: FakeSource[] = [];
  const feed = createLive({
    open: (url) => {
      const fake = new FakeSource(url);
      sources.push(fake);
      return fake as unknown as StreamLike;
    },
    reopenMs,
  });
  return { sources, feed };
}

const wait = () => new Promise((r) => setTimeout(r, 25));
const runsParam = (url: string) =>
  new URL(url, "http://x").searchParams.getAll("run");

const eventsFrame = (project: string, runId: string, seqs: number[]) =>
  ({
    project,
    run_id: runId,
    events: seqs.map((seq) => ({ seq })),
    next: seqs.at(-1) ?? 0,
  }) as unknown as FeedEvents;

const runFrame = (project: string, runId: string) =>
  ({
    run: { project, run_id: runId },
    envelopes: [],
    gates: [],
    live: true,
  }) as unknown as FeedRun;

test("liveUrl with no Run is the bare feed URL", () => {
  expect(liveUrl([])).toBe(LIVE_URL);
});

test("liveUrl carries one run=<project>:<run_id>:<seq> per Run", () => {
  const url = liveUrl([
    { project: "p", runId: "r1", cursor: 0 },
    { project: "q", runId: "r2", cursor: 7 },
  ]);
  expect(runsParam(url)).toEqual(["p:r1:0", "q:r2:7"]);
});

test("nothing opens before start, and start twice opens one source on /api/live", () => {
  const { sources, feed } = harness();
  expect(sources.length).toBe(0);
  feed.start();
  feed.start();
  expect(sources.length).toBe(1);
  expect(new URL(sources[0].url, "http://x").pathname).toBe(LIVE_URL);
  feed.stop();
});

test("focus and running frames land in their refs", () => {
  const { sources, feed } = harness();
  feed.start();
  const focusFrame: HomeFocusResponse = { report: null, goals: null, errors: [] };
  sources[0].emit("focus", focusFrame);
  expect(feed.focus.value).toEqual(focusFrame);
  const runningFrame: LiveRunning = { runs: [] };
  sources[0].emit("running", runningFrame);
  expect(feed.running.value).toEqual(runningFrame);
  feed.stop();
});

test("malformed frame data is ignored and does not throw", () => {
  const { sources, feed } = harness();
  feed.start();
  expect(() => sources[0].emit("focus", "{not json")).not.toThrow();
  expect(feed.focus.value).toBeNull();
  expect(feed.running.value).toBeNull();
  feed.stop();
});

test("a run frame fills its subscribed Run and ignores an unsubscribed one", () => {
  const { sources, feed } = harness();
  feed.setRuns([{ project: "p", runId: "r1" }]);
  feed.start();
  const frame = runFrame("p", "r1");
  sources[0].emit("run", frame);
  expect(feed.runFeed("p", "r1")?.run).toEqual(frame);
  const before = feed.runs.value;
  sources[0].emit("run", runFrame("zzz", "r9"));
  expect(feed.runs.value).toBe(before);
  expect(feed.runFeed("zzz", "r9")).toBeUndefined();
  feed.stop();
});

test("events append and advance the cursor", () => {
  const { sources, feed } = harness();
  feed.setRuns([{ project: "p", runId: "r1" }]);
  feed.start();
  sources[0].emit("events", eventsFrame("p", "r1", [1, 2, 3]));
  expect(feed.runFeed("p", "r1")?.events.map((e) => e.seq)).toEqual([1, 2, 3]);
  expect(feed.runFeed("p", "r1")?.cursor).toBe(3);
  feed.stop();
});

test("an event at or below its Run's cursor is dropped, so a replay never shows a line twice", () => {
  const { sources, feed } = harness();
  feed.setRuns([{ project: "p", runId: "r1" }]);
  feed.start();
  sources[0].emit("events", eventsFrame("p", "r1", [1, 2, 3]));
  sources[0].emit("events", eventsFrame("p", "r1", [2, 3, 4]));
  expect(feed.runFeed("p", "r1")?.events.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
  expect(feed.runFeed("p", "r1")?.cursor).toBe(4);
  const before = feed.runs.value;
  sources[0].emit("events", eventsFrame("p", "r1", [1, 2, 3, 4]));
  expect(feed.runs.value).toBe(before);
  feed.stop();
});

test("an events frame for an unknown Run is ignored", () => {
  const { sources, feed } = harness();
  feed.setRuns([{ project: "p", runId: "r1" }]);
  feed.start();
  const before = feed.runs.value;
  sources[0].emit("events", eventsFrame("q", "r2", [1]));
  expect(feed.runs.value).toBe(before);
  feed.stop();
});

test("a dropped stream reopens after 3 s with the current cursors", async () => {
  expect(REOPEN_MS).toBe(3000);
  const { sources, feed } = harness();
  feed.setRuns([{ project: "p", runId: "r1" }]);
  feed.start();
  sources[0].emit("events", eventsFrame("p", "r1", [1, 2, 3]));
  sources[0].fail();
  expect(sources[0].closed).toBe(true);
  expect(sources.length).toBe(1);
  sources[0].fail();
  await wait();
  expect(sources.length).toBe(2);
  expect(runsParam(sources[1].url)).toEqual(["p:r1:3"]);
  feed.stop();
});

test("opening or closing a Run tab reopens the stream with the new set", () => {
  const { sources, feed } = harness();
  feed.setRuns([{ project: "p", runId: "r1" }]);
  feed.start();
  expect(runsParam(sources[0].url)).toEqual(["p:r1:0"]);

  sources[0].emit("events", eventsFrame("p", "r1", [1, 2, 3]));

  feed.setRuns([
    { project: "p", runId: "r1" },
    { project: "q", runId: "r2" },
  ]);
  expect(sources[0].closed).toBe(true);
  expect(sources.length).toBe(2);
  expect(runsParam(sources[1].url)).toEqual(["p:r1:3", "q:r2:0"]);

  // The same key set reordered does nothing.
  feed.setRuns([
    { project: "q", runId: "r2" },
    { project: "p", runId: "r1" },
  ]);
  expect(sources.length).toBe(2);

  feed.setRuns([{ project: "q", runId: "r2" }]);
  expect(feed.runFeed("p", "r1")).toBeUndefined();
  expect(sources.length).toBe(3);
  expect(runsParam(sources[2].url)).toEqual(["q:r2:0"]);

  // The same set again opens nothing.
  feed.setRuns([{ project: "q", runId: "r2" }]);
  expect(sources.length).toBe(3);
  feed.stop();
});

test("setRuns while a reopen is pending opens once, after the delay, with the new set", async () => {
  const { sources, feed } = harness();
  feed.setRuns([{ project: "p", runId: "r1" }]);
  feed.start();
  sources[0].fail();
  expect(sources.length).toBe(1);
  feed.setRuns([{ project: "q", runId: "r2" }]);
  expect(sources.length).toBe(1);
  await wait();
  expect(sources.length).toBe(2);
  expect(runsParam(sources[1].url)).toEqual(["q:r2:0"]);
  feed.stop();
});

test("stop closes the source, cancels a pending reopen, and frames from a closed or superseded source are ignored", async () => {
  const { sources, feed } = harness();
  feed.setRuns([{ project: "p", runId: "r1" }]);
  feed.start();
  sources[0].fail();
  feed.stop();
  await wait();
  expect(sources.length).toBe(1);
  expect(sources[0].closed).toBe(true);

  const { sources: s2, feed: f2 } = harness();
  f2.setRuns([{ project: "p", runId: "r1" }]);
  f2.start();
  const superseded = s2[0];
  f2.setRuns([
    { project: "p", runId: "r1" },
    { project: "q", runId: "r2" },
  ]);
  // The superseded source's frames no longer land.
  superseded.emit("focus", { report: null, goals: null, errors: [] });
  superseded.emit("events", eventsFrame("p", "r1", [1, 2, 3]));
  expect(f2.focus.value).toBeNull();
  expect(f2.runFeed("p", "r1")?.events).toEqual([]);
  f2.stop();
});

describe("down", () => {
  test("starts false, a dropped stream sets it, and the next parsed frame clears it", async () => {
    const { sources, feed } = harness();
    feed.start();
    expect(feed.down.value).toBe(false);
    sources[0].fail();
    expect(feed.down.value).toBe(true);
    await wait();
    expect(sources.length).toBe(2);
    sources[1].emit("running", { runs: [] } as LiveRunning);
    expect(feed.down.value).toBe(false);
    feed.stop();
  });

  test("a malformed frame never clears it", async () => {
    const { sources, feed } = harness();
    feed.start();
    sources[0].fail();
    await wait();
    sources[1].emit("running", "{not json");
    expect(feed.down.value).toBe(true);
    feed.stop();
  });

  test("a superseded source's error does not set it", () => {
    const { sources, feed } = harness();
    feed.setRuns([{ project: "p", runId: "r1" }]);
    feed.start();
    const superseded = sources[0];
    feed.setRuns([
      { project: "p", runId: "r1" },
      { project: "q", runId: "r2" },
    ]);
    superseded.fail();
    expect(feed.down.value).toBe(false);
    feed.stop();
  });

  test("stop leaves it as it is", () => {
    const { sources, feed } = harness();
    feed.start();
    sources[0].fail();
    expect(feed.down.value).toBe(true);
    feed.stop();
    expect(feed.down.value).toBe(true);
  });
});

describe("wiring", () => {
  const dir = join(import.meta.dir, "..");
  const files = (readdirSync(dir, { recursive: true }) as string[])
    .map((f) => f.split("\\").join("/"))
    .filter((f) => (f.endsWith(".ts") || f.endsWith(".vue")) && !f.endsWith(".test.ts"));
  const source = (f: string) => readFileSync(join(dir, f), "utf8");

  test("nothing in dashboard/src calls /api/home/stream", () => {
    const needle = "/api/home/" + "stream";
    const callers = files.filter((f) => source(f).includes(needle));
    expect(callers).toEqual([]);
  });

  test("only lib/live.ts constructs an EventSource", () => {
    const constructors = files.filter((f) => /new EventSource\(/.test(source(f)));
    expect(constructors).toEqual(["lib/live.ts"]);
  });

  test("App starts the feed on mount and stops it on unmount", () => {
    const app = readFileSync(join(dir, "App.vue"), "utf8");
    expect(app).toContain("live.start()");
    expect(app).toContain("live.stop()");
  });

  test("the Runs pool refetches on running, with no poll", () => {
    const app = source("App.vue");
    expect(app).toContain("watch(live.running");
    expect(app).toContain("refetcher(");
    expect(app).not.toContain("setInterval");
    expect(app).not.toContain("RUNS_POLL_MS");
    expect(source("lib/liveness.ts")).not.toContain("RUNS_POLL_MS");
    expect(source("components/RunsSheet.vue")).not.toContain("fetchRuns");
    expect(source("components/Workspace.vue")).not.toContain("fetchRuns");
  });

  test("open Run tabs set the feed's Runs", () => {
    expect(source("components/Editor.vue")).toContain("emit('runs'");
    const ws = source("components/Workspace.vue");
    expect(ws).toContain('@runs="onRunTabs"');
    expect(ws).toContain("live.setRuns(");
  });
});
