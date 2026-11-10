import { describe, expect, test } from "bun:test";
import {
  addRole,
  addRoleWithChange,
  agentByName,
  benchRoles,
  clampThinking,
  clearDefaults,
  createProfile,
  defaultsModelChange,
  deleteProfile,
  estimate,
  levelsFor,
  modelChange,
  modelPatch,
  orderRoles,
  profileNames,
  renameProfile,
  renameRole,
  removeRole,
  resolveRole,
  sessionAvg,
  setDefaults,
  setRole,
  stepThinking,
  validateSlot,
  THINKING,
  type Draft,
} from "./profiles";
import type { AgentEntry, SessionCost } from "./types";

// ── fixtures: this repo's .lime/config.yaml, verbatim shapes ────────────────

const CONFIG_DEFAULTS = {
  runtime: "pi",
  model: "openrouter/deepseek/deepseek-v4-flash",
  thinking: "medium",
};

function agent(
  name: string,
  extra: Record<string, unknown>,
  color: string,
  tools: string[],
): AgentEntry {
  return {
    name,
    purpose: `${name} purpose — kept verbatim`,
    color,
    prompt_engineering: {
      system: `.lime/prompts/${name}/system.md`,
      user: `.lime/prompts/${name}/user.md`,
    },
    writes: [],
    tools,
    ...extra,
  };
}

function makeAgents(): AgentEntry[] {
  return [
    agent("scout", { model: "openrouter/qwen/qwen3-coder-flash", thinking: "low" }, "#fbbf24", ["read", "grep", "find", "ls", "bash", "write"]),
    agent("researcher", { runtime: "claude_code", model: "sonnet", thinking: "low", harness_engineering: ["lime/harness_engineering/context7.ts"] }, "#34d399", ["read", "grep", "find", "ls", "bash", "write", "web"]),
    agent("planner", { runtime: "claude_code", model: "claude-fable-5-1", thinking: "xhigh" }, "#a78bfa", ["read", "grep", "find", "bash", "write", "subagent"]),
    agent("builder", {}, "#22d3ee", ["read", "grep", "find", "ls", "bash", "edit", "write"]),
    agent("reviewer", { model: "xai/grok-4.6", thinking: "high" }, "#fb7185", ["read", "grep", "find", "ls", "write"]),
    agent("documenter", { thinking: "low" }, "#e879f9", ["read", "grep", "find", "ls", "bash", "write", "edit"]),
    agent("designer", { model: "openrouter/moonshotai/kimi-k3", thinking: "medium" }, "#c1f610", ["read", "grep", "find", "ls", "bash", "edit", "write"]),
  ];
}

function makeProfiles() {
  return {
    quality: {
      defaults: { thinking: "high" },
      agents: [{ name: "builder", model: "openrouter/anthropic/claude-sonnet-5" }],
    },
    "all-claude": {
      defaults: { runtime: "claude_code", model: "sonnet" },
      agents: [
        { name: "scout", model: "haiku" },
        { name: "designer", model: "opus" },
        { name: "reviewer", model: "fable" },
      ],
    },
    "api-research": {
      agents: [{ name: "researcher", runtime: "pi", model: "openrouter/google/gemini-3.7-flash" }],
    },
    "api-fallback": {
      agents: [
        { name: "planner", runtime: "pi", model: "openrouter/meta/muse-spark-1.3", thinking: "xhigh" },
        { name: "builder", runtime: "pi", model: "openrouter/z-ai/glm-5.3-flash", thinking: "high" },
        { name: "reviewer", runtime: "pi", model: "openrouter/google/gemini-3.8-flash", thinking: "high" },
      ],
    },
  };
}

const makeDraft = (): Draft => ({ agents: makeAgents(), profiles: makeProfiles() });

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

describe("resolveRole", () => {
  test("resolveRole: an agent's own model beats a profile's defaults (all-claude scout → qwen on claude_code)", () => {
    const d = makeDraft();
    // all-claude without the scout patch: the entry's model must beat the
    // profile's defaults (the config.py order — the proto had it backwards).
    d.profiles["all-claude"] = {
      defaults: { runtime: "claude_code", model: "sonnet" },
      agents: [{ name: "designer", model: "opus" }, { name: "reviewer", model: "fable" }],
    };
    const r = resolveRole(d, CONFIG_DEFAULTS, "all-claude", "scout");
    expect(r.model).toBe("openrouter/qwen/qwen3-coder-flash");
    expect(r.src.model).toBe("agent");
    expect(r.runtime).toBe("claude_code");
    expect(r.src.runtime).toBe("profile");
    // which is exactly why the real profile patches scout → haiku:
    expect(r.invalid).toBe(
      "claude_code model 'openrouter/qwen/qwen3-coder-flash' is no alias (haiku, sonnet, opus, fable) or claude-* id",
    );
  });

  test("resolveRole: patch beats the entry, profile defaults beat config defaults, src names each layer", () => {
    const d = makeDraft();
    const r = resolveRole(d, CONFIG_DEFAULTS, "all-claude", "scout");
    // patch: model haiku beats the entry's qwen
    expect(r.model).toBe("haiku");
    expect(r.src.model).toBe("patch");
    expect(r.patched).toBe(true);
    // entry: the scout entry's own thinking wins over profile/config defaults
    expect(r.thinking).toBe("low");
    expect(r.src.thinking).toBe("agent");
    // profile defaults: runtime claude_code beats the config default pi
    expect(r.runtime).toBe("claude_code");
    expect(r.src.runtime).toBe("profile");

    const api = resolveRole(d, CONFIG_DEFAULTS, "api-research", "researcher");
    expect(api.model).toBe("openrouter/google/gemini-3.7-flash");
    expect(api.runtime).toBe("pi");
    expect(api.src.runtime).toBe("patch");

    const defs = resolveRole(d, CONFIG_DEFAULTS, "quality", "builder");
    expect(defs.model).toBe("openrouter/anthropic/claude-sonnet-5");
    expect(defs.thinking).toBe("high");
    expect(defs.src.thinking).toBe("profile");
    expect(defs.runtime).toBe("pi");
    expect(defs.src.runtime).toBe("config");
  });

  test("resolveRole under default falls back to config defaults with src config; unset thinking is medium/none", () => {
    const d = makeDraft();
    const r = resolveRole(d, CONFIG_DEFAULTS, "default", "builder");
    expect(r.runtime).toBe("pi");
    expect(r.src.runtime).toBe("config");
    expect(r.model).toBe("openrouter/deepseek/deepseek-v4-flash");
    expect(r.src.model).toBe("config");
    expect(r.thinking).toBe("medium");
    expect(r.src.thinking).toBe("config"); // the config default sets medium
    expect(r.patched).toBe(false);
    expect(r.invalid).toBeNull();
    // nothing set anywhere → medium from nowhere, src none, empty runtime/model
    const d2 = makeDraft();
    const r2 = resolveRole(d2, {}, "default", "builder");
    expect(r2.thinking).toBe("medium");
    expect(r2.src.thinking).toBe("none");
    expect(r2.src.runtime).toBe("none");
    expect(r2.src.model).toBe("none");
    expect(r2.runtime).toBe("");
    expect(r2.model).toBe("");
    expect(r2.invalid).toBe("runtime '' is neither pi nor claude_code");
  });
});

describe("validateSlot", () => {
  test("validateSlot names each rule in the server's words", () => {
    expect(validateSlot({ runtime: "cli", model: "x" })).toBe(
      "runtime 'cli' is neither pi nor claude_code",
    );
    expect(validateSlot({ runtime: "pi" })).toBe(
      "no model resolves (defaults set none either)",
    );
    expect(validateSlot({ runtime: "pi", model: "deepseek-v4" })).toBe(
      "pi model 'deepseek-v4' needs the provider/model-id form",
    );
    expect(validateSlot({ runtime: "pi", model: "anthropic/claude-sonnet-5" })).toBe(
      "anthropic/* on pi would spend the subscription through a third-party harness — use openrouter/anthropic/*",
    );
    expect(validateSlot({ runtime: "claude_code", model: "gpt-5" })).toBe(
      "claude_code model 'gpt-5' is no alias (haiku, sonnet, opus, fable) or claude-* id",
    );
    expect(validateSlot({ runtime: "pi", model: "x/y", thinking: "ultra" })).toBe(
      "thinking 'ultra' is not one of (off, minimal, low, medium, high, xhigh, max)",
    );
    expect(validateSlot({ runtime: "pi", model: "x/y", thinking: "high" })).toBeNull();
    expect(validateSlot({ runtime: "claude_code", model: "claude-fable-5-1" })).toBeNull();
    expect(validateSlot({ runtime: "claude_code", model: "fable" })).toBeNull();
  });
});

describe("bench", () => {
  test("benchRoles orders by the chain and appends unknown roles; missing profile is empty", () => {
    const d = makeDraft();
    expect(benchRoles(d, "default")).toEqual([
      "scout", "researcher", "planner", "builder", "reviewer", "documenter", "designer",
    ]);
    d.profiles.custom = {
      agents: [{ name: "zz-custom" }, { name: "builder" }, { name: "scout" }],
    };
    expect(benchRoles(d, "custom")).toEqual(["scout", "builder", "zz-custom"]);
    expect(benchRoles(d, "nope")).toEqual([]);
    expect(orderRoles(["zz", "reviewer", "scout", "zz", "planner"])).toEqual([
      "scout", "planner", "reviewer", "zz",
    ]);
    expect(profileNames(d)[0]).toBe("default");
  });
});

describe("mutations", () => {
  test("setRole under default mutates the entry in place, null deletes, purpose/tools untouched", () => {
    const d = makeDraft();
    const scout = d.agents[0];
    setRole(d, "default", "scout", { thinking: "off" });
    expect(d.agents[0]).toBe(scout); // mutated in place, not replaced
    expect(scout.thinking).toBe("off");
    expect(scout.purpose).toBe("scout purpose — kept verbatim");
    expect(scout.tools).toEqual(["read", "grep", "find", "ls", "bash", "write"]);
    expect(scout.model).toBe("openrouter/qwen/qwen3-coder-flash");
    setRole(d, "default", "scout", { thinking: null, model: null });
    expect(scout.thinking).toBeUndefined();
    expect(scout.model).toBeUndefined();
    expect(scout.purpose).toBeDefined();
    const before = d.agents.length;
    setRole(d, "default", "ghost", { model: "x/y" });
    expect(d.agents.length).toBe(before);
  });

  test("setRole under a profile upserts exactly one patch; emptied of overrides it stays a name-only member", () => {
    const d = makeDraft();
    d.profiles.extra = { agents: [] };
    setRole(d, "extra", "builder", { model: "openrouter/x/y" });
    setRole(d, "extra", "builder", { thinking: "high" });
    expect(d.profiles.extra.agents?.length).toBe(1);
    expect(d.profiles.extra.agents?.[0]).toEqual({
      name: "builder",
      model: "openrouter/x/y",
      thinking: "high",
    });
    setRole(d, "extra", "builder", { model: null, thinking: null });
    expect(d.profiles.extra.agents).toEqual([{ name: "builder" }]); // ticket 89: still on the bench
    expect(benchRoles(d, "extra")).toEqual(["builder"]);
    removeRole(d, "extra", "builder");
    expect(d.profiles.extra.agents).toEqual([]);
  });

  test("renameRole under default renames the entry and every patch naming it", () => {
    const d = makeDraft();
    expect(renameRole(d, "default", "builder", "  bob  ")).toBe(true);
    expect(d.agents.find((a) => a.name === "bob")).toBeDefined();
    expect(d.agents.find((a) => a.name === "builder")).toBeUndefined();
    expect(d.profiles.quality.agents?.[0].name).toBe("bob");
    expect(d.profiles["api-fallback"].agents?.[1].name).toBe("bob");
    expect(d.profiles["api-fallback"].agents?.[0].name).toBe("planner");
  });

  test("renameRole under a profile replaces an existing target patch — no duplicate — and refuses blank/unchanged", () => {
    const d = makeDraft();
    expect(renameRole(d, "all-claude", "scout", "designer")).toBe(true);
    const names = d.profiles["all-claude"].agents?.map((a) => a.name);
    expect(names).toEqual(["designer", "reviewer"]); // designer patch replaced
    expect(d.profiles["all-claude"].agents?.[0].model).toBe("haiku");
    expect(renameRole(d, "all-claude", "designer", "   ")).toBe(false);
    expect(renameRole(d, "all-claude", "designer", "designer")).toBe(false);
    expect(renameRole(d, "api-research", "nobody", "someone")).toBe(false);
  });

  test("removeRole drops the entry and its patches under default, only the patch under a profile", () => {
    const d = makeDraft();
    removeRole(d, "default", "builder");
    expect(d.agents.find((a) => a.name === "builder")).toBeUndefined();
    expect(d.profiles.quality.agents).toEqual([]); // patch naming builder dropped
    expect(d.profiles["api-fallback"].agents?.some((a) => a.name === "builder")).toBe(false);
    expect(d.agents.length).toBe(6);

    const d2 = makeDraft();
    removeRole(d2, "api-fallback", "builder");
    expect(d2.agents.find((a) => a.name === "builder")).toBeDefined(); // roster kept
    expect(d2.profiles["api-fallback"].agents?.map((a) => a.name)).toEqual([
      "planner", "reviewer",
    ]);
  });

  test("addRole under a profile picks the runtime from the model id; no-op under default", () => {
    const d = makeDraft();
    addRole(d, "quality", "documenter", "openrouter/x/y");
    expect(d.profiles.quality.agents?.[1]).toEqual({
      name: "documenter",
      model: "openrouter/x/y",
      runtime: "pi",
    });
    addRole(d, "quality", "planner", "sonnet");
    expect(d.profiles.quality.agents?.[2]).toEqual({
      name: "planner",
      model: "sonnet",
      runtime: "claude_code",
    });
    const before = structuredClone(d.agents);
    addRole(d, "default", "ghost", "x/y");
    expect(d.agents).toEqual(before);
  });

  test("ticket 89: a role added at the agent's own defaults is a member — the server's name-only echo stays on the bench and resolves to the agent's values", () => {
    const d = makeDraft();
    addRole(d, "all-claude", "planner", "claude-fable-5-1");
    expect(benchRoles(d, "all-claude")).toEqual(["scout", "planner", "reviewer", "designer"]);
    // what `lime profiles set` hands back: the overrides equalled the roster, only the name survives
    d.profiles["all-claude"].agents = (d.profiles["all-claude"].agents ?? []).map((a) =>
      a.name === "planner" ? { name: "planner" } : a,
    );
    expect(benchRoles(d, "all-claude")).toEqual(["scout", "planner", "reviewer", "designer"]);
    const r = resolveRole(d, CONFIG_DEFAULTS, "all-claude", "planner");
    expect([r.runtime, r.model, r.thinking]).toEqual(["claude_code", "claude-fable-5-1", "xhigh"]);
    expect(r.src).toEqual({ runtime: "agent", model: "agent", thinking: "agent" });
    expect(r.invalid).toBeNull();
    removeRole(d, "all-claude", "planner");
    expect(benchRoles(d, "all-claude")).toEqual(["scout", "reviewer", "designer"]);
  });

  test("setDefaults edits the profile's defaults; clearDefaults wipes them; default/unknown are no-ops", () => {
    const d = makeDraft();
    setDefaults(d, "quality", { model: "opus" });
    expect(d.profiles.quality.defaults).toEqual({ thinking: "high", model: "opus" });
    setDefaults(d, "quality", { thinking: null });
    expect(d.profiles.quality.defaults).toEqual({ model: "opus" });
    setDefaults(d, "quality", { model: null });
    expect(d.profiles.quality.defaults).toBeUndefined();
    setDefaults(d, "default", { model: "opus" });
    setDefaults(d, "ghost", { model: "opus" });
    expect(d.profiles.quality).toEqual({ agents: [{ name: "builder", model: "openrouter/anthropic/claude-sonnet-5" }] });
    const d2 = makeDraft();
    setDefaults(d2, "quality", { thinking: "low" });
    clearDefaults(d2, "quality");
    expect(d2.profiles.quality.defaults).toBeUndefined();
    clearDefaults(d2, "default");
    expect(CONFIG_DEFAULTS.model).toBe("openrouter/deepseek/deepseek-v4-flash");
  });
});

describe("profiles CRUD", () => {
  test("createProfile slugs, falls back to new-profile, returns an existing id untouched, copies", () => {
    const d = makeDraft();
    expect(createProfile(d, "Fast Lane!")).toBe("fast-lane");
    expect(d.profiles["fast-lane"]).toEqual({ agents: [] });
    expect(createProfile(d, "fast lane")).toBe("fast-lane");
    expect(Object.keys(d.profiles).filter((k) => k === "fast-lane").length).toBe(1);
    expect(createProfile(d, "!!!")).toBe("new-profile");
    const id = createProfile(d, "copy", "quality");
    expect(id).toBe("copy");
    expect(d.profiles.copy).toEqual(structuredClone(makeProfiles().quality));
    d.profiles.copy.agents?.push({ name: "mutant" });
    expect(d.profiles.quality.agents?.length).toBe(1);
    expect(createProfile(d, "x", "default")).toBe("x");
    expect(d.profiles.x).toEqual({ agents: [] });
  });

  test("deleteProfile and renameProfile", () => {
    const d = makeDraft();
    deleteProfile(d, "quality");
    expect(d.profiles.quality).toBeUndefined();
    expect(renameProfile(d, "all-claude", "Only Claude")).toBe("only-claude");
    expect(d.profiles["only-claude"]).toBeDefined();
    expect(d.profiles["all-claude"]).toBeUndefined();
    expect(renameProfile(d, "api-research", "api-fallback")).toBe("api-research"); // target exists
    expect(renameProfile(d, "api-research", "!!!")).toBe("api-research"); // empty slug
  });
});

test("stepThinking wraps both ways and treats unknown as medium", () => {
  expect(stepThinking("off", -1)).toBe("max");
  expect(stepThinking("max", 1)).toBe("off");
  expect(stepThinking("low", 1)).toBe("medium");
  expect(stepThinking("medium", -1)).toBe("low");
  expect(stepThinking("weird", 1)).toBe("high");
  expect(stepThinking("weird", -1)).toBe("low");
  expect(stepThinking("medium", 0)).toBe("medium");
});

test("sessionAvg prefers the role's rows and falls back to the model", () => {
  const rows: SessionCost[] = [
    row({ model: "m", role: "scout", api_cost: 1 }),
    row({ model: "m", role: "scout", api_cost: 3, notional_cost: 0 }),
    row({ model: "m", role: "builder", api_cost: 10, notional_cost: 2 }),
  ];
  expect(sessionAvg(rows, "m", "scout")).toBe(2);
  expect(sessionAvg(rows, "m", "builder")).toBe(12); // 10 + notional 2
  expect(sessionAvg(rows, "m", "ghost")).toBe(16 / 3);
  expect(sessionAvg(rows, "m")).toBe(16 / 3);
  expect(sessionAvg(rows, "z", "scout")).toBeNull();
});

describe("estimate", () => {
  const estDraft = (): Draft => ({
    agents: [
      { name: "a", runtime: "claude_code", model: "sonnet", thinking: "low" },
      { name: "b", runtime: "pi", model: "openrouter/x/y", thinking: "medium" },
      { name: "c", runtime: "pi", model: "no-slash" },
    ],
    profiles: { onlyb: { agents: [{ name: "b" }] } },
  });
  const estRows: SessionCost[] = [
    row({ model: "sonnet", role: "a", runtime: "claude_code", api_cost: 2, duration_ms: 1000 }),
    row({ model: "openrouter/x/y", role: "b", api_cost: 3, notional_cost: 1, duration_ms: 2000 }),
  ];

  test("estimate: per = Σcost ÷ P(all); sub legs are free with notional off; an invalid leg breaks it; empty bench", () => {
    const d = estDraft();
    const est = estimate(d, CONFIG_DEFAULTS, "default", estRows, true);
    expect(est.legs.length).toBe(3);
    // sonnet: 2 sessions of $2 → avg 2 × THINK_COST.low .88 = 1.76; rate 1 → clamp .95
    expect(est.legs[0].cost).toBeCloseTo(1.76);
    expect(est.legs[0].sub).toBe(true);
    expect(est.legs[0].rate).toBe(0.95);
    // openrouter/x/y: session = 3 + notional 1 = 4 × 1
    expect(est.legs[1].cost).toBe(4);
    expect(est.legs[1].sub).toBe(false);
    expect(est.legs[1].median).toBe(2000);
    // no-slash on pi → invalid, cost null
    expect(est.legs[2].invalid).toBe("pi model 'no-slash' needs the provider/model-id form");
    expect(est.legs[2].cost).toBeNull();
    expect(est.legs[2].rate).toBe(0.5);
    expect(est.broken).toBe(true);
    expect(est.per).toBeNull();
    expect(est.sum).toBeCloseTo(5.76);
    expect(est.wall).toBe(1000 * 0.9 + 2000 * 1);

    // sub legs are free with notional off → only pi legs cost
    const notionalOff = estimate(d, CONFIG_DEFAULTS, "default", estRows, false);
    expect(notionalOff.legs[0].cost).toBe(0);
    expect(notionalOff.legs[1].cost).toBe(4);
    expect(notionalOff.sum).toBe(4);

    // a bench of one clean leg is fine: per = Σ ÷ P(all)
    const ok = estimate(d, CONFIG_DEFAULTS, "onlyb", estRows, true);
    expect(ok.empty).toBe(false);
    expect(ok.broken).toBe(false);
    expect(ok.pAll).toBe(0.95);
    expect(ok.per).toBeCloseTo(4 / 0.95);

    // empty bench
    const d2 = estDraft();
    d2.profiles.none = { agents: [] };
    const empty = estimate(d2, CONFIG_DEFAULTS, "none", estRows, true);
    expect(empty.empty).toBe(true);
    expect(empty.broken).toBe(true);
    expect(empty.per).toBeNull();
    expect(empty.legs).toEqual([]);
  });

  test("estimate: an unpriced leg is not broken — per is the priced legs' figure; none priced → no per; an invalid slot alone breaks", () => {
    const d = estDraft();
    d.agents.push({ name: "z", runtime: "pi", model: "openrouter/x/z", thinking: "medium" }); // valid, no rows
    d.profiles.partly = { agents: [{ name: "b" }, { name: "z" }] };
    d.profiles.nodata = { agents: [{ name: "z" }] };
    d.profiles.bad = { agents: [{ name: "c" }] };

    // fully priced — unchanged
    const full = estimate(d, CONFIG_DEFAULTS, "onlyb", estRows, true);
    expect(full.broken).toBe(false);
    expect(full.per).toBeCloseTo(4 / 0.95);

    // partly priced: z has no ledger rows → cost null, rate falls to the .5 default,
    // but the bench is not broken and per comes from b alone (÷ b's rate, not pAll)
    const partly = estimate(d, CONFIG_DEFAULTS, "partly", estRows, true);
    expect(partly.legs.map((l) => l.cost)).toEqual([4, null]);
    expect(partly.legs[1].invalid).toBeNull();
    expect(partly.broken).toBe(false);
    expect(partly.empty).toBe(false);
    expect(partly.sum).toBe(4);
    expect(partly.pAll).toBeCloseTo(0.475);
    expect(partly.wall).toBe(2000);
    expect(partly.per).toBeCloseTo(4 / 0.95);

    // nothing priced: valid config, no history → per null but NOT broken
    const none = estimate(d, CONFIG_DEFAULTS, "nodata", estRows, true);
    expect(none.broken).toBe(false);
    expect(none.empty).toBe(false);
    expect(none.per).toBeNull();

    // a genuinely invalid slot is the only thing that breaks a non-empty bench
    const bad = estimate(d, CONFIG_DEFAULTS, "bad", estRows, true);
    expect(bad.legs[0].invalid).toBe("pi model 'no-slash' needs the provider/model-id form");
    expect(bad.broken).toBe(true);
    expect(bad.per).toBeNull();
  });
});

// what `lime profiles get` emits for these rows — pinned on the Python side by
// tests/test_config.py::test_thinking_support_lists_every_catalog_model_in_pi_terms
const SUPPORT = {
  "openrouter/deepseek/deepseek-v4-flash": ["off", "high", "xhigh"],
  "openrouter/z-ai/glm-5.3-flash": ["low", "high", "max"],
  "openrouter/qwen/qwen3-coder-flash": ["off"],
};

describe("thinking support (ticket 91)", () => {
  const deepseek = "openrouter/deepseek/deepseek-v4-flash";
  const glm = "openrouter/z-ai/glm-5.3-flash";

  test("the ticket's repro: builder glm-5.3-flash/low → deepseek-v4-flash clamps thinking to high in the same patch", () => {
    const d = makeDraft();
    const builder = d.agents.find((a) => a.name === "builder")!;
    builder.model = glm;
    builder.thinking = "low";
    expect(validateSlot({ runtime: "pi", model: deepseek, thinking: "low" }, SUPPORT)).toBe(
      "thinking 'low' is not supported by openrouter/deepseek/deepseek-v4-flash — supported: (off, high, xhigh)",
    );
    expect(modelPatch(d, CONFIG_DEFAULTS, "default", "builder", deepseek, SUPPORT)).toEqual({
      model: deepseek,
      runtime: "pi",
      thinking: "high",
    });
    setRole(d, "default", "builder", { model: deepseek, runtime: "pi", thinking: "high" });
    expect(resolveRole(d, CONFIG_DEFAULTS, "default", "builder", SUPPORT).invalid).toBeNull();
    expect(builder.purpose).toBe("builder purpose — kept verbatim");
    expect(builder.tools).toEqual(["read", "grep", "find", "ls", "bash", "edit", "write"]);
  });

  test("a supported level is left alone: no thinking key, unknown models and claude_code never clamp", () => {
    const d = makeDraft();
    const builder = d.agents.find((a) => a.name === "builder")!;
    builder.model = glm;
    builder.thinking = "high";
    expect(modelPatch(d, CONFIG_DEFAULTS, "default", "builder", deepseek, SUPPORT)).toEqual({
      model: deepseek,
      runtime: "pi",
    });
    builder.thinking = "low";
    expect(modelPatch(d, CONFIG_DEFAULTS, "default", "builder", "openrouter/x/y", SUPPORT)).toEqual({
      model: "openrouter/x/y",
      runtime: "pi",
    });
    expect(modelPatch(d, CONFIG_DEFAULTS, "default", "builder", "sonnet", SUPPORT)).toEqual({
      model: "sonnet",
      runtime: "claude_code",
    });
    expect(modelPatch(d, CONFIG_DEFAULTS, "default", "builder", deepseek, undefined)).toEqual({
      model: deepseek,
      runtime: "pi",
    });
  });

  test("under a profile the clamp lands in the patch even when thinking is inherited", () => {
    const d = makeDraft();
    const qwen = "openrouter/qwen/qwen3-coder-flash";
    // quality profile: defaults thinking high, builder patched to claude-sonnet-5
    expect(modelPatch(d, CONFIG_DEFAULTS, "quality", "builder", qwen, SUPPORT)).toEqual({
      model: qwen,
      runtime: "pi",
      thinking: "off",
    });
    setRole(d, "quality", "builder", { model: qwen, runtime: "pi", thinking: "off" });
    expect(d.profiles.quality.agents![0]).toEqual({
      name: "builder",
      model: qwen,
      runtime: "pi",
      thinking: "off",
    });
    expect(resolveRole(d, CONFIG_DEFAULTS, "quality", "builder", SUPPORT).invalid).toBeNull();
    expect(agentByName(d, "builder")!.thinking).toBeUndefined();
  });

  test("addRole clamps like a model pick", () => {
    const d = makeDraft();
    addRole(d, "api-research", "builder", deepseek, CONFIG_DEFAULTS, SUPPORT);
    expect(d.profiles["api-research"].agents?.find((a) => a.name === "builder")).toEqual({
      name: "builder",
      model: deepseek,
      runtime: "pi",
      thinking: "high",
    });
    // The existing addRole test (no table) keeps passing unchanged
    const d2 = makeDraft();
    addRole(d2, "quality", "planner", "sonnet");
    expect(d2.profiles.quality.agents?.[1]).toEqual({
      name: "planner",
      model: "sonnet",
      runtime: "claude_code",
    });
  });

  // ── modelChange / defaultsModelChange / addRoleWithChange (step 3) ──────

  test("modelChange returns a ProfileModelChange with model/runtime and clamps thinking", () => {
    const d = makeDraft();
    // builder has no model or thinking set in the agent entry, resolves to defaults deepseek/medium
    const builder = d.agents.find((a) => a.name === "builder")!;
    builder.model = "openrouter/z-ai/glm-5.3-flash";
    builder.thinking = "low";
    // Switch to deepseek which only supports off/high/xhigh
    const change = modelChange(d, CONFIG_DEFAULTS, "default", "builder", deepseek, SUPPORT);
    expect(change.profile).toBe("default");
    expect(change.role).toBe("builder");
    expect(change.patch.model).toBe(deepseek);
    expect(change.patch.runtime).toBe("pi");
    expect(change.patch.thinking).toBe("high");
  });

  test("modelChange: a supported thinking level leaves thinking undefined", () => {
    const d = makeDraft();
    const builder = d.agents.find((a) => a.name === "builder")!;
    builder.model = deepseek;
    builder.thinking = "high"; // already supported
    const change = modelChange(d, CONFIG_DEFAULTS, "default", "builder", deepseek, SUPPORT);
    expect(change.patch.thinking).toBeUndefined();
  });

  test("modelChange: claude_code model never clamps thinking", () => {
    const d = makeDraft();
    const change = modelChange(d, CONFIG_DEFAULTS, "default", "planner", "sonnet");
    expect(change.patch.model).toBe("sonnet");
    expect(change.patch.runtime).toBe("claude_code");
    expect(change.patch.thinking).toBeUndefined();
  });

  test("defaultsModelChange: named-profile defaults model pick with role: null", () => {
    const d = makeDraft();
    // quality profile defaults thinking is "high"
    const change = defaultsModelChange(d, "quality", "openrouter/z-ai/glm-5.3-flash", SUPPORT);
    expect(change.profile).toBe("quality");
    expect(change.role).toBeNull();
    expect(change.patch.model).toBe("openrouter/z-ai/glm-5.3-flash");
    expect(change.patch.runtime).toBe("pi");
    // "high" is in glm's supported levels so no clamp
    expect(change.patch.thinking).toBeUndefined();
  });

  test("defaultsModelChange clamps when current thinking is not supported", () => {
    const d = makeDraft();
    // Set quality defaults thinking to "low"
    d.profiles["quality"].defaults = { thinking: "low" };
    const change = defaultsModelChange(d, "quality", deepseek, SUPPORT);
    expect(change.patch.model).toBe(deepseek);
    expect(change.patch.runtime).toBe("pi");
    // deepseek supports off/high/xhigh — low → high (nearest up)
    expect(change.patch.thinking).toBe("high");
  });

  test("addRoleWithChange mutates the draft and returns the change", () => {
    const d = makeDraft();
    const change = addRoleWithChange(d, "api-research", "documenter", deepseek, CONFIG_DEFAULTS, SUPPORT);
    // Change has the expected shape
    expect(change.profile).toBe("api-research");
    expect(change.role).toBe("documenter");
    expect(change.patch.model).toBe(deepseek);
    expect(change.patch.runtime).toBe("pi");
    // Draft was mutated
    const patch = d.profiles["api-research"].agents?.find((a) => a.name === "documenter");
    expect(patch?.model).toBe(deepseek);
    expect(patch?.runtime).toBe("pi");
    // No-op under default
    const d2 = makeDraft();
    const len = d2.agents.length;
    const change2 = addRoleWithChange(d2, "default", "ghost", "x/y");
    expect(d2.agents.length).toBe(len);
    expect(change2.role).toBe("ghost");
  });

  test("clampThinking picks the nearest supported level, up on a tie, unknown counts as medium", () => {
    expect(clampThinking("low", ["off", "high", "xhigh"])).toBe("high");
    expect(clampThinking("max", ["off", "minimal", "low", "medium", "high"])).toBe("high");
    expect(clampThinking("medium", ["off"])).toBe("off");
    expect(clampThinking("high", ["low", "high", "max"])).toBe("high");
    expect(clampThinking("weird", THINKING)).toBe("medium");
    expect(clampThinking("weird", ["off", "high", "xhigh"])).toBe("high");
    expect(clampThinking("low", [])).toBe("low");
  });

  test("stepThinking cycles only the model's levels, wraps, and an unsupported current counts as its clamp", () => {
    const L = ["off", "high", "xhigh"];
    expect(stepThinking("high", 1, L)).toBe("xhigh");
    expect(stepThinking("xhigh", 1, L)).toBe("off");
    expect(stepThinking("off", -1, L)).toBe("xhigh");
    expect(stepThinking("low", 1, L)).toBe("xhigh");
    expect(stepThinking("low", -1, L)).toBe("off");
    expect(stepThinking("off", 1, ["off"])).toBe("off");
  });

  test("validateSlot: generic check first, per-model check for pi only, absent model unconstrained", () => {
    expect(validateSlot({ runtime: "pi", model: "x/y", thinking: "ultra" }, SUPPORT)).toBe(
      "thinking 'ultra' is not one of (off, minimal, low, medium, high, xhigh, max)",
    );
    expect(validateSlot({ runtime: "pi", model: "openrouter/x/y", thinking: "low" }, SUPPORT)).toBeNull();
    expect(validateSlot({ runtime: "claude_code", model: "sonnet", thinking: "low" }, SUPPORT)).toBeNull();
    expect(validateSlot({ runtime: "pi", model: deepseek, thinking: "medium" }, SUPPORT)).toBe(
      "thinking 'medium' is not supported by openrouter/deepseek/deepseek-v4-flash — supported: (off, high, xhigh)",
    );
    expect(levelsFor(SUPPORT, "sonnet")).toEqual(THINKING);
    expect(levelsFor(undefined, deepseek)).toEqual(THINKING);
    expect(levelsFor(SUPPORT, deepseek)).toEqual(["off", "high", "xhigh"]);
  });

  test("resolveRole and estimate report an unsupported level before save", () => {
    const d = makeDraft();
    // builder resolves to deepseek/medium from CONFIG_DEFAULTS
    const r = resolveRole(d, CONFIG_DEFAULTS, "default", "builder", SUPPORT);
    expect(r.invalid).toBe(
      "thinking 'medium' is not supported by openrouter/deepseek/deepseek-v4-flash — supported: (off, high, xhigh)",
    );
    expect(r.src.thinking).toBe("config");
    const est = estimate(d, CONFIG_DEFAULTS, "default", [], true, SUPPORT);
    expect(est.broken).toBe(true);
    expect(est.legs[3].cost).toBeNull();
    // without SUPPORT invalid is null (backward compatible)
    expect(resolveRole(d, CONFIG_DEFAULTS, "default", "builder").invalid).toBeNull();
  });
});