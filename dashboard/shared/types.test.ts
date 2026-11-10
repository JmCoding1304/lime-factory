import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { isCashModel } from "./types";

const CASH = [
  "openrouter/deepseek/deepseek-v4.1-flash",
  "openrouter/anthropic/claude-sonnet-5",
  "OpenRouter/x/y",
];

const SUB: (string | null | undefined)[] = [
  "openai-codex/gpt-6-astra",
  "haiku",
  "sonnet",
  "opus",
  "fable",
  "claude-haiku-5-5",
  "claude-opus-5-5[1m]",
  "claude-fable-5[1m]",
  "xai/grok-4.6",
  "mystery-model",
  "",
  null,
  undefined,
];

describe("isCashModel", () => {
  test("openrouter/ models are cash, case-insensitively", () => {
    for (const m of CASH) expect(isCashModel(m)).toBe(true);
  });

  test("everything else rides a subscription", () => {
    for (const m of SUB) expect(isCashModel(m)).toBe(false);
  });
});

describe("roster sweep", () => {
  const text = readFileSync(
    new URL("../../lime/templates/config.yaml", import.meta.url),
    "utf8",
  );
  const models = [...text.matchAll(/model:\s*([^\s,}#]+)/g)].map((m) => m[1]!);

  test("the roster has enough models to be meaningful", () => {
    expect(models.length).toBeGreaterThanOrEqual(5);
  });

  test("the roster spans cash, codex and a slash-less Claude Code alias", () => {
    const prefixes = new Set(models.map((m) => m.split("/")[0]));
    expect(prefixes).toContain("openrouter");
    expect(prefixes).toContain("openai-codex");
    expect(models.some((m) => !m.includes("/"))).toBe(true);
  });

  test("every roster model's cash flag equals its literal prefix test", () => {
    for (const v of models) {
      expect(isCashModel(v)).toBe(v.startsWith("openrouter/"));
    }
  });
});
