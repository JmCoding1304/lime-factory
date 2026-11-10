import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { apiPort, OWNER_PORT } from "./port.ts";

describe("apiPort", () => {
  test("no env falls back to the owner's port", () => {
    expect(apiPort({})).toBe(4600);
    expect(apiPort({})).toBe(OWNER_PORT);
  });

  test("LIME_PORT is used", () => {
    expect(apiPort({ LIME_PORT: "3101" })).toBe(3101);
  });

  test("PORT wins over LIME_PORT", () => {
    expect(apiPort({ PORT: "4700", LIME_PORT: "3101" })).toBe(4700);
  });

  test("empty values count as unset", () => {
    expect(apiPort({ PORT: "", LIME_PORT: "" })).toBe(4600);
  });

  test("whitespace PORT falls through to LIME_PORT", () => {
    expect(apiPort({ PORT: " ", LIME_PORT: "3102" })).toBe(3102);
  });

  test("a non-integer PORT throws", () => {
    expect(() => apiPort({ PORT: "abc" })).toThrow(/PORT="abc" is not a port/);
  });

  test("zero LIME_PORT throws", () => {
    expect(() => apiPort({ LIME_PORT: "0" })).toThrow(/LIME_PORT="0"/);
  });

  test("out-of-range LIME_PORT throws", () => {
    expect(() => apiPort({ LIME_PORT: "70000" })).toThrow();
  });
});

describe("vite.config.ts", () => {
  function viteTarget(extra: Record<string, string | undefined>): string {
    const env: Record<string, string | undefined> = { ...process.env, ...extra };
    if (!("PORT" in extra)) delete env.PORT;
    const proc = Bun.spawnSync(
      [
        process.execPath,
        "-e",
        'const c = (await import("./vite.config.ts")).default; console.log(c.server.proxy["/api"].target)',
      ],
      { cwd: resolve(import.meta.dir, ".."), env },
    );
    expect(proc.exitCode).toBe(0);
    return proc.stdout.toString().trim();
  }

  test("proxy follows LIME_PORT", () => {
    expect(viteTarget({ LIME_PORT: "3999" })).toBe("http://127.0.0.1:3999");
  });

  test("PORT wins for the proxy", () => {
    expect(viteTarget({ PORT: "4711", LIME_PORT: "3999" })).toBe("http://127.0.0.1:4711");
  });
});

describe("one port rule", () => {
  const indexSrc = readFileSync(resolve(import.meta.dir, "index.ts"), "utf8");
  const viteSrc = readFileSync(resolve(import.meta.dir, "..", "vite.config.ts"), "utf8");

  test("server/index.ts imports and uses apiPort", () => {
    expect(indexSrc).toContain('from "./port.ts"');
    expect(indexSrc).toContain("apiPort()");
    expect(indexSrc).not.toContain("process.env.PORT");
  });

  test("vite.config.ts imports and uses apiPort", () => {
    expect(viteSrc).toContain('from "./server/port.ts"');
    expect(viteSrc).toContain("apiPort()");
    expect(viteSrc).not.toContain("process.env.PORT");
  });

  test("server/index.ts documents the one rule", () => {
    expect(indexSrc).not.toContain("PORT=4600");
    expect(indexSrc).toContain("Inside a Run the port");
    expect(indexSrc).toContain("LIME_PORT");
  });
});
