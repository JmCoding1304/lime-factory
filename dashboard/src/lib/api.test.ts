import { afterEach, describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  ApiValidationError,
  createFileEntry,
  fetchCatalog,
  fetchDir,
  fetchFileContent,
  fetchFileIndex,
  fetchProfiles,
  FileApiError,
  fetchHomeFocus,
  postHomeTick,
  postHomeVisit,
  putProfiles,
  renameFileEntry,
  saveFileContent,
  syncProfileModels,
  trashFileEntry,
} from "./api";
import type {
  CatalogResponse,
  FileCreated,
  FileListing,
  FileSaved,
  ProfileModelChange,
  ProfileSyncResponse,
  ProfilesPayload,
  ProfilesResponse,
} from "./types";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

interface Call {
  url: string;
  init: RequestInit | undefined;
}

function mockFetch(body: unknown, status: number): Call[] {
  const calls: Call[] = [];
  globalThis.fetch = (async (
    input: string | URL | Request,
    init?: RequestInit,
  ) => {
    calls.push({ url: String(input), init });
    return new Response(JSON.stringify(body), { status });
  }) as typeof fetch;
  return calls;
}

const PAYLOAD: ProfilesPayload = {
  agents: [{ name: "scout", model: "openrouter/qwen/qwen3-coder-flash" }],
  profiles: { quality: { agents: [{ name: "builder", model: "sonnet" }] } },
};

describe("putProfiles", () => {
  test("putProfiles turns a 400 into ApiValidationError carrying every server message", async () => {
    const errors = [
      "bob: system prompt missing (unconfigured; no package default either)",
      "profile 'quality' patches unknown agent 'bob'",
    ];
    const calls = mockFetch({ error: "invalid profiles payload", errors }, 400);
    let caught: unknown = null;
    try {
      await putProfiles("limefactory", PAYLOAD);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiValidationError);
    const err = caught as ApiValidationError;
    expect(err.name).toBe("ApiValidationError");
    expect(err.message).toBe("invalid profiles payload");
    expect(err.errors).toEqual(errors);
    const call = calls[0];
    expect(call.url).toBe("/api/projects/limefactory/profiles");
    expect(call.init?.method).toBe("PUT");
    const headers = (call.init?.headers ?? {}) as Record<string, string>;
    expect(headers["content-type"]).toBe("application/json");
    expect(call.init?.body).toBe(JSON.stringify(PAYLOAD));
  });

  test("putProfiles returns the fresh roster on 200", async () => {
    const fresh: ProfilesResponse = {
      project: "limefactory",
      config_path: "/x/.lime/config.yaml",
      agents: [],
      profiles: {},
      defaults: {},
      merged: {},
      profiles_list: ["default"],
      thinking_support: {},
    };
    mockFetch(fresh, 200);
    expect(await putProfiles("limefactory", PAYLOAD)).toEqual(fresh);
  });

  test("putProfiles throws a plain Error on 500", async () => {
    mockFetch({ error: "lime binary missing" }, 500);
    let caught: unknown = null;
    try {
      await putProfiles("limefactory", PAYLOAD);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(Error);
    expect(caught).not.toBeInstanceOf(ApiValidationError);
    expect((caught as Error).message).toBe(
      "PUT /api/projects/limefactory/profiles → 500",
    );
  });

  test("putProfiles tolerates a non-JSON 400 body", async () => {
    globalThis.fetch = (async () =>
      new Response("Bad Gateway-ish", { status: 400 })) as unknown as typeof fetch;
    let caught: unknown = null;
    try {
      await putProfiles("p", PAYLOAD);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiValidationError);
    expect((caught as ApiValidationError).message).toBe(
      "PUT /api/projects/p/profiles → 400",
    );
    expect((caught as ApiValidationError).errors).toEqual([
      "PUT /api/projects/p/profiles → 400",
    ]);
  });
});

describe("get urls", () => {
  test("fetchProfiles and fetchCatalog hit the ticket-87 urls", async () => {
    const profiles: ProfilesResponse = {
      project: "my proj",
      config_path: "/x/.lime/config.yaml",
      agents: [],
      profiles: {},
      defaults: {},
      merged: {},
      profiles_list: ["default"],
      thinking_support: {},
    };
    const calls = mockFetch(profiles, 200);
    expect(await fetchProfiles("my proj")).toEqual(profiles);
    const catalog: CatalogResponse = { source: "pi", models: [] };
    const catalogCalls = mockFetch(catalog, 200);
    expect(await fetchCatalog()).toEqual(catalog);
    expect([...calls.map((c) => c.url), ...catalogCalls.map((c) => c.url)]).toEqual([
      "/api/projects/my%20proj/profiles",
      "/api/models/catalog",
    ]);
  });
});

// --- syncProfileModels (step 2) ---

const CHANGES: ProfileModelChange[] = [
  { profile: "quality", role: "builder", patch: { model: "m/a", runtime: "pi" } },
];

describe("syncProfileModels", () => {
  test("encodes URL and body correctly", async () => {
    const result: ProfileSyncResponse = { results: [] };
    const calls = mockFetch(result, 200);
    await syncProfileModels("source-alpha", ["target-beta", "target-gamma"], CHANGES);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/api/projects/source-alpha/profiles/sync");
    expect(calls[0].init?.method).toBe("PUT");
    const headers = (calls[0].init?.headers ?? {}) as Record<string, string>;
    expect(headers["content-type"]).toBe("application/json");
    const body = JSON.parse(calls[0].init?.body as string);
    expect(body.projects).toEqual(["target-beta", "target-gamma"]);
    expect(body.changes).toEqual(CHANGES);
  });

  test("all-success returns the full result array", async () => {
    const result: ProfileSyncResponse = {
      results: [
        { project: "a", ok: true, wrote: true, errors: [] },
        { project: "b", ok: true, wrote: false, errors: [] },
      ],
    };
    mockFetch(result, 200);
    const response = await syncProfileModels("source", ["a", "b"], CHANGES);
    expect(response).toEqual(result);
  });

  test("mixed result: some ok, some not", async () => {
    const result: ProfileSyncResponse = {
      results: [
        { project: "a", ok: true, wrote: true, errors: [] },
        { project: "b", ok: false, wrote: false, errors: ["unknown role"] },
      ],
    };
    mockFetch(result, 200);
    const response = await syncProfileModels("source", ["a", "b"], CHANGES);
    expect(response.results).toHaveLength(2);
    expect(response.results[0].ok).toBe(true);
    expect(response.results[1].ok).toBe(false);
    expect(response.results[1].errors).toContain("unknown role");
  });

  test("malformed request — 400 throws ApiValidationError", async () => {
    const errors = ["projects must be a non-empty list"];
    mockFetch({ error: "invalid body", errors }, 400);
    let caught: unknown = null;
    try {
      await syncProfileModels("p", [], []);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiValidationError);
    const err = caught as ApiValidationError;
    expect(err.errors).toEqual(errors);
    expect(err.message).toBe("invalid body");
  });

  test("non-400 transport failure throws a plain Error", async () => {
    mockFetch({ error: "server error" }, 500);
    let caught: unknown = null;
    try {
      await syncProfileModels("p", ["x", "y"], CHANGES);
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(Error);
    expect(caught).not.toBeInstanceOf(ApiValidationError);
    expect((caught as Error).message).toContain("500");
  });
});

// --- file API client (ticket 224, step 1) ---

describe("file API client", () => {
  test("fetchDir encodes the project and the path", async () => {
    const listing: FileListing = {
      path: "src/a b",
      entries: [{ name: "a b", path: "src/a b", dir: true }],
    };
    const calls = mockFetch(listing, 200);
    expect(await fetchDir("my proj", "src/a b")).toEqual(listing);
    expect(calls[0].url).toBe("/api/projects/my%20proj/files?path=src%2Fa%20b");
  });

  test("fetchFileIndex reads the index route", async () => {
    const calls = mockFetch({ paths: ["a.ts"] }, 200);
    expect(await fetchFileIndex("limefactory")).toEqual({ paths: ["a.ts"] });
    expect(calls[0].url).toBe("/api/projects/limefactory/files/index");
  });

  test("a refused file route throws FileApiError with the server's reason", async () => {
    mockFetch({ error: "not found", reason: "not_found" }, 404);
    let caught: unknown = null;
    try {
      await fetchFileIndex("p");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(FileApiError);
    const err = caught as FileApiError;
    expect(err.status).toBe(404);
    expect(err.reason).toBe("not_found");
    expect(err.message).toBe("not found");
  });

  test("a non-JSON failure keeps the status and has no reason", async () => {
    globalThis.fetch = (async () =>
      new Response("oops", { status: 500 })) as unknown as typeof fetch;
    let caught: unknown = null;
    try {
      await fetchFileIndex("p");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(FileApiError);
    const err = caught as FileApiError;
    expect(err.reason).toBeNull();
    expect(err.status).toBe(500);
    expect(err.message.endsWith("→ 500")).toBe(true);
  });

  test("createFileEntry posts the path and kind as JSON", async () => {
    const created: FileCreated = { path: "docs/new.md", kind: "file" };
    const calls = mockFetch(created, 201);
    expect(await createFileEntry("limefactory", "docs/new.md", "file")).toEqual(
      created,
    );
    const call = calls[0];
    expect(call.url).toBe("/api/projects/limefactory/files");
    expect(call.init?.method).toBe("POST");
    const headers = (call.init?.headers ?? {}) as Record<string, string>;
    expect(headers["content-type"]).toBe("application/json");
    expect(call.init?.body).toBe(
      JSON.stringify({ path: "docs/new.md", kind: "file" }),
    );
  });

  test("createFileEntry surfaces a 409 as FileApiError reason exists", async () => {
    const calls = mockFetch({ error: "already exists", reason: "exists" }, 409);
    let caught: unknown = null;
    try {
      await createFileEntry("p", "src/app.ts", "file");
    } catch (e) {
      caught = e;
    }
    expect(calls).toHaveLength(1);
    expect(caught).toBeInstanceOf(FileApiError);
    const err = caught as FileApiError;
    expect(err.status).toBe(409);
    expect(err.reason).toBe("exists");
  });

  test("renameFileEntry POSTs from and to", async () => {
    const renamed = { from: "a.md", to: "b.md" };
    const calls = mockFetch(renamed, 200);
    expect(await renameFileEntry("my proj", "a.md", "b.md")).toEqual(renamed);
    const call = calls[0];
    expect(call.url).toBe("/api/projects/my%20proj/files/rename");
    expect(call.init?.method).toBe("POST");
    const headers = (call.init?.headers ?? {}) as Record<string, string>;
    expect(headers["content-type"]).toBe("application/json");
    expect(JSON.parse(String(call.init?.body))).toEqual({
      from: "a.md",
      to: "b.md",
    });
  });

  test("trashFileEntry POSTs the path", async () => {
    const trashed = { path: "notes.md" };
    const calls = mockFetch(trashed, 200);
    expect(await trashFileEntry("limefactory", "notes.md")).toEqual(trashed);
    const call = calls[0];
    expect(call.url).toBe("/api/projects/limefactory/files/trash");
    expect(call.init?.method).toBe("POST");
    const headers = (call.init?.headers ?? {}) as Record<string, string>;
    expect(headers["content-type"]).toBe("application/json");
    expect(JSON.parse(String(call.init?.body))).toEqual({ path: "notes.md" });
  });

  test("a refused rename keeps its reason", async () => {
    mockFetch({ error: "already exists", reason: "exists" }, 409);
    let caught: unknown = null;
    try {
      await renameFileEntry("p", "a.md", "b.md");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(FileApiError);
    expect((caught as FileApiError).reason).toBe("exists");
  });
});

describe("file content", () => {
  test("fetchFileContent GETs the encoded ?path= URL", async () => {
    const content = { path: "a b.md", content: "hi", size: 2, mtime_ms: 1 };
    const calls = mockFetch(content, 200);
    expect(await fetchFileContent("limefactory", "a b.md")).toEqual(content);
    expect(calls[0].url).toBe(
      "/api/projects/limefactory/files/content?path=a%20b.md",
    );
    expect(calls[0].init?.method).toBeUndefined();
  });

  test("saveFileContent PUTs a JSON body and returns the parsed FileSaved", async () => {
    const saved: FileSaved = { path: "a.md", size: 5, mtime_ms: 9 };
    const calls = mockFetch(saved, 200);
    expect(await saveFileContent("p", "a.md", "hello")).toEqual(saved);
    const call = calls[0];
    expect(call.url).toBe("/api/projects/p/files/content");
    expect(call.init?.method).toBe("PUT");
    const headers = (call.init?.headers ?? {}) as Record<string, string>;
    expect(headers["content-type"]).toBe("application/json");
    expect(call.init?.body).toBe(JSON.stringify({ path: "a.md", content: "hello" }));
  });

  test("a 404 rejects with a FileApiError carrying message, status and reason", async () => {
    mockFetch({ error: "file not found", reason: "not_found" }, 404);
    let caught: unknown = null;
    try {
      await fetchFileContent("p", "gone.md");
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(FileApiError);
    const err = caught as FileApiError;
    expect(err.message).toBe("file not found");
    expect(err.status).toBe(404);
    expect(err.reason).toBe("not_found");
  });

  test("only lib/api.ts names the files/content route", () => {
    const dir = join(import.meta.dir, "..");
    const files = readdirSync(dir, { recursive: true }) as string[];
    const hits = files
      .map((f) => f.split("\\").join("/"))
      .filter((f) => (f.endsWith(".ts") || f.endsWith(".vue")) && !f.endsWith(".test.ts"))
      .filter((f) => readFileSync(join(dir, f), "utf8").includes("files/content"));
    expect(hits).toEqual(["lib/api.ts"]);
  });
});

describe("home fetchers", () => {
  test("postHomeVisit POSTs /api/home/visit", async () => {
    const calls = mockFetch({ ok: true, home_missing: false, message: null }, 200);
    const res = await postHomeVisit();
    expect(res.ok).toBe(true);
    expect(calls[0].url).toBe("/api/home/visit");
    expect(calls[0].init?.method).toBe("POST");
  });

  test("fetchHomeFocus GETs /api/home/focus", async () => {
    const body = { report: null, goals: null, errors: [] };
    const calls = mockFetch(body, 200);
    expect(await fetchHomeFocus()).toEqual(body);
    expect(calls[0].url).toBe("/api/home/focus");
    expect(calls[0].init?.method).toBeUndefined();
  });

  test("postHomeVisit throws the server's error on a non-2xx response", async () => {
    mockFetch({ error: "lime dash visit failed" }, 500);
    let caught: unknown = null;
    try {
      await postHomeVisit();
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toBe("lime dash visit failed");
  });

  test("postHomeTick POSTs JSON to /api/home/tick", async () => {
    const calls = mockFetch({ ok: true, message: null }, 200);
    const req = { kind: "goal", id: "f1", done: true } as const;
    expect(await postHomeTick(req)).toEqual({ ok: true, message: null });
    expect(calls[0].url).toBe("/api/home/tick");
    expect(calls[0].init?.method).toBe("POST");
    expect((calls[0].init?.headers as Record<string, string>)["content-type"]).toBe(
      "application/json",
    );
    expect(calls[0].init?.body).toBe(JSON.stringify(req));
  });

  test("postHomeTick throws the server's error on a non-2xx response", async () => {
    mockFetch({ error: "request body must be JSON" }, 400);
    let caught: unknown = null;
    try {
      await postHomeTick({ kind: "goal", id: "f1", done: true });
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(Error);
    expect((caught as Error).message).toBe("request body must be JSON");
  });
});