import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { networkInterfaces, tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { refuseUnlessLocal } from "./local-only.ts";

const PORTS = [4600, 4601] as const;

function req(path: string, init: RequestInit = {}): Request {
  return new Request(`http://localhost:4600${path}`, init);
}

describe("refuseUnlessLocal", () => {
  test("foreign Host on /api/health is refused", () => {
    const res = refuseUnlessLocal(
      req("/api/health", { headers: { host: "evil.example:4600" } }),
      PORTS,
    );
    expect(res?.status).toBe(403);
  });

  test("foreign Host on / is refused", () => {
    const res = refuseUnlessLocal(
      req("/", { headers: { host: "evil.example:4600" } }),
      PORTS,
    );
    expect(res?.status).toBe(403);
  });

  test("DNS-rebinding Host shape is refused", () => {
    const res = refuseUnlessLocal(
      req("/", { headers: { host: "localhost.evil.example:4600" } }),
      PORTS,
    );
    expect(res?.status).toBe(403);
  });

  test("wrong port is refused", () => {
    const res = refuseUnlessLocal(
      req("/api/health", { headers: { host: "localhost:9999" } }),
      PORTS,
    );
    expect(res?.status).toBe(403);
  });

  test("a Host without a port is refused", () => {
    const res = refuseUnlessLocal(
      req("/api/health", { headers: { host: "localhost" } }),
      PORTS,
    );
    expect(res?.status).toBe(403);
  });

  test("local authorities are allowed", () => {
    for (const host of [
      "localhost:4600",
      "127.0.0.1:4600",
      "[::1]:4600",
      "LOCALHOST:4601",
    ]) {
      const res = refuseUnlessLocal(
        req("/api/health", { headers: { host } }),
        PORTS,
      );
      expect(res).toBeNull();
    }
  });

  test("a foreign Origin on a write is refused", () => {
    const res = refuseUnlessLocal(
      req("/api/projects/x/profiles", {
        method: "PUT",
        headers: { host: "localhost:4600", origin: "http://evil.example" },
      }),
      PORTS,
    );
    expect(res?.status).toBe(403);
  });

  test("Origin: null on a write is refused", () => {
    const res = refuseUnlessLocal(
      req("/api/projects/x/profiles", {
        method: "PUT",
        headers: { host: "localhost:4600", origin: "null" },
      }),
      PORTS,
    );
    expect(res?.status).toBe(403);
  });

  test("the Vite Origin on a write is allowed", () => {
    const res = refuseUnlessLocal(
      req("/api/projects/x/profiles", {
        method: "PUT",
        headers: { host: "localhost:4600", origin: "http://localhost:4601" },
      }),
      PORTS,
    );
    expect(res).toBeNull();
  });

  test("a write without an Origin is allowed", () => {
    const res = refuseUnlessLocal(
      req("/api/projects/x/profiles", {
        method: "PUT",
        headers: { host: "localhost:4600" },
      }),
      PORTS,
    );
    expect(res).toBeNull();
  });

  test("a GET without an Origin is allowed", () => {
    const res = refuseUnlessLocal(
      req("/api/health", { headers: { host: "localhost:4600" } }),
      PORTS,
    );
    expect(res).toBeNull();
  });

  test("a GET skips the Origin check", () => {
    const res = refuseUnlessLocal(
      req("/api/health", {
        headers: { host: "localhost:4600", origin: "http://evil.example" },
      }),
      PORTS,
    );
    expect(res).toBeNull();
  });

  test("a foreign Origin on a GET with originOnGet is refused", () => {
    const res = refuseUnlessLocal(
      req("/api/live", {
        headers: { host: "localhost:4600", origin: "http://evil.example" },
      }),
      PORTS,
      { originOnGet: true },
    );
    expect(res?.status).toBe(403);
  });

  test("a foreign Origin on a GET without originOnGet is allowed", () => {
    const res = refuseUnlessLocal(
      req("/api/live", {
        headers: { host: "localhost:4600", origin: "http://evil.example" },
      }),
      PORTS,
    );
    expect(res).toBeNull();
  });

  test("the Vite Origin on a GET with originOnGet is allowed", () => {
    const res = refuseUnlessLocal(
      req("/api/live", {
        headers: { host: "localhost:4600", origin: "http://localhost:4601" },
      }),
      PORTS,
      { originOnGet: true },
    );
    expect(res).toBeNull();
  });

  test("a GET without an Origin with originOnGet is allowed", () => {
    const res = refuseUnlessLocal(
      req("/api/live", { headers: { host: "localhost:4600" } }),
      PORTS,
      { originOnGet: true },
    );
    expect(res).toBeNull();
  });
});

function firstExternalIPv4(): string | null {
  for (const addrs of Object.values(networkInterfaces())) {
    for (const addr of addrs ?? []) {
      if (addr.family === "IPv4" && !addr.internal) return addr.address;
    }
  }
  return null;
}

describe("dashboard server", () => {
  let proc: Bun.Subprocess<"ignore", "ignore", "pipe">;
  let port = 0;
  let tmp = "";

  beforeAll(async () => {
    tmp = mkdtempSync(join(tmpdir(), "lime-local-only-"));
    const registryFile = join(tmp, "projects");
    const project = join(tmp, "alpha");
    mkdirSync(project, { recursive: true });
    writeFileSync(registryFile, `# repos registered by lime init\n${project}\n`);

    // Never 4600/4601: bind an ephemeral port, learn it, release it, and hand
    // it to the server as LIME_PORT the way a Run does — no PORT, so this also
    // proves the LIME_PORT default.
    const probe = Bun.serve({
      port: 0,
      hostname: "127.0.0.1",
      fetch: () => new Response(),
    });
    if (probe.port === undefined) throw new Error("probe server bound no TCP port");
    port = probe.port;
    probe.stop(true);

    const env: Record<string, string | undefined> = {
      ...process.env,
      LIME_PORT: String(port),
      LIME_PROJECTS: registryFile,
      LIME_BIN: join(tmp, "no-lime"),
    };
    delete env.PORT;

    proc = Bun.spawn([process.execPath, "run", "server/index.ts"], {
      cwd: resolve(import.meta.dir, ".."),
      env,
      stdout: "ignore",
      stderr: "pipe",
    });

    const deadline = Date.now() + 10_000;
    for (;;) {
      try {
        const res = await fetch(`http://127.0.0.1:${port}/api/health`);
        if (res.ok) break;
      } catch {
        // not up yet
      }
      if (Date.now() > deadline) {
        const err = await new Response(proc.stderr).text();
        throw new Error(`dashboard server did not start:\n${err}`);
      }
      await Bun.sleep(100);
    }
  }, 15_000);

  afterAll(async () => {
    proc.kill();
    await proc.exited;
    rmSync(tmp, { recursive: true, force: true });
  });

  test("foreign Host on /api/health is refused", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/health`, {
      headers: { host: "evil.example:4600" },
    });
    expect(res.status).toBe(403);
  });

  test("foreign Host on / is refused", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/`, {
      headers: { host: "evil.example:4600" },
    });
    expect(res.status).toBe(403);
  });

  test("a local health request is served", async () => {
    const res = await fetch(`http://127.0.0.1:${port}/api/health`);
    expect(res.status).toBe(200);
  });

  test("a foreign Origin on a write is refused", async () => {
    const res = await fetch(
      `http://127.0.0.1:${port}/api/projects/nope/profiles`,
      {
        method: "PUT",
        headers: {
          origin: "http://evil.example",
          "content-type": "application/json",
        },
        body: "{}",
      },
    );
    expect(res.status).toBe(403);
  });

  test("an allowed Origin reaches the handler", async () => {
    const res = await fetch(
      `http://127.0.0.1:${port}/api/projects/nope/profiles`,
      {
        method: "PUT",
        headers: {
          origin: "http://localhost:4601",
          "content-type": "application/json",
        },
        body: "{}",
      },
    );
    expect(res.status).not.toBe(403);
    expect(res.status).toBe(404);
  });

  test("only loopback is reachable", async () => {
    const ip = firstExternalIPv4();
    if (!ip) return; // no non-internal interface on this host
    await expect(fetch(`http://${ip}:${port}/api/health`)).rejects.toThrow();
    await expect(fetch(`http://${ip}:${port}/api/live`)).rejects.toThrow();
  });
});
