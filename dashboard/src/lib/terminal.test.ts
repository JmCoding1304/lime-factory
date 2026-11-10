import { describe, expect, test } from "bun:test";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import type { FeedEvent } from "./types";
import {
  TOOL_CLS,
  terminalLines,
  windowLines,
  showEarlier,
  onNewLines,
  jumpLabel,
  emptyText,
  loadTermPrefs,
  saveTermHeight,
  saveTermCollapsed,
  clampTermHeight,
  PAGE,
  TERM_H_KEY,
  TERM_COLLAPSED_KEY,
} from "./terminal";
import { argsSummary } from "./events";
import { fmtBytes, fmtCallMs } from "./format";
import { oneLine } from "./stream";

function ev(
  seq: number,
  type: string,
  payload: unknown,
  over?: Partial<FeedEvent>,
): FeedEvent {
  return {
    seq,
    type: type as FeedEvent["type"],
    name: null,
    phase_id: "p1",
    parent_seq: null,
    payload,
    tokens: null,
    started_at: null,
    ...over,
  };
}

const call = (seq: number, payload: Record<string, unknown>, over?: Partial<FeedEvent>) =>
  ev(seq, "tool_call", { tool_call_id: "c1", ...payload }, over);
const result = (seq: number, payload: Record<string, unknown>, over?: Partial<FeedEvent>) =>
  ev(seq, "tool_result", { tool_call_id: "c1", ...payload }, over);

describe("terminalLines: bash calls", () => {
  test("call + result folds into one line", () => {
    const tail = "x".repeat(2048);
    const lines = terminalLines([
      call(1, { tool: "Bash", args: { command: "echo hi" } }),
      result(2, { ok: true, duration_ms: 1234, out_bytes: 12600, tail }),
    ]);
    expect(lines.length).toBe(1);
    const l = lines[0];
    expect(l.kind).toBe("cmd");
    expect(l.tool).toBeNull();
    expect(l.text).toBe("echo hi");
    expect(l.ok).toBe(true);
    expect(l.right).toBe("1.2s");
    expect(l.out).toEqual([tail]);
    expect(l.meta).toContain("# output 12.3 KB · last 2.0 KB shown");
    expect(l.meta).not.toContain("# no result recorded");
  });

  test("failed result keeps ok false", () => {
    const lines = terminalLines([
      call(1, { tool: "Bash", args: { command: "false" } }),
      result(2, { ok: false, duration_ms: 5, out_bytes: 0, tail: "" }),
    ]);
    expect(lines[0].ok).toBe(false);
    expect(lines[0].meta).toContain("# no output");
  });

  test("out_bytes equal to the tail length shows one size", () => {
    const tail = "y".repeat(340);
    const lines = terminalLines([
      call(1, { tool: "Bash", args: { command: "echo" } }),
      result(2, { ok: true, duration_ms: 100, out_bytes: 340, tail }),
    ]);
    expect(lines[0].meta).toContain("# output 340 B");
  });

  test("out_bytes zero means no output", () => {
    const lines = terminalLines([
      call(1, { tool: "Bash", args: { command: "true" } }),
      result(2, { ok: true, duration_ms: 100, out_bytes: 0, tail: "" }),
    ]);
    expect(lines[0].meta).toContain("# no output");
    expect(lines[0].out).toBeNull();
  });

  test("a description heads the meta", () => {
    const lines = terminalLines([
      call(1, { tool: "Bash", args: { command: "ls", description: "list files" } }),
    ]);
    expect(lines[0].meta[0]).toBe("# list files");
    expect(lines[0].meta).toContain("# no result recorded");
  });
});

describe("terminalLines: other agent tools", () => {
  test("read call shows the tool and its args", () => {
    const args = { file_path: "/a/b.ts" };
    const lines = terminalLines([call(1, { tool: "Read", args })]);
    const l = lines[0];
    expect(l.tool).toBe("read");
    expect(l.toolCls).toBe("t-read");
    expect(l.text).toBe(argsSummary(args));
    expect(l.detail).toEqual(JSON.stringify(args, null, 2).split("\n"));
  });

  test("an unknown tool is dim", () => {
    const lines = terminalLines([call(1, { tool: "Frobnicate", args: { x: 1 } })]);
    expect(lines[0].toolCls).toBe("t-dim");
    expect(TOOL_CLS.frobnicate).toBeUndefined();
  });

  test("a call with no result is unmarked", () => {
    const lines = terminalLines([
      call(1, { tool: "Bash", args: { command: "sleep 1" } }),
    ]);
    expect(lines.length).toBe(1);
    expect(lines[0].ok).toBeNull();
    expect(lines[0].right).toBe("");
    expect(lines[0].meta).toContain("# no result recorded");
  });

  test("a pre-250 call's own boolean ok still marks it", () => {
    const lines = terminalLines([
      call(1, { tool: "Bash", args: { command: "boom" }, ok: false }),
    ]);
    expect(lines[0].ok).toBe(false);
  });
});

describe("terminalLines: result pairing", () => {
  test("an orphan result draws nothing", () => {
    expect(terminalLines([result(1, { ok: true })])).toEqual([]);
  });

  test("a duplicate result: the first wins", () => {
    const lines = terminalLines([
      call(1, { tool: "Bash", args: { command: "echo" } }),
      result(2, { ok: true, duration_ms: 1000, out_bytes: 0, tail: "" }),
      result(3, { ok: false, duration_ms: 9000, out_bytes: 0, tail: "" }),
    ]);
    expect(lines.length).toBe(1);
    expect(lines[0].ok).toBe(true);
    expect(lines[0].right).toBe("1.0s");
  });

  test("a result never draws its own line", () => {
    const lines = terminalLines([result(5, { ok: true })]);
    expect(lines.length).toBe(0);
  });

  test("the same id in two phases pairs within its phase", () => {
    const lines = terminalLines([
      call(1, { tool: "Bash", args: { command: "a" } }, { phase_id: "p1" }),
      ev(2, "tool_result", { tool_call_id: "c1", ok: false, duration_ms: 20, out_bytes: 5, tail: "p1out" }, { phase_id: "p1" }),
      call(3, { tool: "Bash", args: { command: "b" } }, { phase_id: "p2" }),
      ev(4, "tool_result", { tool_call_id: "c1", ok: true, duration_ms: 10, out_bytes: 5, tail: "p2out" }, { phase_id: "p2" }),
    ]);
    expect(lines.length).toBe(2);
    expect(lines[0].text).toBe("a");
    expect(lines[0].ok).toBe(false);
    expect(lines[1].text).toBe("b");
    expect(lines[1].ok).toBe(true);
  });
});

describe("terminalLines: quality calls", () => {
  test("a passing quality command", () => {
    const lines = terminalLines([
      ev(
        1,
        "tool_call",
        {
          command: "bun test",
          returncode: 0,
          passed: true,
          output_artifact: "/log/x.txt",
          started_at: "2024-01-01T00:00:00Z",
          ended_at: "2024-01-01T00:04:07Z",
        },
        { name: "quality:test" },
      ),
    ]);
    const l = lines[0];
    expect(l.kind).toBe("cmd");
    expect(l.text).toBe("bun test");
    expect(l.ok).toBe(true);
    expect(l.right).toBe("exit 0 · 4m 07s");
    expect(l.meta).toContain("# log /log/x.txt");
    expect(l.meta).toContain("# test");
  });

  test("a failing quality command", () => {
    const lines = terminalLines([
      ev(
        1,
        "tool_call",
        { command: "bun test", returncode: 1, passed: false, started_at: "2024-01-01T00:00:00Z", ended_at: "2024-01-01T00:00:05Z" },
        { name: "quality:test" },
      ),
    ]);
    expect(lines[0].ok).toBe(false);
    expect(lines[0].right).toBe("exit 1 · 5s");
  });
});

describe("terminalLines: phase lines", () => {
  test("phase_start draws a rule", () => {
    const lines = terminalLines([
      ev(1, "phase_start", {}, { name: "build_1", started_at: "2024-01-01T00:00:00Z" }),
    ]);
    expect(lines[0].kind).toBe("rule");
    expect(lines[0].text).toBe("▶ build_1");
  });

  test("phase_end success carries ok true and its duration", () => {
    const lines = terminalLines([
      ev(1, "phase_start", {}, { name: "build_1", started_at: "2024-01-01T00:00:00Z" }),
      ev(2, "phase_end", { status: "success" }, { name: "build_1", started_at: "2024-01-01T00:04:07Z" }),
    ]);
    expect(lines[1].ok).toBe(true);
    expect(lines[1].right).toBe("4m 07s");
    expect(lines[1].text).toBe("build_1 · success");
  });

  test("phase_end fail carries ok false", () => {
    const lines = terminalLines([
      ev(1, "phase_end", { status: "fail" }, { name: "build_1", started_at: "2024-01-01T00:00:01Z" }),
    ]);
    expect(lines[0].ok).toBe(false);
  });
});

describe("terminalLines: signal lines", () => {
  test("errors carry the ■ / ⏱ / ✕ glyphs", () => {
    const cases: [string, Record<string, unknown>, string][] = [
      ["budget_stop", { context_tokens: 180000, stop: 160000 }, "■ budget stop"],
      ["budget_kill", { context_tokens: 190000 }, "■ budget kill"],
      ["timeout_kill", { elapsed_s: 1800, timeout_s: 1800 }, "⏱ timeout kill"],
      ["repeat_stop", { tool: "Read", count: 3 }, "■ repeat stop"],
      ["crash_detected", { signal: "SIGKILL", exit_code: 137 }, "✕ crash"],
    ];
    for (const [type, payload, head] of cases) {
      const lines = terminalLines([ev(1, type, payload)]);
      expect(lines[0].kind).toBe("err");
      expect(lines[0].text.startsWith(head)).toBe(true);
    }
  });

  test("timeout_kill repeat vs ceiling", () => {
    const repeat = terminalLines([
      ev(1, "timeout_kill", { elapsed_s: 600, timeout_s: 1800, reason: "repeat" }),
    ]);
    expect(repeat[0].text).toBe("⏱ timeout kill · 10m 00s · repeat");
    const ceiling = terminalLines([
      ev(1, "timeout_kill", { elapsed_s: 1800, timeout_s: 1800 }),
    ]);
    expect(ceiling[0].text).toBe("⏱ timeout kill · 30m 00s · ceiling 30m 00s");
  });

  test("warnings carry the ▲ / ↻ / ⏱ glyphs", () => {
    const cases: [string, Record<string, unknown>, string][] = [
      ["budget_warn", { context_tokens: 120000, warn: 100000 }, "▲ context 120k"],
      ["continuation", { continuation: 2, reason: "leak" }, "↻ fresh session 2"],
      ["timeout_nudge", { elapsed_s: 1200 }, "⏱ time ceiling"],
    ];
    for (const [type, payload, head] of cases) {
      const lines = terminalLines([ev(1, type, payload)]);
      expect(lines[0].kind).toBe("warn");
      expect(lines[0].text.startsWith(head)).toBe(true);
    }
  });

  test("timeout_nudge repeat", () => {
    const lines = terminalLines([ev(1, "timeout_nudge", { elapsed_s: 600, reason: "repeat" })]);
    expect(lines[0].text).toBe("⏱ time ceiling · nudged after 10m 00s · repeat");
  });
});

describe("terminalLines: rescope", () => {
  test("needs_rescope handoff", () => {
    const lines = terminalLines([
      ev(1, "handoff", { summary: "too big", path: ".lime/runs/x/handoff.md" }, { name: "needs_rescope" }),
    ]);
    expect(lines[0].kind).toBe("warn");
    expect(lines[0].text.startsWith("◆ needs rescope ·")).toBe(true);
    expect(lines[0].detail).toContain("handoff: .lime/runs/x/handoff.md");
  });

  test("rescope_recovery_decision log", () => {
    const lines = terminalLines([
      ev(1, "log", { decision: "continue" }, { name: "rescope_recovery_decision" }),
    ]);
    expect(lines[0].kind).toBe("warn");
    expect(lines[0].text).toBe("◆ rescope decision · continue");
  });
});

describe("terminalLines: gates", () => {
  test("gate_pass with checks", () => {
    const lines = terminalLines([
      ev(
        1,
        "gate_pass",
        {
          checks: [
            { item: "diff", ok: true, note: "small" },
            { item: "tests", ok: true, note: "" },
            { item: "lint", ok: true },
          ],
        },
      ),
    ]);
    expect(lines[0].kind).toBe("ok");
    expect(lines[0].text).toBe("gate · 3 checks passed");
    expect(lines[0].detail).toContain("✓ diff  small");
  });

  test("gate_fail with violations", () => {
    const lines = terminalLines([
      ev(1, "gate_fail", { violations: ["a", "b"], checks: [] }),
    ]);
    expect(lines[0].kind).toBe("err");
    expect(lines[0].text).toBe("gate · a; b");
    expect(lines[0].detail).toBeNull();
  });
});

describe("terminalLines: log, prompt and handoff lines", () => {
  test("a log message becomes a comment / err / warn", () => {
    expect(terminalLines([ev(1, "log", { message: "hi" })])[0].kind).toBe("comment");
    expect(terminalLines([ev(1, "log", { message: "bad", level: "error" })])[0].kind).toBe("err");
    expect(terminalLines([ev(1, "log", { message: "hmm", level: "warning" })])[0].kind).toBe("warn");
  });

  test("phase banners draw no line", () => {
    expect(terminalLines([ev(1, "log", { message: "▶ build_1" })])).toEqual([]);
    expect(terminalLines([ev(1, "log", { message: "✓ build_1 done" })])).toEqual([]);
  });

  test("a prompt log", () => {
    const lines = terminalLines([ev(1, "log", { prompt: "do the thing" })]);
    expect(lines[0].kind).toBe("comment");
    expect(lines[0].text).toBe("prompt · do the thing");
  });

  test("plan_provenance becomes a brief", () => {
    const lines = terminalLines([
      ev(1, "handoff", { prompt: "the brief" }, { name: "plan_provenance" }),
    ]);
    expect(lines[0].text).toBe("brief · the brief");
  });

  test("another handoff lists its artifacts", () => {
    const lines = terminalLines([
      ev(1, "handoff", { summary: "done", artifacts: ["a.md", { x: 1 }] }, { name: "builder" }),
    ]);
    expect(lines[0].text).toBe("handoff · done");
    expect(lines[0].detail).toContain("artifacts:");
    expect(lines[0].detail).toContain("  a.md");
    expect(lines[0].detail).toContain('  {"x":1}');
  });
});

describe("terminalLines: agent rules", () => {
  test("agent_start shows the model, thinking and runtime", () => {
    const lines = terminalLines([
      ev(
        1,
        "agent_start",
        { model: "openrouter/deepseek/deepseek-v4.1-flash", thinking: "high", runtime: "pi" },
        { name: "builder" },
      ),
    ]);
    expect(lines[0].kind).toBe("rule");
    expect(lines[0].text).toBe("builder · deepseek-v4.1-flash · high · pi");
  });

  test("agent_end prices a cash model", () => {
    const lines = terminalLines([
      ev(1, "agent_start", { model: "openrouter/deepseek/deepseek-v4.1-flash" }, { name: "builder" }),
      ev(2, "agent_end", { status: "ok", cost: 0.27, context_tokens: 40000 }),
    ]);
    expect(lines[1].text).toBe("end · ok · $0.27 · 40k context");
  });

  test("agent_end shows subscription for a non-cash model", () => {
    const lines = terminalLines([
      ev(1, "agent_start", { model: "claude-opus-5-5" }, { name: "builder" }),
      ev(2, "agent_end", { status: "ok", cost: 0.27, context_tokens: 40000 }),
    ]);
    expect(lines[1].text).toBe("end · ok · subscription · 40k context");
  });
});

describe("terminalLines: bad input", () => {
  test("turn draws no line", () => {
    expect(terminalLines([ev(1, "turn", { turn_no: 1 })])).toEqual([]);
  });

  test("null, string or array payloads never throw", () => {
    for (const payload of [null, "just a string", ["an", "array"]]) {
      const lines = terminalLines([ev(1, "refusal", payload)]);
      expect(lines.length).toBe(1);
      expect(lines[0].ok).toBeNull();
    }
  });
});

describe("terminalLines: filter and order", () => {
  test("phaseId keeps only that phase", () => {
    const events = [
      ev(1, "log", { message: "one" }, { phase_id: "p1" }),
      ev(2, "log", { message: "two" }, { phase_id: "p2" }),
    ];
    expect(terminalLines(events, "p2")).toHaveLength(1);
    expect(terminalLines(events, "p2")[0].text).toBe("two");
    expect(terminalLines(events, null)).toHaveLength(2);
  });

  test("out-of-order seq comes out sorted", () => {
    const lines = terminalLines([
      ev(2, "log", { message: "second" }),
      ev(1, "log", { message: "first" }),
    ]);
    expect(lines.map((l) => l.text)).toEqual(["first", "second"]);
  });
});

describe("format: fmtCallMs / fmtBytes", () => {
  test("fmtCallMs", () => {
    expect(fmtCallMs(340)).toBe("340ms");
    expect(fmtCallMs(1234)).toBe("1.2s");
    expect(fmtCallMs(47_000)).toBe("47s");
    expect(fmtCallMs(247_000)).toBe("4m 07s");
    expect(fmtCallMs(-5)).toBe("0ms");
  });

  test("fmtBytes", () => {
    expect(fmtBytes(340)).toBe("340 B");
    expect(fmtBytes(12_600)).toBe("12.3 KB");
    expect(fmtBytes(3_500_000)).toBe("3.3 MB");
  });
});

describe("stream: oneLine max", () => {
  test("default and custom caps", () => {
    expect(oneLine("a".repeat(500)).length).toBe(161);
    expect(oneLine("a".repeat(500), 400).length).toBe(401);
  });
});

describe("windowLines: 1,600 calls", () => {
  const events: FeedEvent[] = [];
  let seq = 0;
  for (let i = 1; i <= 1600; i++) {
    const id = `c${i}`;
    events.push(
      ev(++seq, "tool_call", {
        tool_call_id: id,
        tool: "Bash",
        args: { command: `echo ${i}` },
      }),
      ev(++seq, "tool_result", {
        tool_call_id: id,
        ok: true,
        duration_ms: 10,
        out_bytes: 0,
        tail: "",
      }),
    );
  }
  const lines = terminalLines(events);

  test("all 1,600 calls draw one line each", () => {
    expect(lines).toHaveLength(1600);
  });

  test("PAGE hides all but the newest 500", () => {
    const w = windowLines(lines, PAGE);
    expect(w.shown).toHaveLength(500);
    expect(w.shown[0].text).toBe("echo 1101");
    expect(w.shown[w.shown.length - 1].text).toBe("echo 1600");
    expect(w.hidden).toBe(1100);
  });

  test("each Show earlier reveals another page", () => {
    let limit = PAGE;
    limit = showEarlier(limit);
    let w = windowLines(lines, limit);
    expect(w.shown[0].text).toBe("echo 601");
    expect(w.hidden).toBe(600);

    limit = showEarlier(limit);
    w = windowLines(lines, limit);
    expect(w.shown[0].text).toBe("echo 101");
    expect(w.hidden).toBe(100);

    limit = showEarlier(limit);
    w = windowLines(lines, limit);
    expect(w.shown).toHaveLength(1600);
    expect(w.shown[0].text).toBe("echo 1");
    expect(w.hidden).toBe(0);
  });

  test("every shown line is a finished call", () => {
    const w = windowLines(lines, PAGE);
    for (const l of w.shown) {
      expect(l.ok).toBe(true);
      expect(l.right).toBeTruthy();
    }
  });
});

describe("tail state", () => {
  test("following keeps the window and clears unseen", () => {
    expect(onNewLines({ limit: 500, unseen: 0 }, 3, true)).toEqual({
      limit: 500,
      unseen: 0,
    });
  });

  test("scrolled up grows the window and counts unseen", () => {
    const a = onNewLines({ limit: 500, unseen: 0 }, 3, false);
    expect(a).toEqual({ limit: 503, unseen: 3 });
    expect(onNewLines(a, 2, false)).toEqual({ limit: 505, unseen: 5 });
  });

  test("no added lines is a no-op", () => {
    const s = { limit: 500, unseen: 0 };
    expect(onNewLines(s, 0, false)).toBe(s);
    expect(onNewLines(s, -2, false)).toBe(s);
  });

  test("jumpLabel", () => {
    expect(jumpLabel(0)).toBe("Jump to latest");
    expect(jumpLabel(3)).toBe("Jump to latest · 3 new");
  });
});

describe("emptyText", () => {
  test("queued, phase and whole Run", () => {
    expect(emptyText("p1", true)).toBe("not started yet");
    expect(emptyText("p1", false)).toBe("no events for this phase");
    expect(emptyText(null, false)).toBe("no events yet");
  });
});

function fakeStorage(init?: Record<string, string>): {
  map: Map<string, string>;
  getItem: (k: string) => string | null;
  setItem: (k: string, v: string) => void;
} {
  const map = new Map<string, string>(Object.entries(init ?? {}));
  return {
    map,
    getItem: (k) => (map.has(k) ? map.get(k)! : null),
    setItem: (k, v) => {
      map.set(k, v);
    },
  };
}

describe("saved prefs", () => {
  test("null storage gives defaults", () => {
    expect(loadTermPrefs(null)).toEqual({ height: null, collapsed: false });
  });

  test("a stored height and collapsed flag are read", () => {
    const s = fakeStorage({ [TERM_H_KEY]: "320", [TERM_COLLAPSED_KEY]: "1" });
    expect(loadTermPrefs(s)).toEqual({ height: 320, collapsed: true });
  });

  test.each(["abc", "-5", "0"])('a malformed height "%s" is ignored', (raw) => {
    const s = fakeStorage({ [TERM_H_KEY]: raw });
    expect(loadTermPrefs(s).height).toBeNull();
  });

  test("a getItem that throws gives defaults", () => {
    const s = {
      getItem: () => {
        throw new Error("nope");
      },
    };
    expect(loadTermPrefs(s)).toEqual({ height: null, collapsed: false });
  });

  test("save rounds and encodes", () => {
    const s = fakeStorage();
    saveTermHeight(s, 320.6);
    saveTermCollapsed(s, true);
    expect(s.map.get(TERM_H_KEY)).toBe("321");
    expect(s.map.get(TERM_COLLAPSED_KEY)).toBe("1");
    saveTermCollapsed(s, false);
    expect(s.map.get(TERM_COLLAPSED_KEY)).toBe("0");
  });

  test("a setItem that throws does not throw", () => {
    const s = {
      setItem: () => {
        throw new Error("nope");
      },
    };
    expect(() => saveTermHeight(s, 300)).not.toThrow();
    expect(() => saveTermCollapsed(s, true)).not.toThrow();
  });
});

describe("clampTermHeight", () => {
  test.each([
    [null, 1000, 400],
    [50, 1000, 96],
    [2000, 1000, 850],
    [300, 0, 300],
    [null, 0, 280],
    [null, 200, 96],
  ] as const)("clamp(%p, %p) = %p", (h, parentH, want) => {
    expect(clampTermHeight(h, parentH)).toBe(want);
  });
});

// ── Terminal.vue source guard ──────────────────────────────────────────────

function styleBlocks(src: string): { scoped: boolean; css: string }[] {
  const out: { scoped: boolean; css: string }[] = [];
  const re = /<style(\s[^>]*)?>([\s\S]*?)<\/style>/g;
  for (const m of src.matchAll(re)) {
    out.push({ scoped: /\bscoped\b/.test(m[1] ?? ""), css: m[2] });
  }
  return out;
}

function cssRules(css: string): { selector: string; body: string }[] {
  const out: { selector: string; body: string }[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  for (const m of css.matchAll(re)) {
    out.push({ selector: m[1].trim(), body: m[2] });
  }
  return out;
}

describe("Terminal.vue", () => {
  const src = readFileSync(
    join(import.meta.dir, "../components/Terminal.vue"),
    "utf8",
  );
  const blocks = styleBlocks(src);
  const css = blocks.map((b) => b.css).join("\n");

  test("dark in both themes: scoped style, fixed tones only", () => {
    expect(blocks.length).toBeGreaterThan(0);
    for (const b of blocks) expect(b.scoped).toBe(true);
    expect(css).not.toContain("data-theme");
    expect(css).not.toContain("prefers-color-scheme");
    const names = [...css.matchAll(/var\((--[\w-]+)\)/g)].map((m) => m[1]);
    expect(names.length).toBeGreaterThan(0);
    for (const n of names) expect(n).toMatch(/^--(t-[\w-]+|mono|sans)$/);
    const term = cssRules(css).find((r) => r.selector === ".term");
    expect(term).toBeDefined();
    expect(term?.body).toMatch(/--t-bg:\s*#[0-9a-fA-F]{3,8}/);
    expect(term?.body).toContain("background: var(--t-bg)");
  });

  test("read-only: no input, textarea or contenteditable", () => {
    expect(src).not.toContain("<input");
    expect(src).not.toContain("<textarea");
    expect(src).not.toContain("contenteditable");
  });

  test("mounted only by RunView", () => {
    const root = join(import.meta.dir, "..");
    const files = (readdirSync(root, { recursive: true }) as string[])
      .map((f) => f.split("\\").join("/"))
      .filter((f) => /\.(vue|ts)$/.test(f) && !f.endsWith("terminal.test.ts"));
    const importers = files.filter((f) =>
      /import[^;\n]*Terminal\.vue/.test(readFileSync(join(root, f), "utf8")),
    );
    expect(importers.sort()).toEqual(["components/RunView.vue"]);
  });

  test("wiring", () => {
    for (const needle of [
      "terminalLines(",
      "windowLines(",
      "showEarlier(",
      "onNewLines(",
      "jumpLabel(",
      "emptyText(",
      "loadTermPrefs(",
      "saveTermHeight(",
      "saveTermCollapsed(",
      "clampTermHeight(",
      "Show earlier",
      "❯",
      "removeEventListener",
      "pointercancel",
      "onBeforeUnmount",
      '<slot name="dock"',
      'class="term-main"',
      "defineExpose(",
      "expand",
    ]) {
      expect(src).toContain(needle);
    }
    expect(src).not.toContain("lf-term");
  });
});
