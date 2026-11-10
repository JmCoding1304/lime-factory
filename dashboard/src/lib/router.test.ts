import { describe, expect, test } from "bun:test";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { hrefFor, namesRun, parseHash, phaseFor, resolveLinkPhase, tabPhase } from "./router";
import { activeTab, openRunTab, runTabName } from "./editorTabs";
import type { TabStrips } from "./editorTabs";
import { CARD_H, CARD_W, revealView } from "./canvas";
import { dockActive, showPhaseDock, type RunDock } from "./editorTabs";

const HOME = { project: null, runId: null, phaseId: null };

describe("parseHash", () => {
  test("#/cost and #/profiles are removed, not redirected: both open home", () => {
    for (const hash of ["#/cost", "#/profiles", "#/cost/", "#/profiles/"]) {
      expect(parseHash(hash)).toEqual(HOME);
    }
  });

  test("#/cost/<run> and #/profiles/<run> are Run links", () => {
    expect(parseHash("#/cost/my-run")).toEqual({
      project: "cost",
      runId: "my-run",
      phaseId: null,
    });
    expect(parseHash("#/profiles/my-run")).toEqual({
      project: "profiles",
      runId: "my-run",
      phaseId: null,
    });
  });

  test("#/, an unknown single segment and a bare hash are home", () => {
    for (const hash of ["#/", "", "#/proj-a", "#weird"]) {
      expect(parseHash(hash)).toEqual(HOME);
    }
  });

  test("#/runs is removed, not redirected: it opens home", () => {
    expect(parseHash("#/runs")).toEqual(HOME);
    expect(parseHash("#/runs/")).toEqual(HOME);
  });

  test("#/<project>/<run> and <phase> are Run links", () => {
    expect(parseHash("#/proj-a/run-1")).toEqual({
      project: "proj-a",
      runId: "run-1",
      phaseId: null,
    });
    expect(parseHash("#/proj-a/run-1/phase-1")).toEqual({
      project: "proj-a",
      runId: "run-1",
      phaseId: "phase-1",
    });
  });

  test("more than three segments is home", () => {
    expect(parseHash("#/a/b/c/d")).toEqual(HOME);
  });

  test("a Run link opens home with that Run tab active in the current project's strip", () => {
    const route = parseHash("#/proj-a/run-1/phase-1");
    const strips: TabStrips = {};
    openRunTab(
      strips,
      "cur",
      { project: route.project!, runId: route.runId! },
      runTabName(null, route.runId!),
    );
    expect(activeTab(strips, "cur")?.run).toEqual({
      project: "proj-a",
      runId: "run-1",
    });
    expect(strips["proj-a"]).toBeUndefined();

    expect(phaseFor(route, { project: "proj-a", runId: "run-1" })).toBe("phase-1");
    expect(phaseFor(route, { project: "proj-a", runId: "run-2" })).toBe(null);
    expect(phaseFor(parseHash("#/"), { project: "proj-a", runId: "run-1" })).toBe(null);
  });
});

describe("a phase link", () => {
  const PHASES = [
    { phase_id: "run-1:01", name: "plan" },
    { phase_id: "run-1:02", name: "build_1" },
  ];

  test("#/<project>/<run>/<phase> selects the card, pans it clear of the dock and opens its tab", () => {
    const route = parseHash("#/proj-a/run-1/build_1");
    const seg = phaseFor(route, { project: "proj-a", runId: "run-1" });
    expect(resolveLinkPhase(seg, PHASES)).toEqual({
      phaseId: "run-1:02",
      rewrite: false,
    });

    const v = revealView({ x: 0, y: 0, k: 1 }, { x: 2000, y: 900 }, 800, 400, true);
    const left = v.x + 2000 * v.k;
    const top = v.y + 900 * v.k;
    expect(left).toBeGreaterThanOrEqual(0);
    expect(top).toBeGreaterThanOrEqual(0);
    expect(left + CARD_W * v.k).toBeLessThanOrEqual(800);
    expect(top + CARD_H * v.k).toBeLessThanOrEqual(400);

    const dock: RunDock = { runOpen: true, active: "run" };
    showPhaseDock(dock);
    expect(dockActive(dock, true)).toBe("phase");
  });

  test("an id resolves, and a retried name resolves to the last match", () => {
    expect(resolveLinkPhase("run-1:01", PHASES)).toEqual({
      phaseId: "run-1:01",
      rewrite: false,
    });
    const retried = [
      { phase_id: "run-1:02", name: "build_1" },
      { phase_id: "run-1:05", name: "build_1" },
    ];
    expect(resolveLinkPhase("build_1", retried)).toEqual({
      phaseId: "run-1:05",
      rewrite: false,
    });
  });

  test("an unknown phase rewrites, an empty list rewrites, and no run frame or no segment does neither", () => {
    expect(resolveLinkPhase("nope", PHASES)).toEqual({ phaseId: null, rewrite: true });
    expect(resolveLinkPhase("build_1", [])).toEqual({ phaseId: null, rewrite: true });
    expect(resolveLinkPhase("build_1", null)).toEqual({ phaseId: null, rewrite: false });
    expect(resolveLinkPhase(null, PHASES)).toEqual({ phaseId: null, rewrite: false });
  });

  test("another Run's link names no phase", () => {
    const route = parseHash("#/proj-a/run-2/build_1");
    const seg = phaseFor(route, { project: "proj-a", runId: "run-1" });
    expect(seg).toBeNull();
    expect(resolveLinkPhase(seg, PHASES)).toEqual({ phaseId: null, rewrite: false });
  });

  test("revealView leaves a card inside the margins alone, centres it on request, and ignores a zero viewport", () => {
    const v = { x: 0, y: 0, k: 1 };
    const pos = { x: 100, y: 100 };
    expect(revealView(v, pos, 800, 400, false)).toEqual(v);
    const centred = revealView(v, pos, 800, 400, true);
    expect(centred).not.toEqual(v);
    expect(centred.k).toBe(1);
    expect(centred.x + (pos.x + CARD_W / 2) * centred.k).toBe(400);
    expect(centred.y + (pos.y + CARD_H / 2) * centred.k).toBe(200);
    expect(revealView({ x: 5, y: 6, k: 2 }, pos, 0, 400, true)).toEqual({
      x: 5,
      y: 6,
      k: 2,
    });
  });

  test("hrefFor and parseHash round-trip encoded segments", () => {
    expect(parseHash(hrefFor("p q", "r/1", "build_1"))).toEqual({
      project: "p q",
      runId: "r/1",
      phaseId: "build_1",
    });
  });
});

describe("a phase belongs to one Run (PLACEMENT row 81)", () => {
  const A = { project: "proj-a", runId: "run-1" };
  const B = { project: "proj-a", runId: "run-2" };

  test("the link speaks only for the Run it names", () => {
    const route = parseHash("#/proj-a/run-1/phase-1");
    expect(namesRun(route, A)).toBe(true);
    expect(namesRun(route, B)).toBe(false);
    expect(tabPhase(route, A, null)).toBe("phase-1");
  });

  test("a phase-less link for this Run closes its card, even with a stored phase", () => {
    const route = parseHash("#/proj-a/run-1");
    expect(tabPhase(route, A, "phase-1")).toBe(null);
  });

  test("switching through another Run keeps each tab's own selection and open card", () => {
    // Run A selected phase-1; the user opens Run B, so the link names B.
    const route = parseHash("#/proj-a/run-2");
    expect(tabPhase(route, A, "phase-1")).toBe("phase-1");
    expect(tabPhase(route, B, null)).toBe(null);
    // B selects its own card; A's selection is untouched.
    const onB = parseHash("#/proj-a/run-2/phase-9");
    expect(tabPhase(onB, B, null)).toBe("phase-9");
    expect(tabPhase(onB, A, "phase-1")).toBe("phase-1");
    // Reactivating A shows its card again, and its dock tab is still open.
    const dock: RunDock = { runOpen: false, active: "phase" };
    expect(dockActive(dock, tabPhase(onB, A, "phase-1") !== null)).toBe("phase");
  });

  test("a Run tab starts with no phase and keeps the one it selects", () => {
    const strips: TabStrips = {};
    const { tab } = openRunTab(strips, "cur", A, runTabName(null, A.runId));
    expect(tab.phase).toBe(null);
    tab.phase = "phase-1";
    expect(activeTab(strips, "cur")?.phase).toBe("phase-1");
  });

  test("the editor reads and writes the active Run tab's phase, not the bare link", () => {
    const dir = join(import.meta.dir, "..");
    const editor = readFileSync(join(dir, "components/Editor.vue"), "utf8");
    expect(editor).toContain("tabPhase(route.value, t.run, t.phase ?? null)");
    expect(editor).toContain('@phase="onRunPhase"');
    expect(editor).not.toContain("phaseFor(route");
    const rv = readFileSync(join(dir, "components/RunView.vue"), "utf8");
    expect(rv).toContain('emit("phase", id)');
    expect(rv).toContain("namesRun(route.value");
  });
});

describe("hash writers", () => {
  const dir = join(import.meta.dir, "..");
  const files = (readdirSync(dir, { recursive: true }) as string[])
    .map((f) => f.split("\\").join("/"))
    .filter((f) => (f.endsWith(".ts") || f.endsWith(".vue")) && !f.endsWith(".test.ts"));
  const source = (f: string) => readFileSync(join(dir, f), "utf8");

  test("only RunView.vue and Workspace.vue write the hash", () => {
    const writers = files.filter((f) =>
      /\bnavigate\(|\breplaceRoute\(|\bhrefFor\(|location\.hash\s*=[^=]|replaceState\(/.test(
        source(f),
      ),
    );
    expect(writers.sort()).toEqual([
      "components/RunView.vue",
      "components/Workspace.vue",
      "lib/router.ts",
    ]);
  });

  test("opening a Run never switches project and renders RunView in the editor", () => {
    const ws = source("components/Workspace.vue");
    for (const name of ["function showRun", "function openRun"]) {
      const start = ws.indexOf(name);
      expect(start).toBeGreaterThan(-1);
      const body = ws.slice(start, ws.indexOf("\n}", start));
      expect(/project\.value\s*=/.test(body)).toBe(false);
    }
    const editor = source("components/Editor.vue");
    expect(editor).toContain("<RunView");
    expect(editor).toContain("openRunTab(");
    expect(editor).not.toContain("RunTrace");
    const app = source("App.vue");
    expect(app).not.toContain("RunTrace");
    expect(app).not.toContain("RunView");
    expect(app).not.toContain("RunsList");
  });

  test("a notch row opens its Run in the current strip without switching project, All included", () => {
    // (a) The pure tab behaviour: a Run tab lands in the given strip, never
    // creating a project strip, and a repeated open reuses the tab.
    for (const cur of ["all", "cur"]) {
      const strips: TabStrips = {};
      openRunTab(
        strips,
        cur,
        { project: "proj-a", runId: "run-1" },
        runTabName(null, "run-1"),
      );
      expect(activeTab(strips, cur)?.run).toEqual({ project: "proj-a", runId: "run-1" });
      expect(strips["proj-a"]).toBeUndefined();
    }
    const strips: TabStrips = {};
    openRunTab(strips, "cur", { project: "proj-a", runId: "run-1" }, runTabName(null, "run-1"));
    const again = openRunTab(
      strips,
      "cur",
      { project: "proj-a", runId: "run-1" },
      runTabName(null, "run-1"),
    );
    expect(again.created).toBe(false);

    // (b) The notch emits; the shell opens. The notch writes no hash itself.
    const ws = source("components/Workspace.vue");
    const tag = ws.match(/<Notch[\s\S]*?\/>/);
    expect(tag).not.toBeNull();
    expect(tag![0]).toContain('@open-run="openRun"');
    const notch = source("components/Notch.vue");
    expect(notch).toContain("emit('open-run'");
    expect(notch).not.toContain("lib/router");

    // (c) The Editor's openRun drops only a null project: the All strip stays
    // open to Run tabs.
    const editor = source("components/Editor.vue");
    const start = editor.indexOf("function openRun");
    const body = editor.slice(start, editor.indexOf("\n}", start));
    expect(body).not.toContain("'all'");
    expect(body).not.toContain('"all"');
  });
});

describe("the Run page", () => {
  const dir = join(import.meta.dir, "..");
  const source = (f: string) => readFileSync(join(dir, f), "utf8");
  const rv = () => source("components/RunView.vue");

  /** The `function name` body, sliced up to the first column-0 `}` (runsSheet.test.ts idiom). */
  function fnBody(src: string, name: string): string {
    const start = src.indexOf(`function ${name}`);
    if (start < 0) return "";
    const end = src.indexOf("\n}", start);
    return src.slice(start, end < 0 ? undefined : end);
  }

  test("the four RunTrace components are deleted", () => {
    for (const f of [
      "components/RunTrace.vue",
      "components/PhaseDetail.vue",
      "components/SessionTab.vue",
      "components/StreamTab.vue",
    ]) {
      expect(existsSync(join(dir, f))).toBe(false);
    }
  });

  test("the dock sits beside the terminal, not over the canvas", () => {
    const src = rv();
    for (const needle of ["<RunCanvas", "<Terminal", "<RunLine"]) {
      expect(src).toContain(needle);
    }
    const bar = src.indexOf("#bar");
    const line = src.indexOf("<RunLine", bar);
    const end = src.indexOf("</Terminal>");
    const dock = src.indexOf("#dock");
    const info = src.indexOf("<RunInfo", dock);
    const card = src.indexOf("<CardDetail", dock);
    expect(bar).toBeGreaterThan(-1);
    expect(line).toBeGreaterThan(bar);
    expect(line).toBeLessThan(end);
    expect(dock).toBeGreaterThan(bar);
    expect(info).toBeGreaterThan(dock);
    expect(card).toBeGreaterThan(dock);
    expect(info).toBeLessThan(end);
    expect(card).toBeLessThan(end);
  });

  test("the empty canvas carries RunInfo, the API banner and the live feed", () => {
    const src = rv();
    const empty = src.indexOf("#empty");
    expect(empty).toBeGreaterThan(-1);
    expect(src.indexOf("<RunInfo", empty)).toBeGreaterThan(empty);
    expect(src).toContain("api unreachable — retrying");
    expect(src).toContain("live.runFeed(");
  });

  test("deep links resolve, reveal and open the dock", () => {
    const src = rv();
    for (const needle of [
      "resolveLinkPhase(",
      "replaceRoute(",
      ".reveal(",
      ':selected="openId"',
      ':phase-id="openId"',
      "showPhaseDock(",
    ]) {
      expect(src).toContain(needle);
    }
  });

  test("closing pushes so Back reopens; it never rewrites the link", () => {
    const src = rv();
    const close = fnBody(src, "closePhase");
    const esc = fnBody(src, "escape");
    expect(close).toContain("navigate(props.project, props.runId)");
    expect(close).not.toContain("replaceRoute");
    expect(esc).not.toContain("replaceRoute");
    expect(esc).toContain("closePhase()");
    expect(close + esc).toContain("navigate(props.project, props.runId)");
  });

  test("the editor forwards Esc to the dock", () => {
    expect(fnBody(source("components/Editor.vue"), "escape")).toContain(
      "runView.value?.escape()",
    );
  });

  test("no hardcoded 150k context threshold in dashboard/src", () => {
    const files = (readdirSync(dir, { recursive: true }) as string[])
      .map((f) => f.split("\\").join("/"))
      .filter(
        (f) =>
          (f.endsWith(".ts") || f.endsWith(".vue") || f.endsWith(".css")) &&
          !f.endsWith(".test.ts"),
      );
    const hits = files.filter((f) =>
      /150[_,]?000|150\s?k\b/i.test(readFileSync(join(dir, f), "utf8")),
    );
    expect(hits).toEqual([]);
  });
});
