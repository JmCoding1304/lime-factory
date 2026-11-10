/**
 * The Focus tab's file watcher: it observes the files that feed `lime status`
 * and `lime focus list`, then spawns lime through `limeProxy.ts` to re-read the
 * report. It never reads or writes a watched file, the home or any db — factory
 * state stays owned by the Python CLI; this module only watches and shells out.
 *
 * One debounced, single-flight read: file events are coalesced into one read
 * per gap, reads never overlap, and a quiet change is read after the debounce.
 * The bursty runs db (a live Run) is gated by the full READ_GAP_MS, while a rare
 * ticket, ADR or home event reads on the short debounce so it reaches the tab
 * well inside 5 s. Each read is published to subscribers — the live feed's
 * `focus` frames.
 */
import { existsSync, watch as fsWatch } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { homeFocus, failureLine, runLime, SUMMARIZE_TIMEOUT_MS } from "./limeProxy.ts";
import type { HomeFocusResponse, StatusReport } from "../shared/types.ts";

/** Quiet window after the last file event before a read fires. */
export const DEBOUNCE_MS = 500;

/** Minimum distance between two read starts, so a busy tree cannot spin. */
export const READ_GAP_MS = 10_000;

/** The dashboard home's focus list file; the only home file we watch. */
export const FOCUS_FILE = "focus.md";

/** The dashboard home under `~/.lime`, resolved from the current `HOME`. */
export function dashboardHome(): string {
  return join(homedir(), ".lime", "dashboard");
}

/** A registered project the watcher may follow. */
export interface WatchProject {
  name: string;
  path: string;
}

/** One watched directory plus the filename predicate that matters inside it. */
export interface WatchTarget {
  /** The owning project's name, or null for the shared home target. */
  project: string | null;
  dir: string;
  match(name: string): boolean;
  /**
   * True for the bursty runs dir (`lime.db` and its `-wal`), which a live Run
   * rewrites constantly: such events are gated by the full `READ_GAP_MS`. A
   * ticket, ADR or home event is rare and reads on the much shorter debounce,
   * so a new ticket still reaches the tab well inside 5 s.
   */
  bursty: boolean;
}

function isMarkdown(name: string): boolean {
  return name.endsWith(".md");
}

/** Only lime.db and its -wal sidecar; never -shm, which every read touches. */
function isRunsFile(name: string): boolean {
  return name === "lime.db" || name === "lime.db-wal";
}

/** The three per-project dirs plus one home target, in a stable order. */
export function watchTargets(
  projects: WatchProject[],
  homeDir: string,
): WatchTarget[] {
  const targets: WatchTarget[] = [];
  for (const project of projects) {
    targets.push({
      project: project.name,
      dir: join(project.path, ".lime", "tickets"),
      match: isMarkdown,
      bursty: false,
    });
    targets.push({
      project: project.name,
      dir: join(project.path, ".lime", "runs"),
      match: isRunsFile,
      bursty: true,
    });
    targets.push({
      project: project.name,
      dir: join(project.path, "docs", "adr"),
      match: isMarkdown,
      bursty: false,
    });
  }
  targets.push({
    project: null,
    dir: homeDir,
    match: (name) => name === FOCUS_FILE,
    bursty: false,
  });
  return targets;
}

export interface FocusWatcherOpts {
  projects: WatchProject[];
  homeDir?: string;
  bin?: string;
  registry?: string;
  debounceMs?: number;
  gapMs?: number;
  summarizeTimeoutMs?: number;
  log?: (line: string) => void;
  read?: () => Promise<HomeFocusResponse>;
}

/**
 * Watches a set of directories and keeps one debounced, single-flight read of
 * the home focus in the air. `start()` is idempotent; `stop()` closes every
 * watcher and the pending timer while letting an in-flight read finish. A
 * direct `refresh()` (a mount or reconnect fetch) joins the in-flight read or
 * reuses a result younger than the gap, so it never spawns a second process.
 */
export class FocusWatcher {
  private readonly projects: WatchProject[];
  private readonly homeDir: string;
  private readonly bin: string | undefined;
  private readonly registry: string | undefined;
  private readonly debounceMs: number;
  private readonly gapMs: number;
  private readonly summarizeTimeoutMs: number;
  private readonly log: (line: string) => void;
  private readonly read: () => Promise<HomeFocusResponse>;

  private readonly watchers: Array<ReturnType<typeof fsWatch>> = [];
  private readonly subscribers = new Set<(res: HomeFocusResponse) => void>();
  /** Projects with a `focus summarize` spawn currently in flight. */
  private readonly running = new Set<string>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  /** When the pending timer fires; earlier than a later `at` keeps priority. */
  private timerAt = Infinity;
  private started = false;
  private stopped = false;
  private dirty = false;
  /** A non-bursty event is pending, so the short debounce gap applies. */
  private fastDirty = false;
  /**
   * A bursty (runs db) event is pending. While one is, the full read gap wins
   * over any fast event, so a live Run's constant writes pace every read by
   * `gapMs` even if a stray directory-level event slips in beside them.
   */
  private slowDirty = false;
  private reading = false;
  private lastStart = -Infinity;
  /** The in-flight read, so a concurrent caller joins it instead of spawning. */
  private inflight: Promise<HomeFocusResponse> | null = null;
  /** The most recent read result, reused by a caller inside the gap window. */
  private lastResult: HomeFocusResponse | null = null;

  constructor(opts: FocusWatcherOpts) {
    this.projects = opts.projects;
    this.homeDir = opts.homeDir ?? dashboardHome();
    this.bin = opts.bin;
    this.registry = opts.registry;
    this.debounceMs = opts.debounceMs ?? DEBOUNCE_MS;
    this.gapMs = opts.gapMs ?? READ_GAP_MS;
    this.summarizeTimeoutMs = opts.summarizeTimeoutMs ?? SUMMARIZE_TIMEOUT_MS;
    this.log = opts.log ?? ((line) => console.error(`[focus] ${line}`));
    this.read =
      opts.read ??
      (() => homeFocus({ bin: this.bin, registry: this.registry }));
  }

  /**
   * The newest read without starting one; the live feed's connect snapshot
   * uses it. Null before the first read has landed.
   */
  latest(): HomeFocusResponse | null {
    return this.lastResult;
  }

  /** Register a publish listener; returns the unsubscribe. */
  subscribe(fn: (res: HomeFocusResponse) => void): () => void {
    this.subscribers.add(fn);
    return () => {
      this.subscribers.delete(fn);
    };
  }

  /** Start watching and run one catch-up read. Safe to call twice. */
  start(): void {
    if (this.started) return;
    this.started = true;

    const missing = new Set<string>();
    for (const project of this.projects) {
      if (!existsSync(project.path)) {
        this.log(`focus watch: ${project.name}: project folder missing, skipped`);
        missing.add(project.name);
      }
    }

    for (const target of watchTargets(this.projects, this.homeDir)) {
      if (target.project !== null && missing.has(target.project)) continue;
      if (!existsSync(target.dir)) continue;
      try {
        const watcher = fsWatch(target.dir, (_event, name) => {
          if (name == null || target.match(String(name))) {
            this.kick(target.bursty);
          }
        });
        watcher.on("error", (err) => {
          this.log(`focus watch: ${target.dir}: ${(err as Error).message}`);
        });
        this.watchers.push(watcher);
      } catch (err) {
        this.log(`focus watch: ${target.dir}: ${(err as Error).message}`);
      }
    }

    this.kick();
  }

  /** Close every watcher and clear the timer; an in-flight read still finishes. */
  stop(): void {
    if (this.stopped) return;
    this.stopped = true;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    for (const watcher of this.watchers) {
      try {
        watcher.close();
      } catch {
        // A watcher already closed by the runtime is fine.
      }
    }
    this.watchers.length = 0;
  }

  /**
   * The watcher's coordinated read for a direct caller (a mount or reconnect
   * fetch of `/api/home/focus`). It never bypasses the single-flight: a read
   * already in the air is joined, and a result fresher than the read gap is
   * reused, so a mount cannot spawn a second `lime status` inside the window.
   */
  refresh(): Promise<HomeFocusResponse> {
    if (this.inflight) return this.inflight;
    if (
      this.lastResult !== null &&
      Date.now() - this.lastStart < this.gapMs
    ) {
      return Promise.resolve(this.lastResult);
    }
    // This read supersedes any scheduled one; drop the timer so it cannot
    // spawn a duplicate right after.
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.dirty = false;
    this.fastDirty = false;
    this.slowDirty = false;
    return this.startRead();
  }

  /** Mark the state dirty and (re)schedule a read. */
  kick(bursty = false): void {
    this.dirty = true;
    if (bursty) this.slowDirty = true;
    else this.fastDirty = true;
    this.schedule();
  }

  private schedule(): void {
    if (this.stopped || this.reading) return;
    // A bursty (runs db) event waits the full gap; a rare ticket/ADR/home event
    // only waits the debounce, so it is delivered well inside 5 s. A bursty
    // event pending beside a fast one keeps the full gap.
    const gap =
      !this.slowDirty && this.fastDirty ? this.debounceMs : this.gapMs;
    const delay = Math.max(
      this.debounceMs,
      this.lastStart + gap - Date.now(),
    );
    const at = Date.now() + delay;
    // Keep an already-earlier timer; only move it sooner.
    if (this.timer !== null && this.timerAt <= at) return;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timerAt = at;
    this.timer = setTimeout(() => {
      void this.fire();
    }, delay);
  }

  private async fire(): Promise<void> {
    this.timer = null;
    if (this.stopped || !this.dirty) return;
    const gap =
      !this.slowDirty && this.fastDirty ? this.debounceMs : this.gapMs;
    if (Date.now() < this.lastStart + gap) {
      this.schedule();
      return;
    }
    this.dirty = false;
    this.fastDirty = false;
    this.slowDirty = false;
    try {
      await this.startRead();
    } catch {
      // performRead already logged the failure.
    }
  }

  /**
   * Run one read unless one is already in the air, then fan the result out and
   * summarize any pending items. The in-flight promise is shared with every
   * caller, so reads never overlap and a mount joins the watcher's read.
   */
  private startRead(): Promise<HomeFocusResponse> {
    if (this.inflight) return this.inflight;
    const p = this.performRead().finally(() => {
      if (this.inflight === p) this.inflight = null;
      if (this.dirty) this.schedule();
    });
    this.inflight = p;
    return p;
  }

  private async performRead(): Promise<HomeFocusResponse> {
    this.reading = true;
    this.lastStart = Date.now();
    try {
      const res = await this.read();
      this.lastResult = res;
      for (const line of res.errors) this.log(line);
      for (const fn of this.subscribers) {
        try {
          fn(res);
        } catch (err) {
          this.log(`focus subscriber threw: ${(err as Error).message}`);
        }
      }
      this.summarizePending(res.report);
      return res;
    } catch (err) {
      this.log(`focus read failed: ${(err as Error).message}`);
      throw err;
    } finally {
      this.reading = false;
    }
  }

  /**
   * Start one `focus summarize --project P` per project that has a pending
   * item, at most one at a time. The summary's own lime.db write triggers the
   * next read, so there is no re-kick here; a run that wrote nothing waits for
   * the next change.
   */
  private summarizePending(report: StatusReport | null): void {
    if (!report) return;
    const pending = new Set<string>();
    for (const it of report.items) {
      if (it.summary_state === "pending" && it.project !== null) {
        pending.add(it.project);
      }
    }
    for (const name of pending) {
      if (this.running.has(name)) continue;
      const project = this.projects.find((p) => p.name === name);
      if (!project || !existsSync(project.path)) {
        this.log(`focus summarize: ${name}: project folder missing, skipped`);
        continue;
      }
      this.running.add(name);
      void runLime(
        ["focus", "summarize", "--project", name],
        this.summarizeTimeoutMs,
        { bin: this.bin, registry: this.registry },
      )
        .then((run) => {
          const line = failureLine(
            `lime focus summarize --project ${name}`,
            run,
            this.summarizeTimeoutMs,
          );
          if (line) this.log(line);
        })
        .finally(() => {
          this.running.delete(name);
        });
    }
  }
}
