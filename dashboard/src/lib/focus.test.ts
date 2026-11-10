import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { JSDOM } from "jsdom";
import {
  actLabel,
  applyFocus,
  cardButtons,
  copiedLine,
  doneLabel,
  errorLine,
  FOCUS_PROJECT_KEY,
  focusEscape,
  focusList,
  goalLine,
  goalRows,
  INIT_LINE,
  KIND_LABEL,
  loadFocusProject,
  loadHome,
  pickerChoice,
  pickerMove,
  pickerOpenIndex,
  pickerQueryIndex,
  projectOptions,
  pushLine,
  reconcileTicks,
  reasonLabel,
  reportRows,
  rowStep,
  saveFocusProject,
  settleTick,
  startTick,
  summaryView,
  tickRequest,
  ticketNum,
  ticketTarget,
  todoRows,
  type HomeApi,
  type HomeState,
} from "./focus";
import type {
  FocusGoal,
  HomeFocusResponse,
  HomeVisitResponse,
  ReportItem,
  StatusReport,
} from "./types";
import type { DoneReason } from "../../shared/types";

function item(over: Partial<ReportItem> = {}): ReportItem {
  return {
    group: "failed", reason: "fail", project: null, ticket: null, title: null,
    run_id: null, branch: null, since: null, text: null, cause: null, count: null,
    kind: null, focus: null, actions: [], pr: null, note: null,
    summary: null, summary_state: null, ...over,
  };
}

function report(items: ReportItem[]): StatusReport {
  return {
    generated_at: "2025-01-01T00:00:00Z",
    items,
    home: { ahead: 0, push_error: null, last_visit: null },
    notes: [],
  };
}

function goal(over: Partial<FocusGoal> = {}): FocusGoal {
  return {
    id: "f1", title: "t", due: null, project: null, done: false,
    group: "today", overdue: false, ...over,
  };
}

describe("focusList", () => {
  const goals = [
    goal({ id: "f1", title: "Alpha one", project: "alpha" }),
    goal({ id: "f2", title: "Global two", project: null }),
    goal({ id: "f3", title: "Beta three", project: "beta" }),
    goal({ id: "f4", title: "Alpha done", project: "alpha", done: true, group: "completed" }),
  ];
  const items = [
    item({ group: "failed", reason: "fail", project: "alpha", ticket: "227-a", since: "2025-03-02T00:00:00Z", title: "Newer fail" }),
    item({ group: "needs_you", reason: "ticket", project: "beta", ticket: "228-b" }),
    item({ group: "failed", reason: "fail", project: "alpha", ticket: "229-b", since: "2025-03-01T00:00:00Z", title: "Older fail" }),
    item({ group: "ready", reason: "ready", project: "beta", ticket: "230-b" }),
    item({ group: "done", reason: "merged", project: "alpha", ticket: "231-a" }),
    item({ group: "needs_you", reason: "adr_proposed", project: "alpha", count: 1 }),
    item({ group: "needs_you", reason: "not_launched", project: "beta", count: 3 }),
  ];

  test("splits open project first and orders each partition oldest-first", () => {
    const list = focusList(todoRows(report(items), goals, {}), "alpha", null);
    expect(list.here.map((r) => r.key)).toEqual([
      "goal:f1",
      "goal:f2",
      "failed:alpha:229-b",
      "failed:alpha:227-a",
    ]);
    expect(list.here[2].title).toBe("Older fail");
    expect(list.elsewhere.map((r) => r.key)).toEqual([
      "goal:f3",
      "needs_you:beta:228-b",
      "ready:beta:230-b",
    ]);
    expect(list.done.map((r) => r.key)).toEqual(["goal:f4", "done:alpha:231-a"]);
    // f4 (done) now sits in the footer, not the open rows.
    expect([...list.here, ...list.elsewhere].some((r) => r.goalId === "f4")).toBe(false);
  });

  test("orders report rows by age, not lime's group rank", () => {
    const mixed = report([
      item({ group: "needs_you", reason: "ticket", project: "alpha", ticket: "300-new", since: "2025-03-03T00:00:00Z" }),
      item({ group: "failed", reason: "fail", project: "alpha", ticket: "301-old", since: "2025-03-01T00:00:00Z" }),
    ]);
    expect(focusList(todoRows(mixed, [], {}), "alpha", null).here.map((r) => r.key)).toEqual([
      "failed:alpha:301-old",
      "needs_you:alpha:300-new",
    ]);
  });

  test("null project puts every row above the divider", () => {
    const list = focusList(todoRows(report(items), goals, {}), null, null);
    expect(list.elsewhere).toEqual([]);
    expect(list.here.map((r) => r.key)).toEqual([
      "goal:f1", "goal:f2", "goal:f3",
      "failed:alpha:229-b", "failed:alpha:227-a",
      "needs_you:beta:228-b", "ready:beta:230-b",
    ]);
  });

  test("a null report or null goals still lists the other half", () => {
    const onlyGoals = focusList(todoRows(null, goals, {}), "alpha", null);
    expect(onlyGoals.here.map((r) => r.goalId)).toEqual(["f1", "f2"]);
    const onlyReport = focusList(todoRows(report(items), null, {}), "alpha", null);
    expect(onlyReport.here.map((r) => r.key)).toEqual([
      "failed:alpha:229-b", "failed:alpha:227-a",
    ]);
    expect(onlyReport.elsewhere.map((r) => r.key)).toEqual([
      "needs_you:beta:228-b", "ready:beta:230-b",
    ]);
  });
});

describe("reportRows", () => {
  test("detail joins the reason and the first text line", () => {
    const { open } = reportRows(report([
      item({ reason: "ci_red", text: "line one\nline two" }),
    ]));
    expect(open[0].detail).toBe("CI red · line one");
  });
});

describe("done footer from the report (244)", () => {
  test("a closed person-ticket from the report shows in the footer after a reload", () => {
    const rows = todoRows(
      report([
        item({ group: "done", reason: "closed", project: "alpha", ticket: "244-a", kind: "task" }),
      ]),
      [],
      {},
    );
    expect(rows.open).toEqual([]);
    expect(rows.done.map((r) => r.key)).toEqual(["done:alpha:244-a"]);
    expect(rows.done[0].detail).toBe("closed");
    expect(rows.done[0].checkable).toBe(false);
    expect(focusList(rows, "alpha", null).done.length).toBe(1);
    expect(doneLabel(1)).toBe("✓ 1 done since your last visit");
  });

  test("a ticked person-ticket shows exactly once before and after the report lists it done", () => {
    const openReport = report([
      item({ group: "needs_you", reason: "ticket", project: "alpha", ticket: "244-a" }),
    ]);
    const row = reportRows(openReport).open[0];
    const writing = startTick({}, row, true, 10);
    const closedReport = report([
      item({ group: "done", reason: "closed", project: "alpha", ticket: "244-a" }),
    ]);
    const count = (out: ReturnType<typeof todoRows>) =>
      [...out.open, ...out.done].filter((r) => r.ticket === "244-a").length;

    const before = todoRows(openReport, [], writing);
    expect(before.done.map((r) => r.key)).toEqual([row.key]);
    expect(count(before)).toBe(1);

    const during = todoRows(closedReport, [], writing);
    expect(during.done.map((r) => r.key)).toEqual([row.key]);
    expect(count(during)).toBe(1);

    const held = todoRows(closedReport, [], writing, [row.key]);
    expect(held.done).toEqual([]);
    expect(held.open.map((r) => r.key)).toEqual([row.key]);
    expect(held.open[0].struck).toBe(true);
    expect(count(held)).toBe(1);

    const ok = settleTick(writing, row.key, { ok: true, message: null }).ticks;
    const kept = reconcileTicks(ok, closedReport, []);
    const settled = todoRows(closedReport, [], kept);
    expect(settled.done.map((r) => r.key)).toEqual([row.key]);
    expect(count(settled)).toBe(1);
    expect(settled.done[0].checkable).toBe(true);

    const reloaded = todoRows(closedReport, [], {});
    expect(reloaded.done.map((r) => r.key)).toEqual(["done:alpha:244-a"]);

    const untick = startTick(kept, { ...row, done: true }, false, 20);
    const un = todoRows(closedReport, [], untick);
    expect(un.done).toEqual([]);
    expect(un.open.map((r) => r.key)).toEqual([row.key]);
    expect(count(un)).toBe(1);
  });

  test("every done reason lime status emits reaches the footer, and so does an unknown one", () => {
    const emitted: Record<DoneReason, true> = { deployed: true, merged: true, closed: true };
    for (const reason of [...Object.keys(emitted), "brand_new_reason"]) {
      const r = report([
        item({
          group: "done",
          reason,
          project: "alpha",
          ticket: "9-a",
          since: "2026-11-05T10:00:00+00:00",
        }),
      ]);
      const list = focusList(todoRows(r, [], {}), "alpha", null);
      expect(list.here).toEqual([]);
      expect(list.done.map((x) => x.detail)).toEqual([reasonLabel(reason)]);
    }
    expect(reasonLabel("closed")).toBe("closed");
    expect(reasonLabel("brand_new_reason")).toBe("brand new reason");
  });
});

describe("checkable rows", () => {
  test("only goals and person-tickets are checkable", () => {
    const split = reportRows(report([
      item({ group: "needs_you", reason: "ticket", project: "alpha", ticket: "1-a" }),
      item({ group: "failed", reason: "fail", project: "alpha" }),
      item({ group: "ready", reason: "ready", project: "alpha" }),
      item({ group: "needs_you", reason: "breach", project: "alpha" }),
    ]));
    const byReason = Object.fromEntries(split.open.map((r) => [r.reason, r.checkable]));
    expect(byReason.ticket).toBe(true);
    expect(byReason.fail).toBe(false);
    expect(byReason.ready).toBe(false);
    expect(byReason.breach).toBe(false);
    const doneRow = reportRows(
      report([item({ group: "done", reason: "merged", project: "alpha", ticket: "2-a" })]),
    ).done[0];
    expect(doneRow.checkable).toBe(false);
    expect(goalRows([goal()])[0].checkable).toBe(true);
  });

  test("no count row shows", () => {
    const counts = report([
      item({ group: "needs_you", reason: "adr_proposed", project: "alpha", count: 1 }),
      item({ group: "needs_you", reason: "not_launched", project: "beta", count: 3 }),
      item({ group: "failed", reason: "fail", project: "alpha", ticket: "9-a" }),
    ]);
    expect(reportRows(counts).open.map((r) => r.key)).toEqual(["failed:alpha:9-a"]);
    const list = focusList(todoRows(counts, [], {}), "alpha", null);
    expect("countsLine" in list).toBe(false);
  });
});

describe("ticks", () => {
  test("a tick moves the row to the done footer, a failed write unticks it and returns the stderr line", () => {
    const goals = [goal({ id: "f1", project: "alpha" })];
    const row = goalRows(goals)[0];
    const ticks = startTick({}, row, true, 1000);
    const ticked = todoRows(null, goals, ticks);
    expect(ticked.open).toEqual([]);
    expect(ticked.done.map((r) => r.key)).toEqual(["goal:f1"]);

    const res = settleTick(ticks, "goal:f1", { ok: false, message: "lime focus: no row f1" });
    expect(res.error).toBe("lime focus: no row f1");
    const after = todoRows(null, goals, res.ticks);
    expect(after.open.map((r) => r.key)).toEqual(["goal:f1"]);
    expect(after.open[0].done).toBe(false);
  });

  test("a held tick stays struck in place", () => {
    const goals = [goal({ id: "f1", project: "alpha" })];
    const row = goalRows(goals)[0];
    const ticks = startTick({}, row, true, 5);
    const out = todoRows(null, goals, ticks, ["goal:f1"]);
    expect(out.done).toEqual([]);
    expect(out.open.map((r) => r.key)).toEqual(["goal:f1"]);
    expect(out.open[0].struck).toBe(true);
    expect(out.open[0].done).toBe(true);
  });

  test("a closed person-ticket stays in the footer after the report drops it", () => {
    const first = report([
      item({ group: "needs_you", reason: "ticket", project: "alpha", ticket: "227-a" }),
    ]);
    const row = reportRows(first).open[0];
    const ticks = settleTick(startTick({}, row, true, 10), row.key, { ok: true, message: null }).ticks;
    const after = todoRows(report([]), [], ticks);
    expect(after.open).toEqual([]);
    expect(after.done.map((r) => r.key)).toEqual(["needs_you:alpha:227-a"]);
  });

  test("a failed untick puts it back checked", () => {
    const first = report([
      item({ group: "needs_you", reason: "ticket", project: "alpha", ticket: "227-a" }),
    ]);
    const row = reportRows(first).open[0];
    const closed = {
      [row.key]: { done: true, row, at: 5, phase: "ok" as const, prev: null },
    };
    const inFlight = startTick(closed, { ...row, done: true }, false, 20);
    const res = settleTick(inFlight, row.key, { ok: false, message: "lime ticket: nope" });
    expect(res.error).toBe("lime ticket: nope");
    expect(todoRows(report([]), [], res.ticks).done.map((r) => r.key)).toEqual([row.key]);
  });

  test("reconcile drops confirmed ticks only", () => {
    const open = goalRows([goal({ id: "f1", project: "alpha" })])[0];
    const confirmed = [goal({ id: "f1", project: "alpha", done: true })];
    const done = settleTick(startTick({}, open, true, 1), "goal:f1", { ok: true, message: null }).ticks;
    expect(Object.keys(reconcileTicks(done, report([]), confirmed))).toEqual([]);

    const writing = startTick({}, open, true, 1);
    expect(Object.keys(reconcileTicks(writing, report([]), confirmed))).toEqual(["goal:f1"]);

    const trow = reportRows(
      report([item({ group: "needs_you", reason: "ticket", project: "alpha", ticket: "9-a" })]),
    ).open[0];
    const closed = settleTick(startTick({}, trow, true, 1), trow.key, { ok: true, message: null }).ticks;
    expect(Object.keys(reconcileTicks(closed, report([]), []))).toEqual([trow.key]);

    const reopened = settleTick(startTick({}, trow, false, 1), trow.key, { ok: true, message: null }).ticks;
    const listed = report([item({ group: "needs_you", reason: "ticket", project: "alpha", ticket: "9-a" })]);
    expect(Object.keys(reconcileTicks(reopened, listed, []))).toEqual([]);
  });

  test("tickRequest maps goal, person-ticket, and nothing else", () => {
    const g = goalRows([goal({ id: "f3", project: "alpha" })])[0];
    expect(tickRequest(g, true)).toEqual({ kind: "goal", id: "f3", done: true });
    const gd = goalRows([goal({ id: "f1", project: "alpha", done: true })])[0];
    expect(tickRequest(gd, false)).toEqual({ kind: "goal", id: "f1", done: false });
    const person = reportRows(
      report([item({ group: "needs_you", reason: "ticket", project: "alpha", ticket: "227-a" })]),
    ).open[0];
    expect(tickRequest(person, true)).toEqual({ kind: "ticket", project: "alpha", number: "227", done: true });
    const failed = reportRows(
      report([item({ group: "failed", reason: "fail", project: "alpha", ticket: "229-b" })]),
    ).open[0];
    expect(tickRequest(failed, true)).toBe(null);
    const noProject = reportRows(
      report([item({ group: "needs_you", reason: "ticket", project: null, ticket: "1-a" })]),
    ).open[0];
    expect(tickRequest(noProject, true)).toBe(null);
  });
});

describe("project filter", () => {
  const goals = [
    goal({ id: "f1", project: "alpha" }),
    goal({ id: "f2", project: null }),
    goal({ id: "f3", project: "beta" }),
    goal({ id: "f4", project: "alpha", done: true, group: "completed" }),
  ];
  const items = [
    item({ group: "failed", reason: "fail", project: "alpha", ticket: "227-a", since: "2025-03-02T00:00:00Z" }),
    item({ group: "needs_you", reason: "ticket", project: "beta", ticket: "228-b" }),
    item({ group: "failed", reason: "fail", project: "alpha", ticket: "229-b", since: "2025-03-01T00:00:00Z" }),
    item({ group: "done", reason: "merged", project: "alpha", ticket: "231-a" }),
  ];

  test("the project filter narrows the list and the done footer", () => {
    const list = focusList(todoRows(report(items), goals, {}), "alpha", "alpha");
    expect(list.elsewhere).toEqual([]);
    expect([...list.here, ...list.done].every((r) => r.project === "alpha")).toBe(true);
    expect([...list.here, ...list.done].some((r) => r.project === null)).toBe(false);
    expect(list.done.map((r) => r.key)).toEqual(["goal:f4", "done:alpha:231-a"]);
  });

  test("projectOptions: All count, current first, selected kept at 0, query filter", () => {
    const open = todoRows(report(items), goals, {}).open;
    const opts = projectOptions(open, "beta", "gamma", "");
    expect(opts[0]).toEqual({ id: null, label: "All", count: open.length });
    expect(opts.map((o) => o.id)).toEqual([null, "beta", "alpha", "gamma"]);
    expect(opts.find((o) => o.id === "gamma")?.count).toBe(0);
    expect(projectOptions(open, "beta", null, "alp").map((o) => o.label)).toEqual(["alpha"]);
  });

  test("load/saveFocusProject use lf-focus-project", () => {
    const store = new Map<string, string>();
    const storage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => { store.set(k, v); },
    };
    expect(loadFocusProject(storage)).toBe(null);
    saveFocusProject(storage, "alpha");
    expect(store.get(FOCUS_PROJECT_KEY)).toBe("alpha");
    expect(loadFocusProject(storage)).toBe("alpha");
    saveFocusProject(storage, null);
    expect(store.get(FOCUS_PROJECT_KEY)).toBe("all");
    expect(loadFocusProject(storage)).toBe(null);
    store.set(FOCUS_PROJECT_KEY, "all");
    expect(loadFocusProject(storage)).toBe(null);
    expect(loadFocusProject({ getItem() { throw new Error("nope"); } })).toBe(null);
    expect(() => saveFocusProject({ setItem() { throw new Error("nope"); } }, "x")).not.toThrow();
  });

  test("a shrinking query moves the highlight onto the narrowed list and Enter picks it", () => {
    const open = todoRows(report(items), goals, {}).open;
    let opts = projectOptions(open, "beta", "alpha", "");
    expect(opts.map((o) => o.id)).toEqual([null, "beta", "alpha"]);
    let active = pickerOpenIndex(opts, "alpha");
    expect(active).toBe(2);

    opts = projectOptions(open, "beta", "alpha", "b");
    expect(opts.map((o) => o.id)).toEqual(["beta"]);
    expect(pickerChoice(opts, active)).toBe(null);
    active = pickerQueryIndex(opts);
    expect(active).toBe(0);
    expect(pickerChoice(opts, active)?.id).toBe("beta");

    for (const q of ["be", "bet"]) {
      opts = projectOptions(open, "beta", "alpha", q);
      active = pickerQueryIndex(opts);
      expect(pickerChoice(opts, active)?.id).toBe("beta");
    }

    opts = projectOptions(open, "beta", "alpha", "betz");
    expect(opts).toEqual([]);
    active = pickerQueryIndex(opts);
    expect(active).toBe(-1);
    expect(pickerChoice(opts, active)).toBe(null);
    expect(pickerMove(active, 0, 1)).toBe(-1);
    expect(pickerMove(active, 0, -1)).toBe(-1);

    opts = projectOptions(open, "beta", "alpha", "");
    active = pickerQueryIndex(opts);
    expect(pickerChoice(opts, active)?.id).toBe(null);
    expect(active).toBe(0);
  });

  test("arrow keys wrap within the narrowed list; an off-list highlight enters at the near end", () => {
    const open = todoRows(report(items), goals, {}).open;
    const opts = projectOptions(open, "beta", null, "al");
    expect(opts.map((o) => o.label)).toEqual(["All", "alpha"]);
    const n = opts.length;

    expect(pickerMove(0, n, 1)).toBe(1);
    expect(pickerMove(1, n, 1)).toBe(0);
    expect(pickerMove(0, n, -1)).toBe(1);
    expect(pickerMove(1, n, -1)).toBe(0);

    expect(pickerMove(-1, n, 1)).toBe(0);
    expect(pickerMove(-1, n, -1)).toBe(1);
    expect(pickerMove(5, n, 1)).toBe(0);
    expect(pickerMove(5, n, -1)).toBe(1);

    expect(pickerOpenIndex([], null)).toBe(-1);
    expect(pickerOpenIndex(opts, "zeta")).toBe(0);
  });
});

describe("card", () => {
  const person = () => reportRows(
    report([item({ group: "needs_you", reason: "ticket", project: "alpha", ticket: "227-a" })]),
  ).open[0];

  test("summaryView", () => {
    const pending = reportRows(report([item({ summary: "hi", summary_state: "pending" })])).open[0];
    expect(summaryView(pending)).toEqual({ state: "writing" });
    const ok = reportRows(report([item({ summary: " Ship it ", summary_state: "ok" })])).open[0];
    expect(summaryView(ok)).toEqual({ state: "text", text: "Ship it" });
    const failed = reportRows(report([item({ summary: "old", summary_state: "failed" })])).open[0];
    expect(summaryView(failed)).toEqual({ state: "failed" });
    const none = reportRows(report([item({ summary: null, summary_state: null })])).open[0];
    expect(summaryView(none)).toEqual({ state: "failed" });
  });

  test("cardButtons", () => {
    const g = goalRows([goal({ id: "f3", project: "alpha" })])[0];
    expect(cardButtons(g).map((b) => b.label)).toEqual(["Mark done"]);
    expect(cardButtons(g)[0].kind).toBe("mark-done");
    const gd = goalRows([goal({ id: "f1", project: "alpha", done: true })])[0];
    expect(cardButtons(gd)).toEqual([]);

    const p = person();
    expect(cardButtons(p).map((b) => b.kind)).toEqual(["open-ticket"]);
    expect(cardButtons(p)[0].label).toBe("Open ticket ›");

    const f = reportRows(report([item({
      group: "failed", reason: "fail", project: "alpha", ticket: "229-b",
      summary: "do x", summary_state: "ok", run_id: "r1",
      actions: ["lime run --resume r1", "lime runs rm r1"],
    })])).open[0];
    const btns = cardButtons(f);
    expect(btns.map((b) => b.label)).toEqual(["Open ticket ›", "Resume", "Remove", "Open run"]);
    expect(btns[0].kind).toBe("open-ticket");
    expect(btns[0].primary).toBe(false);
    expect(btns[1].primary).toBe(true);
    expect(btns[2].primary).toBe(false);

    const ff = reportRows(report([item({
      group: "failed", reason: "fail", project: "alpha", ticket: "229-b",
      summary: null, summary_state: null, actions: ["lime run --resume r2"],
    })])).open[0];
    const b2 = cardButtons(ff);
    expect(b2[0].kind).toBe("open-ticket");
    expect(b2[1].label).toBe("Resume");

    const rd = reportRows(report([item({
      group: "ready", reason: "ready", project: "alpha", ticket: "230-b",
      summary: "Ready to ship", summary_state: "ok", actions: ["lime deploy alpha 238"],
    })])).open[0];
    const b3 = cardButtons(rd);
    expect(b3.map((b) => b.label)).toEqual(["Open ticket ›", "Deploy"]);
    expect(b3[0].kind).toBe("open-ticket");
    expect(b3[1].label).toBe("Deploy");
    expect(b3[1].primary).toBe(true);

    const pend = reportRows(report([item({
      group: "failed", reason: "fail", project: "alpha", ticket: "231-b",
      summary_state: "pending", actions: ["lime run --resume r3"],
    })])).open[0];
    expect(cardButtons(pend)[0].kind).toBe("open-ticket");
  });

  test("actLabel", () => {
    expect(actLabel("lime run --resume r1")).toBe("Resume");
    expect(actLabel("lime runs rm r1")).toBe("Remove");
    expect(actLabel("lime deploy alpha 238")).toBe("Deploy");
    expect(actLabel("lime something else")).toBe("lime something else");
  });

  test("goalLine", () => {
    const g = goalRows([goal({ id: "f3", title: "Ship", due: "2026-02-01", project: "alpha" })])[0];
    expect(goalLine(g)).toBe("f3 · Ship · due 2026-02-01 · alpha");
    const bare = goalRows([goal({ id: "f9", title: "Bare" })])[0];
    expect(goalLine(bare)).toBe("f9 · Bare");
  });

  test("copiedLine", () => {
    expect(copiedLine("lime deploy alpha 1", "alpha")).toBe("Copied — run in alpha: lime deploy alpha 1");
    expect(copiedLine("lime run --resume r1", null)).toBe("Copied: lime run --resume r1");
  });
});

describe("keys", () => {
  test("focusEscape closes the picker, then the card, then nothing", () => {
    expect(focusEscape({ open: "goal:f1", picker: true }))
      .toEqual({ ui: { open: "goal:f1", picker: false }, closed: "picker" });
    expect(focusEscape({ open: "goal:f1", picker: false }))
      .toEqual({ ui: { open: null, picker: false }, closed: "card" });
    expect(focusEscape({ open: null, picker: false }).closed).toBe(null);
  });

  test("rowStep walks rows with the keyboard", () => {
    const dom = new JSDOM(`<ul>
      <li class="frow"><div class="frow-main"><button class="cbox"></button><button class="f-hit" id="h1"></button></div><div class="fcard" inert><button class="f-hit" id="hi"></button></div></li>
      <li class="frow"><div class="frow-main"><button class="cbox"></button><button class="f-hit" id="h2"></button></div><div class="fcard"><button></button></div></li>
      <li class="frow"><div class="frow-main"><button class="cbox"></button><button class="f-hit" id="h3"></button></div><div class="fcard" inert><button class="f-hit" id="hi3"></button></div></li>
    </ul><button class="fdone" id="fd"></button>`);
    const root = dom.window.document;
    const h1 = root.getElementById("h1")!;
    const h2 = root.getElementById("h2")!;
    const h3 = root.getElementById("h3")!;
    const hi = root.getElementById("hi")!;
    const fd = root.getElementById("fd")!;
    const cbox1 = root.querySelector(".cbox")!;
    expect(rowStep(root, h1, 1)).toBe(h2);
    expect(rowStep(root, cbox1, 1)).toBe(h2);
    expect(rowStep(root, h1, -1)).toBe(h1);
    expect(rowStep(root, h2, -1)).toBe(h1);
    expect(rowStep(root, h3, 1)).toBe(fd);
    expect(rowStep(root, hi, 1)).toBe(null);
    expect(rowStep(root, fd, 1)).toBe(fd);
  });
});

describe("pushLine", () => {
  test("null when nothing is ahead", () => {
    expect(pushLine({ ahead: 0, push_error: null, last_visit: null })).toBe(null);
    expect(pushLine(null)).toBe(null);
  });
  test("names the commits and appends the push error", () => {
    expect(pushLine({ ahead: 2, push_error: "auth failed", last_visit: null }))
      .toBe("⚠ dashboard: 2 commits not pushed · auth failed");
    expect(pushLine({ ahead: 1, push_error: null, last_visit: null }))
      .toBe("⚠ dashboard: 1 commits not pushed");
  });
});

describe("doneLabel", () => {
  test("null at zero, a count otherwise", () => {
    expect(doneLabel(0)).toBe(null);
    expect(doneLabel(3)).toBe("✓ 3 done since your last visit");
  });
});

describe("constants", () => {
  test("init line and kind labels", () => {
    expect(INIT_LINE).toContain("run lime dash init");
    expect(KIND_LABEL.goal).toBe("Goal");
    expect(KIND_LABEL.needs_you).toBe("Needs you");
    expect(KIND_LABEL.failed).toBe("Failed");
    expect(KIND_LABEL.ready).toBe("Ready to deploy");
    expect(KIND_LABEL.done).toBe("Done");
  });
});

describe("ticketNum and reasonLabel", () => {
  test("known and unknown values", () => {
    expect(ticketNum("227-focus-tab")).toBe("227");
    expect(ticketNum("no-digits")).toBe("");
    expect(ticketNum(null)).toBe("");
    expect(reasonLabel("fail")).toBe("failed");
    expect(reasonLabel("budget_exhausted")).toBe("budget exhausted");
    expect(reasonLabel("stale_running")).toBe("stopped mid-run");
    expect(reasonLabel("ci_red")).toBe("CI red");
    expect(reasonLabel("adr_proposed")).toBe("proposed ADR");
    expect(reasonLabel("not_launched")).toBe("not launched");
    expect(reasonLabel("some_unknown_reason")).toBe("some unknown reason");
  });
});

describe("loadHome", () => {
  function api(order: string[], impl: {
    visit: () => Promise<HomeVisitResponse>;
    focus: () => Promise<HomeFocusResponse>;
  }): HomeApi {
    return {
      visit: async () => { order.push("visit"); return impl.visit(); },
      focus: async () => { order.push("focus"); return impl.focus(); },
    };
  }

  test("calls visit once, before focus", async () => {
    const order: string[] = [];
    await loadHome(api(order, {
      visit: async () => ({ ok: true, home_missing: false, message: null }),
      focus: async () => ({ report: null, goals: [], errors: [] }),
    }));
    expect(order).toEqual(["visit", "focus"]);
  });

  test("home_missing gives the init line, not an error", async () => {
    const state: HomeState = await loadHome(api([], {
      visit: async () => ({ ok: false, home_missing: true, message: "dashboard: no home repo" }),
      focus: async () => ({ report: null, goals: [], errors: [] }),
    }));
    expect(state.initLine).toContain("run lime dash init");
    expect(state.errors).toEqual([]);
  });

  test("a failed visit is one error and focus still loads", async () => {
    const state = await loadHome(api([], {
      visit: async () => ({ ok: false, home_missing: false, message: "dash visit blew up" }),
      focus: async () => ({
        report: report([item({ ticket: "1-x", title: "thing" })]),
        goals: [goal()],
        errors: [],
      }),
    }));
    expect(state.errors).toEqual(["dash visit blew up"]);
    expect(state.report?.items.length).toBe(1);
    expect(state.goals?.length).toBe(1);
  });

  test("a thrown visit is one error and focus still loads", async () => {
    const state = await loadHome(api([], {
      visit: async () => { throw new Error("network down"); },
      focus: async () => ({ report: null, goals: [goal()], errors: [] }),
    }));
    expect(state.errors).toEqual(["lime dash visit: network down"]);
    expect(state.goals?.length).toBe(1);
  });

  test("a thrown focus fetch leaves report and goals null with one error", async () => {
    const state = await loadHome(api([], {
      visit: async () => ({ ok: true, home_missing: false, message: null }),
      focus: async () => { throw new Error("focus route 500"); },
    }));
    expect(state.report).toBe(null);
    expect(state.goals).toBe(null);
    expect(state.errors).toEqual(["focus route 500"]);
  });
});

describe("applyFocus", () => {
  test("a failed read keeps the last report and goals and shows the new errors", () => {
    const prev: HomeState = {
      report: report([item({ ticket: "1-x" })]),
      goals: [goal()],
      errors: ["old"],
      initLine: "init",
    };
    const next = applyFocus(prev, { report: null, goals: null, errors: ["boom"] });
    expect(next.report).toBe(prev.report);
    expect(next.goals).toBe(prev.goals);
    expect(next.errors).toEqual(["boom"]);
    expect(next.initLine).toBe("init");
  });

  test("a good read replaces them and keeps initLine", () => {
    const prev: HomeState = { report: null, goals: null, errors: [], initLine: "init" };
    const fresh = report([item({ ticket: "2-y" })]);
    const goals = [goal({ id: "f9" })];
    const next = applyFocus(prev, { report: fresh, goals, errors: [] });
    expect(next.report).toBe(fresh);
    expect(next.goals).toBe(goals);
    expect(next.initLine).toBe("init");
  });
});

describe("FocusTab wiring", () => {
  const source = readFileSync(
    join(import.meta.dir, "../components/FocusTab.vue"),
    "utf8",
  );

  test("loads once, then follows the live feed's focus and stops on unmount", () => {
    expect(source).toContain("live.focus");
    expect(source).toContain("applyFocus(state.value");
    expect(source).toContain("onUnmounted");
    expect(source).not.toContain("followHome");
    expect(source).not.toContain("openHomeStream");
    expect(source.match(/loadHome\(/g)?.length ?? 0).toBe(1);
  });

  test("wires the tick write, the todo rules, the filter and the keyboard", () => {
    for (const s of [
      "postHomeTick(",
      "todoRows(",
      "loadFocusProject(",
      "saveFocusProject(",
      "defineExpose({ escape })",
      "rowStep(",
      "setQuery(",
      "pickerOpenIndex(",
      "pickerQueryIndex(",
      "pickerMove(",
      "pickerChoice(",
    ]) {
      expect(source).toContain(s);
    }
    for (const s of ["hcard", "openFocusMd", "countsLine", "fcounts"]) {
      expect(source).not.toContain(s);
    }
    expect(source).not.toContain('@input="query =');
    expect(source).toContain('@mousemove="active = i"');
  });

  test("the keyboard reaches every row and button", () => {
    let i = source.indexOf("@click=");
    let onButtons = 0;
    while (i !== -1) {
      const before = source.slice(0, i);
      const tag = before.slice(before.lastIndexOf("<"));
      if (tag.startsWith("<button")) {
        onButtons++;
      } else {
        // the one exception: the listbox option driven by the input's keys
        expect(source.startsWith('@click="pick(', i)).toBe(true);
      }
      i = source.indexOf("@click=", i + 1);
    }
    expect(onButtons).toBeGreaterThan(0);
    expect(source).not.toContain('tabindex="-1"');
    expect(source).toContain(":inert=");
  });
});

describe("ticketTarget", () => {
  test("opens .lime/tickets/<slug>.md in a registered project", () => {
    const row = reportRows(
      report([item({ group: "needs_you", reason: "ticket", project: "alpha", ticket: "227-focus-tab" })]),
    ).open[0];
    expect(ticketTarget(["alpha", "beta"], row)).toEqual({
      project: "alpha",
      path: ".lime/tickets/227-focus-tab.md",
    });
  });

  test("is null for an unregistered project, a goal, or no ticket", () => {
    const registered = reportRows(
      report([item({ group: "needs_you", reason: "ticket", project: "alpha", ticket: "227-a" })]),
    ).open[0];
    expect(ticketTarget(["beta"], registered)).toBe(null);
    const ticketless = reportRows(
      report([item({ group: "failed", reason: "fail", project: "alpha", ticket: null })]),
    ).open[0];
    expect(ticketTarget(["alpha"], ticketless)).toBe(null);
    const g = goalRows([goal({ id: "f1", project: "alpha" })])[0];
    expect(ticketTarget(["alpha"], g)).toBe(null);
  });
});

describe("Workspace wiring", () => {
  test("openTicket sets project only from ticketTarget", () => {
    const source = readFileSync(join(import.meta.dir, "../components/Workspace.vue"), "utf8");
    const start = source.indexOf("async function openTicket");
    expect(start).toBeGreaterThan(-1);
    const body = source.slice(start, source.indexOf("\n}", start));
    expect(body).toContain("ticketTarget(");
    expect(body.match(/project\.value\s*=/g)?.length ?? 0).toBe(1);
  });

  test("closeTop chains the Focus tab and drops openFocusMd", () => {
    const source = readFileSync(join(import.meta.dir, "../components/Workspace.vue"), "utf8");
    const start = source.indexOf("function closeTop");
    expect(start).toBeGreaterThan(-1);
    const body = source.slice(start, source.indexOf("\n}", start));
    expect(body).toContain("focusTabEl.value?.escape()");
    expect(source).not.toContain("openFocusMd");
  });
});

describe("errorLine", () => {
  test("joins or nulls", () => {
    expect(errorLine([])).toBe(null);
    expect(errorLine(["a", "b"])).toBe("a · b");
  });
});
