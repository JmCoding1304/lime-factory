/**
 * Focus tab logic — turns lime's `status --json` report and `focus list --json`
 * goals into one ranked, partitioned row list. Pure TypeScript, no Vue import,
 * so every rule here is unit-testable. lime already ranks the report by group;
 * for the Focus list we present each project's rows oldest-first instead.
 */
import type {
  FocusGoal,
  HomeBlock,
  HomeFocusResponse,
  HomeTickRequest,
  HomeTickResponse,
  HomeVisitResponse,
  StatusReport,
} from "./types";

/** The init line a home-less visit yields — the one source is lime dash visit. */
export const INIT_LINE = "dashboard: no home repo — run lime dash init";

/** A row's kind — a goal, or the report group it came from. */
export type FocusKind = "goal" | "needs_you" | "failed" | "ready" | "done";

/** The marker label shown for each row kind. */
export const KIND_LABEL: Record<FocusKind, string> = {
  goal: "Goal",
  needs_you: "Needs you",
  failed: "Failed",
  ready: "Ready to deploy",
  done: "Done",
};

/** One rendered row — a goal or a report item, flattened to the fields the UI draws. */
export interface FocusRow {
  key: string;
  kind: FocusKind;
  title: string;
  num: string;
  project: string | null;
  ticket: string | null;
  run_id: string | null;
  since: string | null;
  due: string | null;
  overdue: boolean;
  detail: string | null;
  goalId: string | null;
  /** The report reason (null on goals) — a person-ticket is `needs_you` + `ticket`. */
  reason: string | null;
  /** A goal, or a `needs_you` `ticket` — the only rows with a write checkbox. */
  checkable: boolean;
  /** The row's effective done flag: a goal's own, a done group's, else false. */
  done: boolean;
  /** A tick in flight keeps the row struck in place. */
  struck: boolean;
  /** The AI "what to do" line, or null on goals and count rows. */
  summary: string | null;
  summaryState: "ok" | "pending" | "failed" | null;
  /** The copyable actions a report row carries (`Resume`, `Remove`, `Deploy`). */
  actions: string[];
}

/** The leading digits of a ticket slug (e.g. `227-focus-tab` → `227`), or `""`. */
export function ticketNum(slug: string | null): string {
  return slug?.match(/^\d+/)?.[0] ?? "";
}

const REASON_LABEL: Record<string, string> = {
  ticket: "needs you",
  breach: "breach",
  blocked: "blocked",
  refusal: "refusal",
  conflict: "conflict",
  ci_red: "CI red",
  fail: "failed",
  budget_exhausted: "budget exhausted",
  stale_running: "stopped mid-run",
  ready: "ready to deploy",
  deployed: "deployed",
  merged: "merged",
  closed: "closed",
  adr_proposed: "proposed ADR",
  not_launched: "not launched",
};

/** A human label for a report reason; unknown reasons spell out their underscores. */
export function reasonLabel(reason: string): string {
  return REASON_LABEL[reason] ?? reason.replace(/_/g, " ");
}

/**
 * A row's oldest-first presentation key: goals (rank 0) before report rows
 * (rank 1), then the row's date — a goal's `due`, a report row's `since` —
 * ascending, with a missing date sorting last as `9999`.
 */
function presentationKey(row: FocusRow): [number, string] {
  const age = row.kind === "goal" ? row.due : row.since;
  return [row.kind === "goal" ? 0 : 1, age ?? "9999"];
}

/** Every goal as a goal row, in source order — a done goal now sits in the footer. */
export function goalRows(goals: FocusGoal[]): FocusRow[] {
  return goals.map((g) => ({
    key: "goal:" + g.id,
    kind: "goal" as const,
    title: g.title,
    num: g.id,
    project: g.project,
    ticket: null,
    run_id: null,
    since: null,
    due: g.due,
    overdue: g.overdue,
    detail: null,
    goalId: g.id,
    reason: null,
    checkable: true,
    done: g.done,
    struck: false,
    summary: null,
    summaryState: null,
    actions: [],
  }));
}

/**
 * Split a report into open and done rows. Count items (`count !== null`) never
 * become rows — they are simply skipped, and no counts line is built. Report
 * order is preserved verbatim — lime already ranked it. Rows carry a stable
 * key (no index): on a collision within one report a `#2`, `#3` … suffix keeps
 * them distinct.
 */
export function reportRows(report: StatusReport | null): {
  open: FocusRow[];
  done: FocusRow[];
} {
  const open: FocusRow[] = [];
  const done: FocusRow[] = [];
  const seen = new Set<string>();
  for (const item of report?.items ?? []) {
    if (item.count !== null) continue;
    const reason = reasonLabel(item.reason);
    const firstLine = (item.text ?? item.note)?.split("\n")[0] ?? null;
    const baseKey = `${item.group}:${item.project}:${
      item.ticket ?? item.run_id ?? item.reason + "@" + (item.since ?? "")
    }`;
    let key = baseKey;
    for (let n = 2; seen.has(key); n++) key = `${baseKey}#${n}`;
    seen.add(key);
    const row: FocusRow = {
      key,
      kind: item.group === "done" ? "done" : (item.group as FocusKind),
      title: item.title ?? item.ticket ?? reason,
      num: ticketNum(item.ticket),
      project: item.project,
      ticket: item.ticket,
      run_id: item.run_id,
      since: item.since,
      due: null,
      overdue: false,
      detail: firstLine ? `${reason} · ${firstLine}` : reason,
      goalId: null,
      reason: item.reason,
      checkable: item.group === "needs_you" && item.reason === "ticket",
      done: item.group === "done",
      struck: false,
      summary: item.summary,
      summaryState: item.summary_state,
      actions: item.actions,
    };
    if (item.group === "done") done.push(row);
    else open.push(row);
  }
  return { open, done };
}

/** Oldest-first: goals (rank 0) before report rows (rank 1), then the row's age. */
function byAge(a: FocusRow, b: FocusRow): number {
  const [ka, aa] = presentationKey(a);
  const [kb, ab] = presentationKey(b);
  if (ka !== kb) return ka - kb;
  if (aa === ab) return 0;
  return aa < ab ? -1 : 1;
}

/**
 * The Focus list: todo rows oldest-first and stable-partitioned into the
 * current project (`here`) and the rest (`elsewhere`). A project `filter`
 * narrows both the open list and the done footer; when it is set — or `project`
 * is null (All) — everything goes to `here`.
 */
export function focusList(
  rows: { open: FocusRow[]; done: FocusRow[] },
  project: string | null,
  filter: string | null,
): { here: FocusRow[]; elsewhere: FocusRow[]; done: FocusRow[] } {
  const pass = (row: FocusRow) => filter === null || row.project === filter;
  const sorted = [...rows.open].filter(pass).sort(byAge);
  const done = rows.done.filter(pass);
  const here: FocusRow[] = [];
  const elsewhere: FocusRow[] = [];
  if (filter !== null || project === null) {
    here.push(...sorted);
  } else {
    for (const row of sorted) {
      if (row.project === project || row.project === null) here.push(row);
      else elsewhere.push(row);
    }
  }
  return { here, elsewhere, done };
}

// ── Ticks: an in-memory override keyed by row key. The report now lists closed
// person-tickets as `done`/`closed`; a live tick shadows that row so the ticket
// shows once with its checkbox and struck-in-place hold ──

/** How long a ticked row stays struck in place before moving to the footer. */
export const TICK_MS = 600;

/** One tick: the target done state, the row snapshot, and the write's progress. */
export interface Tick {
  done: boolean;
  row: FocusRow;
  at: number;
  phase: "writing" | "ok";
  prev: Tick | null;
}

/** Every in-flight or settled tick, keyed by row key. */
export type Ticks = Record<string, Tick>;

/**
 * A person-ticket's identity across groups — its open `ticket` row and its done
 * `closed` row share it; null on any other row.
 */
function personTicket(row: FocusRow): string | null {
  if (row.project === null || row.ticket === null) return null;
  if (row.reason !== "ticket" && row.reason !== "closed") return null;
  return `${row.project}:${row.ticket}`;
}

/**
 * Merge todo rows from the report and goals with in-memory ticks. Base rows
 * are goals then report rows (open then done), plus each tick's snapshot row
 * when no source lists it. A live tick shadows the report row for its ticket
 * (matched by identity, not key), so a ticket shows exactly once. A ticked row
 * that is done moves to the footer (by `at`, newest first); a held tick stays
 * struck in `open` so its write can be seen before it lands.
 */
export function todoRows(
  report: StatusReport | null,
  goals: FocusGoal[] | null,
  ticks: Ticks,
  holding: readonly string[] = [],
): { open: FocusRow[]; done: FocusRow[] } {
  const reportSplit = reportRows(report);
  const shadows = new Set<string>();
  for (const tick of Object.values(ticks)) {
    const id = personTicket(tick.row);
    if (id !== null) shadows.add(id);
  }
  const shadowed = (row: FocusRow) => {
    const id = personTicket(row);
    return id !== null && shadows.has(id) && !ticks[row.key];
  };
  const base = [
    ...goalRows(goals ?? []),
    ...reportSplit.open.filter((r) => !shadowed(r)),
    ...reportSplit.done.filter((r) => !shadowed(r)),
  ];
  const keys = new Set(base.map((r) => r.key));
  for (const key of Object.keys(ticks)) {
    if (!keys.has(key)) base.push(ticks[key].row);
  }
  const open: FocusRow[] = [];
  const ticked: { row: FocusRow; at: number }[] = [];
  const rest: FocusRow[] = [];
  for (const row of base) {
    const tick = ticks[row.key];
    const done = tick?.done ?? row.done;
    if (done && tick && holding.includes(row.key)) {
      open.push({ ...row, done, struck: true });
    } else if (!done) {
      open.push(row);
    } else if (tick) {
      ticked.push({ row, at: tick.at });
    } else {
      rest.push(row);
    }
  }
  ticked.sort((a, b) => b.at - a.at);
  return { open, done: [...ticked.map((t) => t.row), ...rest] };
}

/** The server request a tick should send for `row`, or null when it has none. */
export function tickRequest(row: FocusRow, done: boolean): HomeTickRequest | null {
  if (row.kind === "goal" && row.goalId) {
    return { kind: "goal", id: row.goalId, done };
  }
  if (row.checkable && row.project !== null && row.num) {
    return { kind: "ticket", project: row.project, number: row.num, done };
  }
  return null;
}

/** Begin a tick: snapshot the row, keep the previous tick as `prev` for a rollback. */
export function startTick(ticks: Ticks, row: FocusRow, done: boolean, now: number): Ticks {
  const prev = ticks[row.key] ? { ...ticks[row.key], prev: null } : null;
  return {
    ...ticks,
    [row.key]: { done, row: { ...row, struck: false }, at: now, phase: "writing", prev },
  };
}

/**
 * Settle a tick write: success marks it `ok`, a failure restores `prev` (or
 * drops the tick when there was none) and returns the command's stderr line.
 */
export function settleTick(
  ticks: Ticks,
  key: string,
  res: HomeTickResponse,
): { ticks: Ticks; error: string | null } {
  const tick = ticks[key];
  if (res.ok) {
    if (!tick) return { ticks, error: null };
    return { ticks: { ...ticks, [key]: { ...tick, phase: "ok" } }, error: null };
  }
  const next = { ...ticks };
  if (tick?.prev) next[key] = tick.prev;
  else delete next[key];
  return { ticks: next, error: res.message ?? "write failed" };
}

/**
 * Drop only the settled (`ok`) ticks the live sources now confirm, so a stream
 * read never drops a tick still writing. A goal tick is confirmed when `goals`
 * holds its id at the ticked `done`; a reopened ticket tick when the report
 * lists the row open. A closed person-ticket tick is kept for the session;
 * while it lives it shadows the report's done row for that ticket, and after a
 * reload the report's row stands alone. A null `report` or `goals` keeps every
 * tick.
 */
export function reconcileTicks(
  ticks: Ticks,
  report: StatusReport | null,
  goals: FocusGoal[] | null,
): Ticks {
  if (report === null || goals === null) return ticks;
  const openKeys = new Set(reportRows(report).open.map((r) => r.key));
  const goalDone = new Map(goals.map((g) => [g.id, g.done]));
  const next: Ticks = {};
  for (const [key, tick] of Object.entries(ticks)) {
    if (tick.phase !== "ok") {
      next[key] = tick;
    } else if (tick.row.kind === "goal") {
      if (!(tick.row.goalId !== null && goalDone.get(tick.row.goalId) === tick.done)) {
        next[key] = tick;
      }
    } else if (tick.done || !openKeys.has(key)) {
      next[key] = tick;
    }
  }
  return next;
}

// ── Project filter (ticket 238): a searchable dropdown, kept in localStorage ──

/** The localStorage key the Focus tab's project filter lives under. */
export const FOCUS_PROJECT_KEY = "lf-focus-project";

/** Read the stored project filter; a missing value, `"all"` or a throw → null (All). */
export function loadFocusProject(storage: Pick<Storage, "getItem"> | null): string | null {
  try {
    const v = storage?.getItem(FOCUS_PROJECT_KEY);
    return !v || v === "all" ? null : v;
  } catch {
    return null;
  }
}

/** Persist the project filter: null writes `"all"`; a throwing storage is swallowed. */
export function saveFocusProject(
  storage: Pick<Storage, "setItem"> | null,
  p: string | null,
): void {
  try {
    storage?.setItem(FOCUS_PROJECT_KEY, p ?? "all");
  } catch {
    /* a failed write keeps the in-memory filter */
  }
}

/** One project-filter option: the All row, or a project with its open count. */
export interface ProjectOption {
  id: string | null;
  label: string;
  count: number;
}

/**
 * The dropdown options: All first, then each project with its open count (the
 * shell's `current` first, the rest alphabetical). A `selected` project stays
 * listed at count 0 when it has no rows. Only labels that include the trimmed,
 * lower-cased `query` survive.
 */
export function projectOptions(
  open: FocusRow[],
  current: string | null,
  selected: string | null,
  query: string,
): ProjectOption[] {
  const counts = new Map<string, number>();
  for (const row of open) {
    if (row.project === null) continue;
    counts.set(row.project, (counts.get(row.project) ?? 0) + 1);
  }
  if (selected !== null && !counts.has(selected)) counts.set(selected, 0);
  const ids = [...counts.keys()].sort((a, b) => {
    if (a === current) return -1;
    if (b === current) return 1;
    return a.localeCompare(b);
  });
  const candidates: ProjectOption[] = [
    { id: null, label: "All", count: open.length },
    ...ids.map((id) => ({ id, label: id, count: counts.get(id) ?? 0 })),
  ];
  const q = query.trim().toLowerCase();
  return candidates.filter((o) => o.label.toLowerCase().includes(q));
}

// ── Project picker highlight (ticket 243): an index into the visible options, -1 = none ──

/** The highlight when the picker opens: the current filter's row, else the first; -1 when empty. */
export function pickerOpenIndex(options: ProjectOption[], filter: string | null): number {
  if (!options.length) return -1;
  return Math.max(0, options.findIndex((o) => o.id === filter));
}

/** The highlight after the query changes: the first matching row, or -1 when nothing matches. */
export function pickerQueryIndex(options: ProjectOption[]): number {
  return options.length ? 0 : -1;
}

/**
 * One arrow-key step over `n` visible rows, wrapping at both ends. An off-list
 * highlight (-1, or past the end) enters at the near end: down → first, up → last.
 * No rows → -1.
 */
export function pickerMove(active: number, n: number, dir: 1 | -1): number {
  if (n <= 0) return -1;
  if (active < 0 || active >= n) return dir === 1 ? 0 : n - 1;
  return (active + dir + n) % n;
}

/** The option Enter picks: the highlighted row, or null when nothing is highlighted. */
export function pickerChoice(options: ProjectOption[], active: number): ProjectOption | null {
  return options[active] ?? null;
}

// ── Card (ticket 238): the row's summary and buttons ──

/** What the card's summary block shows: skeletons, text, or the failure line. */
export type SummaryView =
  | { state: "writing" }
  | { state: "text"; text: string }
  | { state: "failed" };

/** How a row's summary renders: pending → writing, a non-blank summary → text, else failed. */
export function summaryView(row: FocusRow): SummaryView {
  if (row.summaryState === "pending") return { state: "writing" };
  const text = row.summary?.trim();
  if (text && row.summaryState !== "failed") return { state: "text", text };
  return { state: "failed" };
}

/** A short label for a copyable action command (Resume, Remove, Deploy, else the command). */
export function actLabel(cmd: string): string {
  if (cmd.includes("--resume")) return "Resume";
  if (cmd.includes("runs rm")) return "Remove";
  if (cmd.startsWith("lime deploy")) return "Deploy";
  return cmd;
}

/** One card button: its route, label, copyable command and emphasis. */
export interface CardButton {
  kind: "open-ticket" | "command" | "mark-done" | "open-run";
  label: string;
  command: string | null;
  primary: boolean;
}

/**
 * The card's buttons: a goal's "Mark done" (when not done), else Open ticket
 * (every ticket-backed row, whatever its summary state) then one command per
 * action, then Open run. Ready rows and the first action are primary.
 */
export function cardButtons(row: FocusRow): CardButton[] {
  if (row.kind === "goal") {
    return row.done ? [] : [{ kind: "mark-done", label: "Mark done", command: null, primary: true }];
  }
  const buttons: CardButton[] = [];
  if (row.ticket) {
    buttons.push({ kind: "open-ticket", label: "Open ticket ›", command: null, primary: false });
  }
  row.actions.forEach((cmd, i) => {
    buttons.push({ kind: "command", label: actLabel(cmd), command: cmd, primary: row.kind === "ready" || i === 0 });
  });
  if (row.run_id) buttons.push({ kind: "open-run", label: "Open run", command: null, primary: false });
  return buttons;
}

/** A goal's focus.md line: `f3 · Ship · due 2026-02-01 · alpha` (due/project only when set). */
export function goalLine(row: FocusRow): string {
  let line = `${row.num} · ${row.title}`;
  if (row.due) line += ` · due ${row.due}`;
  if (row.project) line += ` · ${row.project}`;
  return line;
}

/** The note shown after a card command is copied. */
export function copiedLine(cmd: string, project: string | null): string {
  return project === null ? `Copied: ${cmd}` : `Copied — run in ${project}: ${cmd}`;
}

// ── Keys (ticket 238): card and picker state, and keyboard row walking ──

/** The Focus tab's key layer: the open card's row key, and whether the picker is open. */
export interface FocusUi {
  open: string | null;
  picker: boolean;
}

/** Esc closes the picker first, then the card, else reports nothing to close. */
export function focusEscape(ui: FocusUi): {
  ui: FocusUi;
  closed: "picker" | "card" | null;
} {
  if (ui.picker) return { ui: { ...ui, picker: false }, closed: "picker" };
  if (ui.open !== null) return { ui: { ...ui, open: null }, closed: "card" };
  return { ui, closed: null };
}

/**
 * Move the keyboard focus one row down (`dir` 1) or up (-1) among the focus
 * rows' hit buttons and the done toggle, skipping anything inside `[inert]`. An
 * active checkbox maps to its row's hit button. The step is clamped at both
 * ends; an active element outside the list returns null.
 */
export function rowStep(
  root: ParentNode,
  active: Element | null,
  dir: 1 | -1,
): HTMLElement | null {
  const buttons = Array.from(
    root.querySelectorAll<HTMLElement>("button.f-hit, button.fdone"),
  ).filter((el) => !el.closest("[inert]"));
  let target = active as HTMLElement | null;
  if (target?.classList.contains("cbox")) {
    target = target.closest(".frow-main")?.querySelector<HTMLElement>(".f-hit") ?? null;
  }
  const i = target ? buttons.indexOf(target) : -1;
  if (i === -1) return null;
  return buttons[Math.min(Math.max(i + dir, 0), buttons.length - 1)];
}

/**
 * The ticket an openable report row points at, or null when the row is a goal,
 * has no ticket, or names a project the shell does not have registered. The
 * caller writes `project` from this target alone, so a stray row can never
 * switch the shell to an unregistered project.
 */
export function ticketTarget(
  projects: string[],
  row: FocusRow,
): { project: string; path: string } | null {
  if (row.kind === "goal" || !row.ticket || row.project === null) return null;
  if (!projects.includes(row.project)) return null;
  return { project: row.project, path: `.lime/tickets/${row.ticket}.md` };
}

/** The unpushed-commits warning line, or null when nothing is ahead. */
export function pushLine(home: HomeBlock | null): string | null {
  if (!home || home.ahead <= 0) return null;
  let line = `⚠ dashboard: ${home.ahead} commits not pushed`;
  if (home.push_error) line += ` · ${home.push_error}`;
  return line;
}

/** The Done toggle's label, or null at zero. */
export function doneLabel(n: number): string | null {
  return n === 0 ? null : `✓ ${n} done since your last visit`;
}

/** The two home fetches the Focus tab drives; a fake stands in for tests. */
export interface HomeApi {
  visit(): Promise<HomeVisitResponse>;
  focus(): Promise<HomeFocusResponse>;
}

/** The Focus tab's loaded state — never throws; failures land in `errors`. */
export interface HomeState {
  report: StatusReport | null;
  goals: FocusGoal[] | null;
  errors: string[];
  initLine: string | null;
}

/**
 * Load the home: visit first (so Done reads the fresh previous stamp), then the
 * focus report. Every failure becomes an error line or the init line; this
 * never throws, and a visit failure still lets focus load.
 */
export async function loadHome(api: HomeApi): Promise<HomeState> {
  const state: HomeState = { report: null, goals: null, errors: [], initLine: null };
  try {
    const visit = await api.visit();
    if (visit.home_missing) state.initLine = INIT_LINE;
    else if (!visit.ok) state.errors.push(visit.message ?? "lime dash visit failed");
  } catch (e) {
    state.errors.push("lime dash visit: " + (e instanceof Error ? e.message : String(e)));
  }
  try {
    const focus = await api.focus();
    state.report = focus.report;
    state.goals = focus.goals;
    state.errors.push(...focus.errors);
  } catch (e) {
    state.errors.push(e instanceof Error ? e.message : String(e));
  }
  return state;
}

/**
 * Merge a live-feed focus frame into the loaded state. A null `report`
 * or `goals` (a failed read) keeps the last good value; `errors` is always
 * replaced and `initLine` is kept.
 */
export function applyFocus(state: HomeState, res: HomeFocusResponse): HomeState {
  return {
    report: res.report ?? state.report,
    goals: res.goals ?? state.goals,
    errors: res.errors,
    initLine: state.initLine,
  };
}

/** The error lines joined for one display line, or null when there are none. */
export function errorLine(errors: string[]): string | null {
  return errors.length ? errors.join(" · ") : null;
}
