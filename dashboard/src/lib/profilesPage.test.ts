import { describe, expect, test } from "bun:test";
import {
  canvasNote,
  cardLabel,
  colorOf,
  costLabel,
  errorsForRole,
  money,
  nudge,
  popPosition,
  PROFILE_KEYS,
  profileBindings,
  profileCards,
  rosterGrid,
  SHORTCUTS,
  targetProject,
  trayFace,
} from "./profilesPage";
import { estimate, type Draft, type Leg } from "./profiles";
import { createKeymap, type KeyEventLike } from "./keymap";
import type { SessionCost } from "./types";
import type { TrayModel } from "./modelTray";

test("money", () => {
  expect(money(null)).toBe("—");
  expect(money(undefined)).toBe("—");
  expect(money(0)).toBe("<$0.01");
  expect(money(0.004)).toBe("<$0.01");
  expect(money(0.01)).toBe("$0.01");
  expect(money(1.234)).toBe("$1.23");
});

test("costLabel: one figure per block — blank when invalid, free sub, no data, $/task", () => {
  const leg = (over: Partial<Leg>): Leg => ({
    role: "builder",
    runtime: "pi",
    model: "openrouter/x/y",
    thinking: "medium",
    src: { runtime: "agent", model: "agent", thinking: "none" },
    patched: false,
    invalid: null,
    sub: false,
    avg: 1,
    cost: 1,
    rate: 0.9,
    runs: 3,
    median: 1000,
    ...over,
  });
  expect(costLabel(leg({ invalid: "pi model 'x' needs the provider/model-id form" }), true)).toBe("");
  expect(costLabel(leg({ sub: true, cost: 0 }), false)).toBe("sub · free");
  expect(costLabel(leg({ sub: true, cost: 0 }), true)).toBe("<$0.01 / task");
  expect(costLabel(leg({ cost: null }), true)).toBe("no data");
  expect(costLabel(leg({ cost: 1.234 }), true)).toBe("$1.23 / task");
});

test("errorsForRole picks the block's own server messages", () => {
  const errors = [
    "bob: system prompt missing (unconfigured; no package default either)",
    "bob:   pi model 'x' needs the provider/model-id form",
    "profile 'quality' patches unknown agent 'bob'",
    "builder: timeout_s 0 must be a whole number of seconds, at least 1",
  ];
  expect(errorsForRole(errors, "bob")).toEqual([
    "system prompt missing (unconfigured; no package default either)",
    "pi model 'x' needs the provider/model-id form",
  ]);
  expect(errorsForRole(errors, "builder")).toEqual([
    "timeout_s 0 must be a whole number of seconds, at least 1",
  ]);
  expect(errorsForRole(errors, "scout")).toEqual([]);
  expect(errorsForRole(null, "bob")).toEqual([]);
});

test("targetProject resolves 'all' to the first registered project and null when none", () => {
  expect(targetProject("limefactory", ["limefactory", "other"])).toBe("limefactory");
  expect(targetProject("all", ["limefactory", "other"])).toBe("limefactory");
  expect(targetProject("all", [])).toBeNull();
});

test("canvasNote names where an edit lands", () => {
  expect(canvasNote("default")).toBe("roster — edits save to agents:");
  expect(canvasNote("quality")).toBe("profile quality — edits save to its patch");
});

describe("profileCards", () => {
  // fixtures from this repo's config
  const draft: Draft = {
    agents: [
      { name: "scout", color: "#fbbf24", model: "openrouter/qwen/qwen3-coder-flash", thinking: "low" },
      { name: "builder" },
      { name: "reviewer", model: "xai/grok-4.6", thinking: "high" },
    ],
    profiles: {
      quality: { agents: [{ name: "builder", model: "openrouter/anthropic/claude-sonnet-5" }] },
      solo: { agents: [{ name: "scout", model: "haiku" }] },
      blank: { agents: [] },
    },
  };
  const defaults = { runtime: "pi", model: "openrouter/deepseek/deepseek-v4-flash", thinking: "medium" };
  const rows: SessionCost[] = [
    row({ model: "openrouter/qwen/qwen3-coder-flash", role: "scout", api_cost: 2 }),
    row({ model: "haiku", role: "scout", runtime: "claude_code", api_cost: 1 }),
    row({ model: "openrouter/deepseek/deepseek-v4-flash", role: "builder", api_cost: 3 }),
    row({ model: "xai/grok-4.6", role: "reviewer", api_cost: 5 }),
  ];

  function row(partial: Partial<SessionCost> & { model: string }): SessionCost {
    return {
      project: "limefactory",
      run_id: "r1",
      run_status: "success",
      role: "builder",
      runtime: "pi",
      api_cost: 0,
      notional_cost: null,
      tokens: 0,
      duration_ms: null,
      ...partial,
    };
  }

  test("profileCards summarise every profile with default first", () => {
    const cards = profileCards(draft, defaults, rows, true);
    expect(cards.map((c) => c.name)).toEqual(["default", "quality", "solo", "blank"]);
    const def = cards[0];
    expect(def.count).toBe(3);
    expect(def.empty).toBe(false);
    // distinct models in bench order; qwen row cost 2 × .88 (thinking low)
    expect(def.models).toEqual([
      "openrouter/qwen/qwen3-coder-flash",
      "openrouter/deepseek/deepseek-v4-flash",
      "xai/grok-4.6",
    ]);
    expect(def.per).toBeCloseTo((2 * 0.88 + 3 + 5 * 1.28) / 0.95 ** 3, 5);
    expect(cards[3].empty).toBe(true);
    expect(cards[3].models).toEqual([]);
    expect(colorOf({ name: "scout", color: "#fbbf24" })).toBe("#fbbf24");
    expect(colorOf(undefined)).toBe("var(--dim)");
    // estimate agrees with the card's per
    const est = estimate(draft, defaults, "solo", rows, true);
    expect(cards[2].per).toBe(est.per);
  });

  test("profileCards forwards thinking support: a bench with an unsupported level has per null", () => {
    const cards = profileCards(draft, defaults, rows, true, { "openrouter/qwen/qwen3-coder-flash": ["off"] });
    // scout is qwen/low, but only off is supported → invalid, per null
    expect(cards[0].per).toBeNull();
    // The existing 4-arg call still yields a number
    const cards4 = profileCards(draft, defaults, rows, true);
    expect(cards4[0].per).not.toBeNull();
    // cards[2] (solo — scout on haiku with pi runtime) is broken for an unrelated reason
  });
  test("profileCards: cardLabel shows a priced figure, 'no data' without history, 'invalid config' only for a bad slot", () => {
    const d: Draft = {
      ...draft,
      profiles: {
        ...draft.profiles,
        mixed: { agents: [{ name: "scout" }, { name: "builder", model: "openrouter/anthropic/claude-sonnet-5" }] },
      },
    };
    const cards = profileCards(d, defaults, rows, true);
    const by = Object.fromEntries(cards.map((c) => [c.name, c]));
    // fully priced roster — figure unchanged from the existing test
    expect(cardLabel(by.default)).toBe("$13.02 / task");
    // partly priced: scout (qwen, $2 × .88 @ .95) priced, builder never run
    expect(by.mixed.broken).toBe(false);
    expect(by.mixed.per).toBeCloseTo(1.76 / 0.95, 5);
    expect(cardLabel(by.mixed)).toBe("$1.85 / task");
    // nothing priced: valid slot, no ledger rows
    expect(by.quality.broken).toBe(false);
    expect(by.quality.per).toBeNull();
    expect(cardLabel(by.quality)).toBe("no data");
    // genuinely invalid: haiku resolves to runtime pi → validateSlot fails
    expect(by.solo.broken).toBe(true);
    expect(by.solo.per).toBeNull();
    expect(cardLabel(by.solo)).toBe("invalid config");
    // empty bench wins over everything
    expect(cardLabel(by.blank)).toBe("empty bench");
    // the state-only contract, independent of estimate
    const base = { name: "x", count: 1, empty: false, broken: false, per: 0.005, models: [] };
    expect(cardLabel({ ...base, per: null })).toBe("no data");
    expect(cardLabel({ ...base, broken: true, per: null })).toBe("invalid config");
    expect(cardLabel(base)).toBe("<$0.01 / task");
  });
});

test("trayFace shows 8 chips until a query reveals the catalog", () => {
  const models: TrayModel[] = Array.from({ length: 12 }, (_, i) => ({
    id: `m${i}`,
    name: `m${i}`,
    provider: "p",
    cc: false,
    runs: 0,
    avg: null,
    note: "",
    group: "catalog" as const,
  }));
  expect(trayFace(models, "")).toHaveLength(8);
  expect(trayFace(models, "  ")).toHaveLength(8);
  // a query only switches off the cap — filtering happens in filterTray
  expect(trayFace(models, "m9")).toHaveLength(12);
  expect(trayFace(models, "m")).toHaveLength(12);
});

test("nudge moves by the grid, ×4 with shift, never negative", () => {
  const pos = { x: 66, y: 66 };
  expect(nudge(pos, "ArrowRight", false)).toEqual({ x: 66 + 22, y: 66 });
  expect(nudge(pos, "ArrowLeft", false)).toEqual({ x: 44, y: 66 });
  expect(nudge(pos, "ArrowDown", true)).toEqual({ x: 66, y: 66 + 88 });
  expect(nudge(pos, "ArrowUp", true)).toEqual({ x: 66, y: 0 });
  expect(nudge({ x: 10, y: 10 }, "ArrowLeft", false)).toEqual({ x: 0, y: 10 });
  expect(nudge({ x: 10, y: 10 }, "ArrowUp", false)).toEqual({ x: 10, y: 0 });
  expect(nudge(pos, "a", false)).toBeNull();
  expect(nudge(pos, "Enter", true)).toBeNull();
  // the input pos is never mutated
  expect(pos).toEqual({ x: 66, y: 66 });
});
test("popPosition clamps to the viewport and flips above a low anchor", () => {
  // fits below the anchor → anchor.left, anchor.bottom + 6
  expect(popPosition({ left: 100, right: 120, top: 100, bottom: 130 }, 340, 300, 1440, 900)).toEqual({
    left: 100,
    top: 136,
  });
  // clamped into the left edge
  expect(popPosition({ left: 2, right: 30, top: 40, bottom: 60 }, 340, 300, 1440, 900).left).toBe(8);
  // clamped against the right edge
  expect(
    popPosition({ left: 1400, right: 1430, top: 40, bottom: 60 }, 340, 300, 1440, 900).left,
  ).toBe(1440 - 340 - 8);
  // would not fit below → flipped above the anchor
  const low = popPosition({ left: 100, right: 400, top: 800, bottom: 880 }, 340, 300, 1440, 900);
  expect(low.top).toBe(800 - 300 - 6);
  expect(low.left).toBe(100);
  // flip still stays inside the top edge
  expect(popPosition({ left: 100, right: 400, top: 1, bottom: 890 }, 340, 300, 1440, 900).top).toBe(8);
});

test("rosterGrid lists the roster in chain order and flags roles on the bench", () => {
  const draft: Draft = {
    agents: [
      { name: "builder", purpose: "Implement the plan.", color: "#60a5fa" },
      { name: "scout", purpose: "Find where things live.", color: "#fbbf24" },
      { name: "documenter", purpose: "Write it down." },
    ],
    profiles: {
      quality: { agents: [{ name: "scout", model: "haiku" }] },
    },
  };
  const grid = rosterGrid(draft, "quality");
  // ROLE_ORDER members first in their order, then the rest as given
  expect(grid.map((c) => c.name)).toEqual(["scout", "builder", "documenter"]);
  const scout = grid[0];
  expect(scout.color).toBe("#fbbf24");
  expect(scout.purpose).toBe("Find where things live.");
  expect(scout.onBench).toBe(true);
  expect(grid[1].onBench).toBe(false);
  // an agent without an explicit color falls back to the dim token
  expect(grid[2].color).toBe("var(--dim)");
  // under default the whole roster is on the bench
  expect(rosterGrid(draft, "default").every((c) => c.onBench)).toBe(true);
});

test("SHORTCUTS map the proto keys", () => {
  expect(SHORTCUTS).toEqual({ p: "profiles", m: "tray", a: "add", n: "new", "/": "search" });
});

// ── sync helpers (step 3) ───────────────────────────────────────────────────

import {
  applySyncResults,
  coalesceChanges,
  pendingChangesKey,
  removePendingChange,
  rekeyPendingChange,
  selectedExtraCount,
  syncStatusLabel,
  syncTargetsInit,
  toggleSyncTarget,
  type PendingChange,
  type SyncTarget,
} from "./profilesPage";
import type { ProfileModelChange, ProfileSyncResult } from "./types";

describe("syncTargetsInit", () => {
  test("current project is selected, others opt-in", () => {
    const targets = syncTargetsInit("alpha", ["alpha", "beta", "gamma"]);
    expect(targets).toHaveLength(3);
    expect(targets[0]).toEqual({ name: "alpha", selected: true, status: "not-attempted" });
    expect(targets[1]).toEqual({ name: "beta", selected: false, status: "not-attempted" });
    expect(targets[2]).toEqual({ name: "gamma", selected: false, status: "not-attempted" });
  });

  test("current project not in list — still selected", () => {
    const targets = syncTargetsInit("source", ["alpha", "beta"]);
    expect(targets[0].name).toBe("alpha");
    expect(targets[0].selected).toBe(false);
  });
});

describe("toggleSyncTarget", () => {
  const base: SyncTarget[] = [
    { name: "a", selected: true, status: "not-attempted" },
    { name: "b", selected: false, status: "not-attempted" },
  ];

  test("toggles an opt-in target on/off", () => {
    expect(toggleSyncTarget(base, "b", "a")[1].selected).toBe(true);
    const toggled = toggleSyncTarget(base, "b", "a");
    expect(toggleSyncTarget(toggled, "b", "a")[1].selected).toBe(false);
  });

  test("current project stays selected no matter what", () => {
    expect(toggleSyncTarget(base, "a", "a")).toEqual(base);
  });

  test("resets status on toggle", () => {
    const withStatus: SyncTarget[] = [
      { name: "a", selected: true, status: "not-attempted" },
      { name: "b", selected: false, status: "error", message: "fail" },
    ];
    const result = toggleSyncTarget(withStatus, "b", "a");
    expect(result[1].selected).toBe(true);
    expect(result[1].status).toBe("not-attempted");
    expect(result[1].message).toBeUndefined();
  });
});

describe("pendingChangesKey", () => {
  test("profile+role for role changes, profile+defaults for defaults", () => {
    expect(pendingChangesKey("default", "builder")).toBe("default/builder");
    expect(pendingChangesKey("quality", null)).toBe("quality/defaults");
  });
});

describe("coalesceChanges", () => {
  const change1: ProfileModelChange = { profile: "default", role: "builder", patch: { model: "m1", runtime: "pi" } };
  const change2: ProfileModelChange = { profile: "default", role: "builder", patch: { model: "m2", runtime: "pi" } };
  const change3: ProfileModelChange = { profile: "quality", role: null, patch: { model: "m3", runtime: "pi" } };

  test("adds a new change", () => {
    const result = coalesceChanges([], change1);
    expect(result).toHaveLength(1);
    expect(result[0].key).toBe("default/builder");
    expect(result[0].change).toBe(change1);
  });

  test("replaces an existing change with the same key", () => {
    const withOne = coalesceChanges([], change1);
    const result = coalesceChanges(withOne, change2);
    expect(result).toHaveLength(1);
    expect(result[0].change).toBe(change2);
  });

  test("keeps different keys separate", () => {
    const one = coalesceChanges([], change1);
    const two = coalesceChanges(one, change3);
    expect(two).toHaveLength(2);
    expect(two[0].key).toBe("default/builder");
    expect(two[1].key).toBe("quality/defaults");
  });
});

describe("removePendingChange", () => {
  test("removes by profile+role", () => {
    const change1: ProfileModelChange = { profile: "default", role: "builder", patch: { model: "m1", runtime: "pi" } };
    const change2: ProfileModelChange = { profile: "default", role: "scout", patch: { model: "m2", runtime: "pi" } };
    const pending: PendingChange[] = [
      { key: "default/builder", change: change1 },
      { key: "default/scout", change: change2 },
    ];
    const result = removePendingChange(pending, "default", "builder");
    expect(result).toHaveLength(1);
    expect(result[0].key).toBe("default/scout");
  });

  test("removes by profile+null (defaults)", () => {
    const change: ProfileModelChange = { profile: "quality", role: null, patch: { model: "m1", runtime: "pi" } };
    const pending: PendingChange[] = [{ key: "quality/defaults", change }];
    expect(removePendingChange(pending, "quality", null)).toHaveLength(0);
  });

  test("unknown key leaves pending unchanged", () => {
    const change: ProfileModelChange = { profile: "default", role: "builder", patch: { model: "m1", runtime: "pi" } };
    const pending: PendingChange[] = [{ key: "default/builder", change }];
    expect(removePendingChange(pending, "default", "ghost")).toHaveLength(1);
  });
});

describe("rekeyPendingChange", () => {
  test("re-keys a change when a role is renamed", () => {
    const change: ProfileModelChange = { profile: "default", role: "builder", patch: { model: "m1", runtime: "pi" } };
    const pending: PendingChange[] = [{ key: "default/builder", change }];
    const result = rekeyPendingChange(pending, "default", "builder", "bob");
    expect(result).toHaveLength(1);
    expect(result[0].key).toBe("default/bob");
    expect(result[0].change.role).toBe("bob");
  });

  test("non-matching key is left unchanged", () => {
    const change: ProfileModelChange = { profile: "default", role: "scout", patch: { model: "m1", runtime: "pi" } };
    const pending: PendingChange[] = [{ key: "default/scout", change }];
    const result = rekeyPendingChange(pending, "default", "builder", "bob");
    expect(result).toHaveLength(1);
    expect(result[0].key).toBe("default/scout");
  });
});

describe("applySyncResults", () => {
  const base: SyncTarget[] = [
    { name: "a", selected: true, status: "not-attempted" },
    { name: "b", selected: true, status: "not-attempted" },
    { name: "c", selected: false, status: "not-attempted" },
  ];

  test("maps ok+wrote to success", () => {
    const results: ProfileSyncResult[] = [
      { project: "a", ok: true, wrote: true, errors: [] },
      { project: "b", ok: true, wrote: false, errors: [] },
    ];
    const updated = applySyncResults(base, results);
    expect(updated[0].status).toBe("success");
    expect(updated[1].status).toBe("noop");
    expect(updated[2].status).toBe("not-attempted"); // not in results
  });

  test("maps ok=false to error with message", () => {
    const results: ProfileSyncResult[] = [
      { project: "a", ok: false, wrote: false, errors: ["unknown role"] },
    ];
    const updated = applySyncResults(base, results);
    expect(updated[0].status).toBe("error");
    expect(updated[0].message).toBe("unknown role");
  });

  test("multiple errors joined by semicolon", () => {
    const results: ProfileSyncResult[] = [
      { project: "a", ok: false, wrote: false, errors: ["err1", "err2"] },
    ];
    const updated = applySyncResults(base, results);
    expect(updated[0].message).toBe("err1; err2");
  });
});

describe("syncStatusLabel", () => {
  test("returns the correct label for each status", () => {
    expect(syncStatusLabel("not-attempted")).toBe("not attempted");
    expect(syncStatusLabel("saving")).toBe("saving…");
    expect(syncStatusLabel("success")).toBe("saved ✓");
    expect(syncStatusLabel("noop")).toBe("no change");
    expect(syncStatusLabel("error")).toBe("failed ✗");
  });
});

describe("selectedExtraCount", () => {
  test("counts targets beyond the current project", () => {
    const targets: SyncTarget[] = [
      { name: "a", selected: true, status: "not-attempted" },
      { name: "b", selected: true, status: "not-attempted" },
      { name: "c", selected: false, status: "not-attempted" },
    ];
    expect(selectedExtraCount(targets)).toBe(1); // only b is extra
  });

  test("zero when only current is selected", () => {
    const targets: SyncTarget[] = [
      { name: "a", selected: true, status: "not-attempted" },
      { name: "b", selected: false, status: "not-attempted" },
    ];
    expect(selectedExtraCount(targets)).toBe(0);
  });
});

function evt(o: {
  key?: string;
  code?: string;
  target?: unknown;
}): KeyEventLike & { preventCount: number; stopCount: number } {
  const e = {
    key: o.key ?? "",
    code: o.code ?? "",
    altKey: false,
    metaKey: false,
    ctrlKey: false,
    target: o.target ?? null,
    preventCount: 0,
    stopCount: 0,
    preventDefault() {
      e.preventCount += 1;
    },
    stopPropagation() {
      e.stopCount += 1;
    },
  };
  return e;
}

describe("profileBindings", () => {
  test("single keys do nothing while an input has focus", () => {
    const m = createKeymap();
    let calls = 0;
    m.register(profileBindings(() => { calls += 1; }));
    for (const key of PROFILE_KEYS) {
      for (const target of [
        { tagName: "INPUT" },
        { tagName: "TEXTAREA" },
        { tagName: "SELECT" },
        { tagName: "DIV", isContentEditable: true },
      ]) {
        const e = evt({ key, target });
        expect(m.dispatch(e)).toBe(false);
        expect(e.preventCount).toBe(0);
      }
    }
    expect(calls).toBe(0);
  });

  test("run from the page body", () => {
    const m = createKeymap();
    let calls = 0;
    m.register(profileBindings(() => { calls += 1; }));
    for (const key of ["p", "a", "Delete", "ArrowUp"]) {
      const e = evt({ key, target: { tagName: "BODY" } });
      m.dispatch(e);
      expect(e.preventCount).toBe(0);
    }
    expect(calls).toBe(4);
  });

  test("Escape is the shell's", () => {
    expect(PROFILE_KEYS).not.toContain("Escape");
    for (const key of Object.keys(SHORTCUTS)) {
      expect(PROFILE_KEYS).toContain(key);
    }
  });
});
