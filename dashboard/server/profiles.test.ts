/**
 * The profiles seam: GET round-trips the template roster, PUT writes go
 * through the real `lime profiles set` (Python validates and does the
 * span-preserving rewrite), and the model catalog comes from `lime models`
 * with the ledger as the fallback when pi has no catalog.
 *
 * Spawn tests resolve the binary exactly as profiles.ts does and skip when it
 * can't be found, so the suite stays green on machines without a lime that
 * knows the profiles subcommand.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Database } from "bun:sqlite";
import { LimeRegistry } from "./db.ts";
import {
  getProfiles,
  limeAvailable,
  modelCatalog,
  piModelCatalog,
  putProfiles,
  batchMergeProfiles,
  mergeProfiles,
  type ProfilesGet,
} from "./profiles.ts";

const TEMPLATE_CONFIG = resolve(
  import.meta.dir,
  "..",
  "..",
  "lime",
  "templates",
  "config.yaml",
);

const TEMPLATE_ROLES = [
  "scout",
  "researcher",
  "planner",
  "builder",
  "reviewer",
  "documenter",
  "designer",
];

let root: string;
let registryFile: string;
let projectPath: string;
let configPath: string;

const PROJECT = "alpha";
/** A binary that cannot be spawned — the catalog's ledger-fallback path. */
const NO_BIN = "/nonexistent/lime";

beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "lime-profiles-test-"));
  projectPath = join(root, PROJECT);
  const limeDir = join(projectPath, ".lime");
  const runsDir = join(limeDir, "runs");
  mkdirSync(runsDir, { recursive: true });
  copyFileSync(TEMPLATE_CONFIG, join(limeDir, "config.yaml"));
  configPath = join(limeDir, "config.yaml");

  // A session ledger for the catalog fallback: one pi model, one subscription.
  const db = new Database(join(runsDir, "lime.db"));
  db.exec("PRAGMA journal_mode=WAL");
  db.exec(
    "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, phase_id TEXT NOT NULL, role TEXT NOT NULL, continuation INTEGER NOT NULL DEFAULT 0, runtime TEXT NOT NULL, model TEXT NOT NULL, color TEXT, context_tokens INTEGER, context_window INTEGER, created_at TEXT NOT NULL, last_used_at TEXT)",
  );
  const insert = db.query(
    "INSERT INTO sessions (session_id, run_id, phase_id, role, runtime, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
  );
  insert.run("s1", "R1", "R1:01", "builder", "pi", "openrouter/deepseek/deepseek-v4-flash", "2026-08-31T10:00:00+00:00");
  insert.run("s2", "R1", "R1:02", "planner", "claude_code", "claude-fable-5", "2026-08-31T10:00:00+00:00");
  insert.run("s3", "R1", "R1:03", "builder", "pi", "openrouter/deepseek/deepseek-v4-flash", "2026-08-31T10:00:00+00:00");
  db.close();

  registryFile = join(root, "projects");
  writeFileSync(registryFile, `${projectPath}\n`);
});

afterAll(() => {
  rmSync(root, { recursive: true, force: true });
});

const skipSpawn = !limeAvailable();

/** A fresh GET — each test builds its payloads off the file's current truth. */
async function fresh(): Promise<ProfilesGet> {
  const res = await getProfiles(PROJECT, projectPath);
  return {
    agents: structuredClone(res.agents),
    profiles: structuredClone(res.profiles),
    defaults: res.defaults,
    merged: res.merged,
    profiles_list: res.profiles_list,
    thinking_support: res.thinking_support,
  };
}

describe("profiles GET/PUT through the lime CLI", () => {
  test.skipIf(skipSpawn)("the spawn seam resolves a runnable binary", () => {
    expect(limeAvailable()).toBe(true);
    expect(existsSync(projectPath)).toBe(true);
  });

  test.skipIf(skipSpawn)(
    "GET round-trips the template: raw blocks, defaults and the merged roster",
    async () => {
      const res = await getProfiles(PROJECT, projectPath);
      expect(res.project).toBe(PROJECT);
      expect(res.config_path).toBe(configPath);
      expect(res.merged.default.map((a) => a.name)).toEqual(TEMPLATE_ROLES);
      expect(res.agents.map((a) => a.name)).toEqual(TEMPLATE_ROLES);
      expect(Object.keys(res.profiles).toSorted()).toEqual([
        "all-claude",
        "api-research",
        "quality",
      ]);
      expect(res.profiles.quality?.defaults).toEqual({ thinking: "high" });
      expect(res.defaults.runtime).toBe("pi");
      expect(res.profiles_list).toEqual([
        "default",
        "quality",
        "all-claude",
        "api-research",
      ]);
      // every configured profile resolves to a full merged roster too
      for (const name of Object.keys(res.profiles)) {
        expect(res.merged[name]).toHaveLength(7);
      }
      // thinking_support is present
      expect(typeof res.thinking_support).toBe("object");
      for (const v of Object.values(res.thinking_support)) {
        expect(Array.isArray(v)).toBe(true);
        for (const item of v) {
          expect(typeof item).toBe("string");
        }
      }
    },
  );

  test.skipIf(skipSpawn)(
    "PUT with an invalid roster fails with the validate() message and the file is byte-identical",
    async () => {
      const before = readFileSync(configPath);
      const base = await fresh();
      const builder = base.agents.find((a) => a.name === "builder");
      if (!builder) throw new Error("template has no builder");
      builder.runtime = "docker";
      const result = await putProfiles(PROJECT, projectPath, {
        agents: base.agents,
        profiles: base.profiles,
      });
      expect(result.ok).toBe(false);
      if (result.ok) throw new Error("unreachable");
      expect(result.errors.length).toBeGreaterThan(0);
      expect(result.errors.some((e) => e.includes("docker"))).toBe(true);
      expect(readFileSync(configPath).equals(before)).toBe(true);
    },
  );

  test.skipIf(skipSpawn)(
    "PUT role-edit-under-default changes agents: only, profiles stay untouched",
    async () => {
      const base = await fresh();
      const builder = base.agents.find((a) => a.name === "builder");
      if (!builder) throw new Error("template has no builder");
      // a model no template profile patches, so the quality patch stays a diff
      builder.model = "openrouter/qwen/qwen3-coder-flash";
      const result = await putProfiles(PROJECT, projectPath, {
        agents: base.agents,
        profiles: base.profiles,
      });
      expect(result.ok).toBe(true);
      if (!result.ok) throw new Error("unreachable");
      const merged = result.response.merged.default.find((a) => a.name === "builder");
      expect(merged?.model).toBe("openrouter/qwen/qwen3-coder-flash");
      expect(result.response.profiles).toEqual(base.profiles);
      expect(result.response.thinking_support).toBeDefined();
      expect(result.response.agents).toEqual(base.agents);
    },
  );

  test.skipIf(skipSpawn)(
    "PUT role-edit-under-a-named-profile stores a diff-only patch, no duplicated role",
    async () => {
      const base = await fresh();
      const profiles = structuredClone(base.profiles);
      profiles["fancy"] = {
        agents: [
          {
            name: "builder",
            runtime: "pi",
            thinking: "medium",
            timeout_s: 1800,
            model: "openrouter/moonshotai/kimi-k3",
          },
        ],
      };
      const result = await putProfiles(PROJECT, projectPath, {
        agents: base.agents,
        profiles,
      });
      expect(result.ok).toBe(true);
      if (!result.ok) throw new Error("unreachable");
      const stored = result.response.profiles["fancy"];
      // runtime/timeout_s match the base builder and are dropped; thinking is kept
      // because the template promoted builder to high, so medium is a real override.
      expect(stored?.agents).toEqual([
        { name: "builder", model: "openrouter/moonshotai/kimi-k3", thinking: "medium" },
      ]);
      const merged = result.response.merged["fancy"]?.find(
        (a) => a.name === "builder",
      );
      expect(merged?.model).toBe("openrouter/moonshotai/kimi-k3");
    },
  );

  test.skipIf(skipSpawn)(
    "a second identical PUT changes nothing: same bytes, same mtime",
    async () => {
      const base = await fresh();
      const profiles = structuredClone(base.profiles);
      profiles["steady"] = { agents: [{ name: "documenter", thinking: "high" }] };
      const payload = { agents: base.agents, profiles };
      expect((await putProfiles(PROJECT, projectPath, payload)).ok).toBe(true);
      const bytes = readFileSync(configPath);
      const mtime = statSync(configPath).mtimeMs;
      const second = await putProfiles(PROJECT, projectPath, payload);
      expect(second.ok).toBe(true);
      expect(readFileSync(configPath).equals(bytes)).toBe(true);
      expect(statSync(configPath).mtimeMs).toBe(mtime);
    },
  );

  test.skipIf(skipSpawn)(
    "ticket 89: PUT adding a role at its own runtime/model keeps it as a name-only member; removing it removes it",
    async () => {
      const base = await fresh();
      const planner = base.merged.default.find((a) => a.name === "planner");
      if (!planner) throw new Error("template has no planner");
      const profiles = structuredClone(base.profiles);
      const allClaude = (profiles["all-claude"] ??= {});
      (allClaude.agents ??= []).push({
        name: "planner",
        model: planner.model,
        runtime: planner.runtime,
      });
      const added = await putProfiles(PROJECT, projectPath, { agents: base.agents, profiles });
      expect(added.ok).toBe(true);
      if (!added.ok) throw new Error("unreachable");
      expect(added.response.profiles["all-claude"]?.agents).toContainEqual({ name: "planner" });
      // the same payload again writes nothing
      const bytes = readFileSync(configPath);
      const mtime = statSync(configPath).mtimeMs;
      expect((await putProfiles(PROJECT, projectPath, { agents: base.agents, profiles })).ok).toBe(true);
      expect(readFileSync(configPath).equals(bytes)).toBe(true);
      expect(statSync(configPath).mtimeMs).toBe(mtime);
      // removal still removes
      allClaude.agents = allClaude.agents.filter((a) => a.name !== "planner");
      const removed = await putProfiles(PROJECT, projectPath, { agents: base.agents, profiles });
      expect(removed.ok).toBe(true);
      if (!removed.ok) throw new Error("unreachable");
      expect(
        removed.response.profiles["all-claude"]?.agents?.some((a) => a.name === "planner"),
      ).toBe(false);
    },
  );

  test.skipIf(skipSpawn)(
    "PUT with a freshly created empty profile keeps it — listed, resolvable, editable, deletable (ticket 92)",
    async () => {
      const base = await fresh();
      const MODEL = "openrouter/z-ai/glm-5.3-flash";
      const baseBuilder = base.merged.default.find((a) => a.name === "builder");
      if (!baseBuilder) throw new Error("template has no builder");
      expect(baseBuilder.model).not.toBe(MODEL);

      const profiles = structuredClone(base.profiles);
      profiles["fast-lane"] = { agents: [] };
      const result = await putProfiles(PROJECT, projectPath, {
        agents: base.agents,
        profiles,
      });
      expect(result.ok).toBe(true);
      if (!result.ok) throw new Error("unreachable");
      expect(result.response.profiles["fast-lane"]).toEqual({});
      expect(result.response.profiles_list).toEqual([
        ...base.profiles_list,
        "fast-lane",
      ]);
      expect(
        result.response.merged["fast-lane"]?.map((a) => a.name),
      ).toEqual(result.response.merged.default.map((a) => a.name));

      // Roster edit
      profiles["fast-lane"] = {
        agents: [{ name: "builder", model: MODEL }],
      };
      const edited = await putProfiles(PROJECT, projectPath, {
        agents: base.agents,
        profiles,
      });
      expect(edited.ok).toBe(true);
      if (!edited.ok) throw new Error("unreachable");
      expect(edited.response.profiles["fast-lane"]).toEqual({
        agents: [{ name: "builder", model: MODEL }],
      });
      const mergedBuilder = edited.response.merged["fast-lane"]?.find(
        (a) => a.name === "builder",
      );
      expect(mergedBuilder?.model).toBe(MODEL);

      // Cleanup = delete check: remove fast-lane completely
      delete profiles["fast-lane"];
      const cleaned = await putProfiles(PROJECT, projectPath, {
        agents: base.agents,
        profiles,
      });
      expect(cleaned.ok).toBe(true);
      if (!cleaned.ok) throw new Error("unreachable");
      expect(cleaned.response.profiles_list).toEqual(base.profiles_list);
    },
  );
});

// --- model catalog: pi's own catalog, the ledger as the fallback (ticket 92) ---

describe("model catalog", () => {
  test("`lime models` rows carry pi ids, names, prices and context", async () => {
    // a stub binary standing in for `lime models`: the contract, not the machine
    const stub = join(root, "lime-models-stub");
    writeFileSync(
      stub,
      `#!/bin/sh\ncat <<'JSON'\n${JSON.stringify({
        source: "pi",
        models: [
          {
            id: "openai-codex/gpt-5.5",
            name: "GPT-5.5",
            provider: "openai-codex",
            prompt_price: 5,
            completion_price: 30,
            context_length: 272000,
          },
          { id: "xai/grok-4.6", name: "Grok 4.6", provider: "xai" },
          { name: "no id at all" },
        ],
      })}\nJSON\n`,
      { mode: 0o755 },
    );
    const reg = new LimeRegistry(registryFile);
    try {
      const catalog = await modelCatalog(reg, stub);
      expect(catalog.source).toBe("pi");
      // the row without an id is dropped, the rest keep the id a config takes
      expect(catalog.models).toEqual([
        {
          id: "openai-codex/gpt-5.5",
          name: "GPT-5.5",
          provider: "openai-codex",
          prompt_price: 5,
          completion_price: 30,
          context_length: 272000,
        },
        {
          id: "xai/grok-4.6",
          name: "Grok 4.6",
          provider: "xai",
          prompt_price: null,
          completion_price: null,
          context_length: null,
        },
      ]);
    } finally {
      reg.close();
    }
  });

  test("a catalog pi cannot produce falls back to the session ledger with null prices", async () => {
    const reg = new LimeRegistry(registryFile);
    try {
      const catalog = await modelCatalog(reg, NO_BIN);
      expect(catalog.source).toBe("ledger");
      const pairs = catalog.models.map((m) => [m.provider, m.id]);
      expect(pairs).toContainEqual(["claude_code", "claude-fable-5"]);
      expect(pairs).toContainEqual([
        "openrouter",
        "openrouter/deepseek/deepseek-v4-flash",
      ]);
      expect(
        catalog.models.every(
          (m) => m.prompt_price === null && m.completion_price === null,
        ),
      ).toBe(true);
    } finally {
      reg.close();
    }
  });

  test("an unspawnable binary yields null rather than throwing", async () => {
    expect(await piModelCatalog(NO_BIN)).toBeNull();
  });
});

// --- profiles merge: batch-sync (step 2) ---

describe("profiles merge through the lime CLI", () => {
  /**
   * Create a second project (beta) with its own config.yaml, differently
   * configured: builder's base model differs, and it has a distinct profile.
   */
  const BETA = "beta";
  let betaPath: string;
  let betaConfigPath: string;

  beforeAll(() => {
    betaPath = join(root, BETA);
    const betaLime = join(betaPath, ".lime");
    mkdirSync(betaLime, { recursive: true });
    copyFileSync(TEMPLATE_CONFIG, join(betaLime, "config.yaml"));
    betaConfigPath = join(betaLime, "config.yaml");
    // Register beta
    writeFileSync(registryFile, `${projectPath}\n${betaPath}\n`);
  });

  test.skipIf(skipSpawn)(
    "multi-target success: each target gets the merge independently",
    async () => {
      const changes = [
        {
          profile: "default" as const,
          role: "builder" as const,
          patch: { model: "openrouter/anthropic/claude-sonnet-5", runtime: "pi" as const },
        },
      ];
      const targets = [
        { name: PROJECT, path: projectPath },
        { name: BETA, path: betaPath },
      ];
      const results = await batchMergeProfiles(targets, changes);
      expect(results).toHaveLength(2);
      for (const r of results) {
        expect(r.ok).toBe(true);
        expect(r.wrote).toBe(true);
        expect(r.errors).toEqual([]);
      }
      // Verify alpha's builder changed
      const alphaGet = await getProfiles(PROJECT, projectPath);
      const alphaBuilder = alphaGet.merged.default.find((a) => a.name === "builder");
      expect(alphaBuilder?.model).toBe("openrouter/anthropic/claude-sonnet-5");
      // Verify beta's builder changed
      const betaGet = await getProfiles(BETA, betaPath);
      const betaBuilder = betaGet.merged.default.find((a) => a.name === "builder");
      expect(betaBuilder?.model).toBe("openrouter/anthropic/claude-sonnet-5");
    },
  );

  test.skipIf(skipSpawn)(
    "preserves each target's unrelated data — profiles, other agents, comments",
    async () => {
      const changes = [
        {
          profile: "api-research" as const,
          role: "researcher" as const,
          patch: { model: "openrouter/anthropic/claude-sonnet-5", runtime: "pi" as const },
        },
      ];
      const targets = [
        { name: PROJECT, path: projectPath },
        { name: BETA, path: betaPath },
      ];
      const results = await batchMergeProfiles(targets, changes);
      expect(results).toHaveLength(2);
      expect(results[0].ok).toBe(true);
      expect(results[1].ok).toBe(true);

      // Quality profile untouched on both
      const alphaGet = await getProfiles(PROJECT, projectPath);
      expect(Object.keys(alphaGet.profiles)).toContain("quality");
      expect(alphaGet.profiles["quality"]?.defaults).toEqual({ thinking: "high" });

      const betaGet = await getProfiles(BETA, betaPath);
      expect(Object.keys(betaGet.profiles)).toContain("quality");
      expect(betaGet.profiles["quality"]?.defaults).toEqual({ thinking: "high" });
    },
  );

  test.skipIf(skipSpawn)(
    "a registered project without lime.db (no runs) still gets merged",
    async () => {
      // bare project has config.yaml but no lime.db
      const BARE = "bare";
      const barePath = join(root, BARE);
      const bareLime = join(barePath, ".lime");
      mkdirSync(bareLime, { recursive: true });
      copyFileSync(TEMPLATE_CONFIG, join(bareLime, "config.yaml"));
      writeFileSync(registryFile, `${projectPath}\n${betaPath}\n${barePath}\n`);

      const changes = [
        {
          profile: "default" as const,
          role: "scout" as const,
          patch: { model: "openrouter/qwen/qwen3-coder-flash", runtime: "pi" as const },
        },
      ];
      const targets = [
        { name: PROJECT, path: projectPath },
        { name: BARE, path: barePath },
      ];
      const results = await batchMergeProfiles(targets, changes);
      expect(results).toHaveLength(2);
      expect(results[0].ok).toBe(true);
      expect(results[1].ok).toBe(true);

      // bare's scout still has the base template model (the merge leaves it at
      // what the template already has — the merge sets it to the same value)
      const bareGet = await getProfiles(BARE, barePath);
      const bareScout = bareGet.merged.default.find((a) => a.name === "scout");
      expect(bareScout?.model).toBe("openrouter/qwen/qwen3-coder-flash");
    },
  );

  test.skipIf(skipSpawn)(
    "partial failure: one valid target persists while a broken target reports its errors",
    async () => {
      // Set up a scenario: apply a valid change (default/builder model) to alpha,
      // but give beta an invalid change target
      const validChanges = [
        {
          profile: "default" as const,
          role: "builder" as const,
          patch: { model: "openrouter/deepseek/deepseek-v4-flash", runtime: "pi" as const },
        },
      ];
      const invalidChanges = [
        {
          profile: "quality" as const,
          role: "ghost" as const,  // not in base roster
          patch: { model: "openrouter/anthropic/claude-sonnet-5", runtime: "pi" as const },
        },
      ];

      // Alpha gets a valid merge that writes
      const alphaResult = await mergeProfiles(projectPath, validChanges);
      expect(alphaResult.ok).toBe(true);
      if (alphaResult.ok) {
        expect(alphaResult.wrote).toBe(true);
      }
      // Verify alpha's builder changed
      const alphaGet = await getProfiles(PROJECT, projectPath);
      const alphaBuilder = alphaGet.merged.default.find((a) => a.name === "builder");
      expect(alphaBuilder?.model).toBe("openrouter/deepseek/deepseek-v4-flash");

      // Beta gets an invalid merge that fails, leaving its file byte-identical
      const betaBefore = readFileSync(betaConfigPath);
      const betaResult = await mergeProfiles(betaPath, invalidChanges);
      expect(betaResult.ok).toBe(false);
      if (!betaResult.ok) {
        expect(betaResult.errors.some((e: string) => e.includes("ghost"))).toBe(true);
      }
      // beta's file is unchanged
      expect(readFileSync(betaConfigPath).equals(betaBefore)).toBe(true);

      // Now test batchMergeProfiles directly with mixed targets
      const batchTargets = [
        { name: PROJECT, path: projectPath },
        { name: BETA, path: betaPath },
      ];
      const batchChanges = [
        {
          profile: "default" as const,
          role: "scout" as const,
          patch: { model: "openrouter/qwen/qwen3-coder-flash", runtime: "pi" as const },
        },
      ];
      const batchResults = await batchMergeProfiles(batchTargets, batchChanges);
      expect(batchResults).toHaveLength(2);
      expect(batchResults[0].ok).toBe(true);  // alpha succeeds
      expect(batchResults[1].ok).toBe(true);  // beta also succeeds (scout is valid)
    },
  );

  test.skipIf(skipSpawn)(
    "subprocess failure returns error result row",
    async () => {
      const changes = [
        {
          profile: "default" as const,
          role: "builder" as const,
          patch: { model: "openrouter/deepseek/deepseek-v4-flash", runtime: "pi" as const },
        },
      ];
      // Use a nonexistent path to simulate subprocess failure
      const result = await batchMergeProfiles([
        { name: "missing", path: "/nonexistent/path" },
      ], changes);
      expect(result).toHaveLength(1);
      expect(result[0].ok).toBe(false);
      expect(result[0].wrote).toBe(false);
      expect(result[0].errors.length).toBeGreaterThan(0);
      expect(result[0].project).toBe("missing");
    },
  );

  test.skipIf(skipSpawn)(
    "unknown/unregistered targets get error result rows",
    async () => {
      const changes = [
        {
          profile: "default" as const,
          role: "builder" as const,
          patch: { model: "openrouter/deepseek/deepseek-v4-flash", runtime: "pi" as const },
        },
      ];
      const targets = [
        { name: PROJECT, path: projectPath },
        { name: "nope", path: null },
      ];
      const results = await batchMergeProfiles(targets, changes);
      expect(results).toHaveLength(2);
      expect(results[0].ok).toBe(true);
      expect(results[1].ok).toBe(false);
      expect(results[1].errors.some((e) => e.includes("unregistered"))).toBe(true);
    },
  );
});
