/**
 * lime dashboard server — JSON API over every registered project's lime.db,
 * plus the built UI when ./dist exists. The dbs stay read-only: no write route
 * touches any database, no ingest endpoint, no websocket. The write routes are
 * PUT /api/projects/:project/profiles, which rewrites the working tree's
 * .lime/config.yaml by shelling out to `lime profiles set` (Python owns the
 * schema, the validation and the span-preserving rewrite), and the file API's
 * save, create, rename and trash (server/files.ts). Both are confined to
 * registered roots, refuse `.git/`, and never touch any db or run git writes.
 * The data path is
 * spine → sqlite → web ui, and the UI gets there by polling, plus one
 * server-sent-events live feed, `GET /api/live` (`server/feed.ts`). It carries
 * `focus` from the Focus watcher and `running`, `run` and `events` from
 * lime.db through `FeedSource`.
 *
 *   bun run server/index.ts
 *   bun run server/index.ts --registry /path/to/projects
 *   LIME_PROJECTS=/path/to/projects bun run server/index.ts
 *
 * Port: PORT ?? LIME_PORT ?? 4600 (server/port.ts). Inside a Run the port
 * comes from LIME_PORT, so a worktree never binds the owner's 4600;
 * `lime dash --port N` sets PORT.
 *
 * Home: POST /api/home/visit spawns `lime dash visit`; GET /api/home/focus and
 * the GET /api/live server-sent-events feed ride one watcher
 * (server/focusWatch.ts), which spawns `lime status --json` and
 * `lime focus list --json` (server/limeProxy.ts) and shares a single-flight,
 * gap-gated read with both; the home repo is written only by lime. POST
 * /api/home/tick spawns the owner's tick commands through lime.
 *
 * Binds 127.0.0.1 only; local-only.ts refuses a foreign Host, and a foreign
 * Origin on writes.
 */
import { existsSync, statSync } from "node:fs";
import { join, resolve, sep } from "node:path";
import { apiPort } from "./port.ts";
import { LimeRegistry, resolveRegistryPath } from "./db.ts";
import { getProfiles, modelCatalog, putProfiles, batchMergeProfiles } from "./profiles.ts";
import { homeTick, homeVisit, tickArgs } from "./limeProxy.ts";
import { LimeFeedSource, liveStream, parseCursors } from "./feed.ts";
import { FocusWatcher } from "./focusWatch.ts";
import {
  FileRefused,
  listDir,
  readText,
  saveText,
  createEntry,
  renameEntry,
  trashEntry,
  fileIndex,
} from "./files.ts";
import { refuseUnlessLocal, UI_PORT } from "./local-only.ts";
import type {
  ApiError,
  FileCreateRequest,
  FileRefusalBody,
  FileRenameRequest,
  FileSaveRequest,
  FileTrashRequest,
  HealthResponse,
  HomeFocusResponse,
  HomeTickRequest,
  HomeVisitResponse,
  ProfilesPayload,
  ProfileSyncRequest,
  ProfileSyncResponse,
  RolePrompts,
} from "../shared/types.ts";

const PORT = apiPort();
const ALLOWED_PORTS = [PORT, UI_PORT] as const;
const DIST_DIR = resolve(import.meta.dir, "..", "dist");

let registry: LimeRegistry;
try {
  registry = new LimeRegistry(resolveRegistryPath());
} catch (error) {
  console.error(`[lime] ${(error as Error).message}`);
  process.exit(1);
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function notFound(message: string): Response {
  return json({ error: message } satisfies ApiError, 404);
}

/** Guard every handler so a malformed query can't take the server down mid-run. */
function safely(
  handler: (req: Request) => Response | Promise<Response>,
): (req: Request) => Promise<Response> {
  return async (req) => {
    const refused = refuseUnlessLocal(req, ALLOWED_PORTS);
    if (refused) return refused;
    try {
      return await handler(req);
    } catch (error) {
      console.error(
        `[lime] ${req.method} ${new URL(req.url).pathname}:`,
        error,
      );
      return json({ error: (error as Error).message } satisfies ApiError, 500);
    }
  };
}

/**
 * run_ids and role names are path segments on disk, so anything that isn't a
 * plain identifier is rejected outright rather than sanitized into something
 * that might still escape the runs directory. Project names are repo dir
 * names and take the same rule.
 */
const SAFE_SEGMENT = /^[A-Za-z0-9._-]+$/;

function isSafeSegment(value: string): boolean {
  return SAFE_SEGMENT.test(value) && value !== "." && value !== "..";
}

function param(req: Request, key: string): string {
  return decodeURIComponent(
    (req as Request & { params: Record<string, string> }).params[key] ?? "",
  );
}

function intQuery(req: Request, key: string, fallback: number): number {
  const raw = new URL(req.url).searchParams.get(key);
  if (raw === null || raw.trim() === "") return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** The project segment every detail route starts with, resolved or a 404. */
function withDb(
  handler: (
    db: import("./db.ts").LimeDb,
    req: Request,
  ) => Response | Promise<Response>,
): (req: Request) => Promise<Response> {
  return safely((req) => {
    const project = param(req, "project");
    const db = registry.get(project);
    if (!db) return notFound(`no project ${project}`);
    return handler(db, req);
  });
}

/**
 * The file routes' common prefix: a safe, registered project root, or a 400
 * for a malformed name and a 404 for an unregistered one. Wrapped in safely(),
 * so the local-only guard runs before the project is even looked up.
 */
function withProjectRoot(
  handler: (root: string, req: Request) => Response | Promise<Response>,
): (req: Request) => Promise<Response> {
  return safely(async (req) => {
    const project = param(req, "project");
    if (!isSafeSegment(project)) {
      return json({ error: "invalid project" } satisfies ApiError, 400);
    }
    const root = registry.projectPath(project);
    if (root === null) return notFound(`no project ${project}`);
    try {
      return await handler(root, req);
    } catch (e) {
      if (e instanceof FileRefused) {
        return json(
          { error: e.message, reason: e.reason } satisfies FileRefusalBody,
          e.status,
        );
      }
      throw e;
    }
  });
}

/** Serve the built SPA if it has been built; otherwise point at the dev server. */
async function serveStatic(req: Request): Promise<Response> {
  const { pathname } = new URL(req.url);

  if (!existsSync(DIST_DIR)) {
    return new Response(
      `lime dashboard API is running on :${PORT}.\n\n` +
        `No ./dist build found. Run "bun run dev" for the Vite dev server ` +
        `(it proxies /api here), or "bun run build" to serve the UI from this process.\n`,
      { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  }

  // Reject traversal before touching the filesystem.
  const candidate = resolve(join(DIST_DIR, pathname));
  if (candidate === DIST_DIR || candidate.startsWith(DIST_DIR + "/")) {
    if (existsSync(candidate) && statSync(candidate).isFile()) {
      return new Response(Bun.file(candidate));
    }
  }

  // SPA fallback: breadcrumb routes are client-side.
  const indexHtml = join(DIST_DIR, "index.html");
  if (existsSync(indexHtml)) {
    return new Response(Bun.file(indexHtml), {
      headers: { "content-type": "text/html; charset=utf-8" },
    });
  }
  return notFound("not found");
}

// The Focus tab's watcher. Constructed now, not started until after the
// startup logs; it publishes fresh reads to the live feed's Focus frames.
const watcher = new FocusWatcher({
  projects: registry.projects().map(({ name, path }) => ({ name, path })),
  registry: registry.registryPath,
});
const feed = new LimeFeedSource(registry, { focus: watcher });

const server = Bun.serve({
  port: PORT,
  hostname: "127.0.0.1",
  routes: {
    "/api/health": safely(() =>
      json({
        ok: true,
        registry: registry.registryPath,
        projects: registry.projects(),
        runs: registry.runCount(),
      } satisfies HealthResponse),
    ),

    "/api/projects": safely(() => json(registry.projects())),

    "/api/runs": safely((req) =>
      json(registry.runs(intQuery(req, "limit", 200))),
    ),

    // Sidebar widget: last 7 days of turn-event spend, summed per provider.
    "/api/spend": safely(() =>
      json(
        registry.spend(
          new Date(Date.now() - 7 * 24 * 3600 * 1000).toISOString(),
        ),
      ),
    ),

    // Cost page: one row per Session of every finished run started in the window.
    "/api/cost": safely((req) => {
      const days = Math.min(365, Math.max(1, intQuery(req, "days", 7)));
      return json(registry.sessionCosts(new Date(Date.now() - days * 24 * 3600 * 1000).toISOString()));
    }),

    "/api/projects/:project/runs/:run_id": withDb((db, req) => {
      const detail = db.runDetail(param(req, "run_id"));
      return detail ? json(detail) : notFound(`no run ${param(req, "run_id")}`);
    }),

    "/api/projects/:project/runs/:run_id/events": withDb((db, req) =>
      json(
        db.events(
          param(req, "run_id"),
          intQuery(req, "after", 0),
          intQuery(req, "limit", 500),
        ),
      ),
    ),

    "/api/projects/:project/runs/:run_id/envelopes": withDb((db, req) =>
      json(db.envelopes(param(req, "run_id"))),
    ),

    "/api/projects/:project/runs/:run_id/gates": withDb((db, req) =>
      json(db.gates(param(req, "run_id"))),
    ),

    // The exact prompts a role's Session was sent, read from the Session dir.
    // Files are the raw record; the db has no copy of them.
    "/api/projects/:project/runs/:run_id/roles/:role/prompts": withDb(
      async (db, req) => {
        const runId = param(req, "run_id");
        const role = param(req, "role");
        if (!isSafeSegment(runId) || !isSafeSegment(role)) {
          return json(
            { error: "invalid run_id or role" } satisfies ApiError,
            400,
          );
        }
        if (!db.run(runId)) return notFound(`no run ${runId}`);

        const dir = db.promptsDir(runId, role);
        // Defense in depth: the segment check already forbids traversal.
        if (
          dir !== null &&
          dir !== db.runsDir &&
          !dir.startsWith(db.runsDir + sep)
        ) {
          return json({ error: "invalid path" } satisfies ApiError, 400);
        }

        // A prompt file is absent whenever the role never ran in this run —
        // a normal state, so it reads as null rather than an error.
        const read = async (name: string): Promise<string | null> => {
          if (!dir) return null;
          const file = Bun.file(join(dir, `${name}.md`));
          return (await file.exists()) ? await file.text() : null;
        };
        return json({
          system: await read("system"),
          user: await read("user"),
        } satisfies RolePrompts);
      },
    ),
    // Sync endpoint: apply profile-model changes to multiple repositories
    // in one batch.  Must come before the catch-all /profiles route below.
    "/api/projects/:project/profiles/sync": safely(async (req) => {
      if (req.method !== "PUT") {
        return json({ error: "method not allowed" } satisfies ApiError, 405);
      }
      const sourceProject = param(req, "project");
      if (!isSafeSegment(sourceProject)) {
        return json({ error: "invalid project" } satisfies ApiError, 400);
      }
      const sourcePath = registry.projectPath(sourceProject);
      if (!sourcePath) return notFound(`no project ${sourceProject}`);

      let body: unknown;
      try {
        body = await req.json();
      } catch {
        return json({ error: "request body must be JSON" } satisfies ApiError, 400);
      }
      const payload = body as ProfileSyncRequest | null;
      if (
        !payload ||
        !Array.isArray(payload.projects) ||
        !Array.isArray(payload.changes) ||
        payload.projects.length === 0 ||
        payload.changes.length === 0
      ) {
        return json(
          { error: 'body must be {"projects": [...], "changes": [...]}' } satisfies ApiError,
          400,
        );
      }
      // Source project must be in the list if they want to include it
      if (!payload.projects.includes(sourceProject)) {
        payload.projects.unshift(sourceProject);
      }

      // Resolve all targets (ordered, deduplicated)
      const targets = registry.resolveProjects(payload.projects);

      // Run the batch merge
      const results = await batchMergeProfiles(targets, payload.changes);

      return json({ results } satisfies ProfileSyncResponse);
    }),

    "/api/projects/:project/profiles": safely(async (req) => {
      if (req.method !== "GET" && req.method !== "PUT") {
        return json({ error: "method not allowed" } satisfies ApiError, 405);
      }
      const project = param(req, "project");
      if (!isSafeSegment(project)) {
        return json({ error: "invalid project" } satisfies ApiError, 400);
      }
      // The config lives in the working tree, not in any db — a project
      // registered before its first Run still has one to read and write.
      const projectPath = registry.projectPath(project);
      if (!projectPath) return notFound(`no project ${project}`);

      if (req.method === "GET") {
        // A `lime profiles get` failure (broken yaml, no lime binary) is a
        // server-side fault: safely() turns the throw into a 500.
        return json(await getProfiles(project, projectPath));
      }

      let body: unknown;
      try {
        body = await req.json();
      } catch {
        return json({ error: "request body must be JSON" } satisfies ApiError, 400);
      }
      const payload = body as ProfilesPayload | null;
      if (
        !payload ||
        !Array.isArray(payload.agents) ||
        typeof payload.profiles !== "object" ||
        payload.profiles === null ||
        Array.isArray(payload.profiles)
      ) {
        return json(
          { error: 'body must be {"agents": [], "profiles": {}}' } satisfies ApiError,
          400,
        );
      }
      const result = await putProfiles(project, projectPath, payload);
      if (!result.ok) {
        return json(
          { error: result.errors[0] ?? "invalid profiles payload", errors: result.errors } satisfies ApiError & {
            errors: string[],
          },
          400,
        );
      }
      return json(result.response);
    }),

    // Project file API (ticket 222): list, index, read, atomic save, create,
    // rename, trash.
    // Every route resolves a registered root through the C1 guard; files.ts
    // confines each path and refuses `.git/`.
    "/api/projects/:project/files": withProjectRoot(async (root, req) => {
      const path = new URL(req.url).searchParams.get("path");
      if (req.method === "GET") return json(listDir(root, path ?? ""));
      if (req.method !== "POST") {
        return json({ error: "method not allowed" } satisfies ApiError, 405);
      }

      let body: unknown;
      try {
        body = await req.json();
      } catch {
        return json({ error: "request body must be JSON" } satisfies ApiError, 400);
      }
      const payload = body as FileCreateRequest | null;
      if (
        !payload ||
        typeof payload.path !== "string" ||
        (payload.kind !== "file" && payload.kind !== "dir")
      ) {
        return json(
          { error: 'body must be {"path": "…", "kind": "file"|"dir"}' } satisfies ApiError,
          400,
        );
      }
      return json(createEntry(root, payload.path, payload.kind), 201);
    }),

    "/api/projects/:project/files/index": withProjectRoot(async (root, req) => {
      if (req.method !== "GET") {
        return json({ error: "method not allowed" } satisfies ApiError, 405);
      }
      return json(await fileIndex(root));
    }),

    "/api/projects/:project/files/content": withProjectRoot(async (root, req) => {
      const path = new URL(req.url).searchParams.get("path");
      if (req.method === "GET") return json(readText(root, path ?? ""));
      if (req.method !== "PUT") {
        return json({ error: "method not allowed" } satisfies ApiError, 405);
      }

      let body: unknown;
      try {
        body = await req.json();
      } catch {
        return json({ error: "request body must be JSON" } satisfies ApiError, 400);
      }
      const payload = body as FileSaveRequest | null;
      if (
        !payload ||
        typeof payload.path !== "string" ||
        typeof payload.content !== "string"
      ) {
        return json(
          { error: 'body must be {"path": "…", "content": "…"}' } satisfies ApiError,
          400,
        );
      }
      return json(saveText(root, payload.path, payload.content));
    }),

    "/api/projects/:project/files/rename": withProjectRoot(async (root, req) => {
      if (req.method !== "POST") {
        return json({ error: "method not allowed" } satisfies ApiError, 405);
      }
      let body: unknown;
      try {
        body = await req.json();
      } catch {
        return json({ error: "request body must be JSON" } satisfies ApiError, 400);
      }
      const payload = body as FileRenameRequest | null;
      if (
        !payload ||
        typeof payload.from !== "string" ||
        typeof payload.to !== "string"
      ) {
        return json(
          { error: 'body must be {"from": "…", "to": "…"}' } satisfies ApiError,
          400,
        );
      }
      return json(renameEntry(root, payload.from, payload.to));
    }),

    "/api/projects/:project/files/trash": withProjectRoot(async (root, req) => {
      if (req.method !== "POST") {
        return json({ error: "method not allowed" } satisfies ApiError, 405);
      }
      let body: unknown;
      try {
        body = await req.json();
      } catch {
        return json({ error: "request body must be JSON" } satisfies ApiError, 400);
      }
      const payload = body as FileTrashRequest | null;
      if (!payload || typeof payload.path !== "string") {
        return json(
          { error: 'body must be {"path": "…"}' } satisfies ApiError,
          400,
        );
      }
      return json(await trashEntry(root, payload.path));
    }),

    // pi's own catalog via `lime models` — every provider pi is authenticated
    // for — else the ledger of (runtime, model) pairs this installation ran.
    "/api/models/catalog": safely(async () => json(await modelCatalog(registry))),

    // Home (ticket 227): the Focus tab's visit. POST-only; a repeated visit is
    // safe because lime records the visit only after its 30-min gap.
    "/api/home/visit": safely(async (req) => {
      if (req.method !== "POST") {
        return json({ error: "method not allowed" } satisfies ApiError, 405);
      }
      const res: HomeVisitResponse = await homeVisit({
        registry: registry.registryPath,
      });
      return json(res);
    }),

    // Home: the owner's tick — one checkbox or status toggle. POST-only; the
    // body is validated here, so a hostile shape is refused with 400 and never
    // spawned. lime makes a repeat tick a no-op, so a repeat POST is safe.
    "/api/home/tick": safely(async (req) => {
      if (req.method !== "POST") {
        return json({ error: "method not allowed" } satisfies ApiError, 405);
      }
      let body: unknown;
      try {
        body = await req.json();
      } catch {
        return json(
          { error: "request body must be JSON" } satisfies ApiError,
          400,
        );
      }
      if (tickArgs(body) === null) {
        return json(
          {
            error:
              'body must be {"kind":"goal","id":"fN","done":bool} or {"kind":"ticket","project":"…","number":"NN","done":bool}',
          } satisfies ApiError,
          400,
        );
      }
      return json(
        await homeTick(body as HomeTickRequest, {
          registry: registry.registryPath,
        }),
      );
    }),

    // Home: the Focus tab's data — status report plus focus goals. It reads
    // through the watcher, so a mount or reconnect joins the single-flight read
    // and reuses a result younger than the read gap instead of spawning a
    // second `lime status` inside the window.
    "/api/home/focus": safely(async () => {
      const res: HomeFocusResponse = await watcher.refresh();
      return json(res);
    }),

    // One server-sent-events live feed: the Focus watcher's reads plus lime.db's
    // running Runs, run headers and event pages, through server/feed.ts. GET
    // only; Origin is checked even on the read, because a long-lived stream is
    // not a same-origin-safe GET.
    "/api/live": safely((req) => {
      if (req.method !== "GET") {
        return json({ error: "method not allowed" } satisfies ApiError, 405);
      }
      const refused = refuseUnlessLocal(req, ALLOWED_PORTS, { originOnGet: true });
      if (refused) return refused;
      const cursors = parseCursors(new URL(req.url).searchParams.getAll("run"));
      if (cursors === null) {
        return json(
          { error: "run must be <project>:<run_id>:<seq>" } satisfies ApiError,
          400,
        );
      }
      server.timeout(req, 0);
      return liveStream({ source: feed, cursors, signal: req.signal });
    }),
  },

  fetch(req) {
    const refused = refuseUnlessLocal(req, ALLOWED_PORTS);
    if (refused) return refused;
    const { pathname } = new URL(req.url);
    if (pathname.startsWith("/api/")) return notFound(`no route ${pathname}`);
    return serveStatic(req);
  },
});

console.log(`[lime] dashboard api  http://127.0.0.1:${server.port}`);
console.log(`[lime] registry       ${registry.registryPath}`);
for (const p of registry.projects()) {
  console.log(
    `[lime]   ${p.name.padEnd(20)} ${p.db_found ? p.path : `${p.path}  (no lime.db yet)`}`,
  );
}
console.log(
  existsSync(DIST_DIR)
    ? `[lime] serving ui from  ${DIST_DIR}`
    : `[lime] no ./dist — use "bun run dev" for the Vite dev server on :4601`,
);

watcher.start();
feed.start();

process.on("SIGINT", () => {
  feed.stop();
  watcher.stop();
  registry.close();
  process.exit(0);
});
