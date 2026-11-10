import { describe, expect, test } from "bun:test";
import { filterTray, rankModels, trayModels } from "./modelTray";
import type { CatalogResponse, SessionCost } from "./types";

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

const LEDGER_ROWS: SessionCost[] = [
  // deepseek: 5 sessions (5 distinct runs), avg $1
  row({ model: "openrouter/deepseek/deepseek-v4-flash", run_id: "a", api_cost: 1 }),
  row({ model: "openrouter/deepseek/deepseek-v4-flash", run_id: "b", api_cost: 1 }),
  row({ model: "openrouter/deepseek/deepseek-v4-flash", run_id: "c", api_cost: 1 }),
  row({ model: "openrouter/deepseek/deepseek-v4-flash", run_id: "d", api_cost: 1 }),
  row({ model: "openrouter/deepseek/deepseek-v4-flash", run_id: "e", api_cost: 1 }),
  // sonnet: 3 sessions (3 distinct runs), avg $2 — a claude_code alias already in the ledger
  row({ model: "sonnet", run_id: "a", runtime: "claude_code", api_cost: 3 }),
  row({ model: "sonnet", run_id: "f", runtime: "claude_code", api_cost: 3 }),
  row({ model: "sonnet", run_id: "g", runtime: "claude_code", api_cost: 2 }),
  // grok: 1 run, expensive
  row({ model: "xai/grok-4.6", run_id: "h", api_cost: 9 }),
];

// pi's catalog: ids exactly as a config takes them, every provider pi is
// authenticated for — not only the OpenRouter ones (ticket 92).
const PI_CATALOG: CatalogResponse = {
  source: "pi",
  models: [
    {
      id: "openrouter/deepseek/deepseek-v4-flash",
      name: "DeepSeek V4 Flash",
      provider: "deepseek",
      prompt_price: null,
      completion_price: null,
      context_length: 128000,
    },
    {
      id: "openrouter/z-ai/glm-5.3-flash",
      name: "GLM 5.3 Flash",
      provider: "z-ai",
      prompt_price: null,
      completion_price: null,
      context_length: null,
    },
    {
      id: "openai-codex/gpt-5.5",
      name: "GPT-5.5",
      provider: "openai-codex",
      prompt_price: 5,
      completion_price: 30,
      context_length: 272000,
    },
  ],
};

describe("rankModels", () => {
  test("ledger models come first — most sessions, then cheapest", () => {
    const models = rankModels(LEDGER_ROWS, PI_CATALOG).filter(
      (m) => m.group === "ledger",
    );
    expect(models.map((m) => m.id)).toEqual([
      "openrouter/deepseek/deepseek-v4-flash",
      "sonnet",
      "xai/grok-4.6",
    ]);
    const deepseek = models[0];
    expect(deepseek.runs).toBe(5);
    expect(deepseek.avg).toBe(1);
    expect(deepseek.note).toBe("5 sessions");
    expect(deepseek.provider).toBe("openrouter");
    expect(deepseek.cc).toBe(false);
    const sonnet = models[1];
    expect(sonnet.cc).toBe(true);
    expect(sonnet.provider).toBe("claude_code");
    expect(sonnet.name).toBe("sonnet");
  });

  test("the four aliases follow the ledger without duplicating one already used", () => {
    const models = rankModels(LEDGER_ROWS, null);
    const aliases = models.filter((m) => m.group === "alias");
    expect(aliases.map((m) => m.id)).toEqual(["haiku", "opus", "fable"]); // sonnet already listed
    for (const a of aliases) {
      expect(a.cc).toBe(true);
      expect(a.provider).toBe("claude_code");
      expect(a.runs).toBe(0);
      expect(a.avg).toBeNull();
      expect(a.note).toBe("claude_code alias — rides the subscription");
    }
  });

  test("catalog ids pass through as-is, non-openrouter providers included, duplicates skipped", () => {
    const models = rankModels(LEDGER_ROWS, PI_CATALOG);
    const catalog = models.filter((m) => m.group === "catalog");
    // deepseek already listed in the ledger → skipped; a subscription model is offered
    expect(catalog.map((m) => m.id)).toEqual([
      "openai-codex/gpt-5.5",
      "openrouter/z-ai/glm-5.3-flash",
    ]);
    expect(catalog[0].name).toBe("GPT-5.5");
    expect(catalog[0].note).toBe("openai-codex · 272k ctx");
    expect(catalog[1].name).toBe("GLM 5.3 Flash");
    expect(catalog[1].note).toBe("z-ai");
    expect(catalog[1].cc).toBe(false);

    // direct providers before the metered OpenRouter pool, then provider, then
    // name — with a context-length note
    const cat2: CatalogResponse = {
      source: "pi",
      models: [
        { ...PI_CATALOG.models[1] },
        { ...PI_CATALOG.models[0] },
        { ...PI_CATALOG.models[2] },
      ],
    };
    const both = rankModels([], cat2).filter((m) => m.group === "catalog");
    expect(both.map((m) => m.provider)).toEqual([
      "openai-codex",
      "deepseek",
      "z-ai",
    ]);
    expect(both[1].note).toBe("deepseek · 128k ctx");

    // ledger-source ids pass through as-is, claude_code provider marks cc
    const ledgerCat: CatalogResponse = {
      source: "ledger",
      models: [
        { id: "xai/grok-4.6", name: "Grok", provider: "xai", prompt_price: null, completion_price: null, context_length: null },
        { id: "openrouter/google/gemini-3.8-flash", name: "Gemini", provider: "google", prompt_price: null, completion_price: null, context_length: null },
        { id: "claude-opus-5", name: "Opus 5", provider: "claude_code", prompt_price: null, completion_price: null, context_length: null },
      ],
    };
    const kept = rankModels(LEDGER_ROWS, ledgerCat).filter(
      (m) => m.group === "catalog",
    );
    // grok already listed in the ledger → skipped; the other two pass through
    expect(kept.map((m) => m.id)).toEqual([
      "claude-opus-5",
      "openrouter/google/gemini-3.8-flash",
    ]);
    expect(kept[0].cc).toBe(true);
    expect(kept[0].provider).toBe("claude_code");
    expect(kept[1].cc).toBe(false);
  });
});

test("filterTray matches id, name and provider across every group", () => {
  const models = rankModels(LEDGER_ROWS, PI_CATALOG);
  expect(filterTray(models, "  ").length).toBe(models.length);
  expect(filterTray(models, "GROK").map((m) => m.id)).toEqual(["xai/grok-4.6"]);
  expect(filterTray(models, "z-ai").map((m) => m.id)).toEqual([
    "openrouter/z-ai/glm-5.3-flash",
  ]);
  expect(filterTray(models, "alias — rides").map((m) => m.group)).toEqual([
    "alias", "alias", "alias",
  ]);
  expect(filterTray(models, "no match at all")).toEqual([]);
  expect(trayModels(LEDGER_ROWS, PI_CATALOG, "sonnet")[0].id).toBe("sonnet");
});

test("a null catalog yields ledger + aliases only", () => {
  const models = rankModels(LEDGER_ROWS, null);
  expect(models.some((m) => m.group === "catalog")).toBe(false);
  expect(models.map((m) => m.group)).toEqual([
    "ledger", "ledger", "ledger", "alias", "alias", "alias",
  ]);
});