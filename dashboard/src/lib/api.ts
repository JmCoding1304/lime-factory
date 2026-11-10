import type {
  CatalogResponse,
  Envelope,
  EventRow,
  EventsPage,
  FileCreateRequest,
  FileCreated,
  FileRenamed,
  FileRenameRequest,
  FileTrashed,
  FileTrashRequest,
  FileContent,
  FileIndex,
  FileKind,
  FileListing,
  FileRefusalReason,
  FileSaved,
  FileSaveRequest,
  GateResult,
  ProfileModelChange,
  ProfileSyncRequest,
  ProfileSyncResponse,
  ProfilesPayload,
  ProfilesResponse,
  ProjectInfo,
  ProviderSpend,
  RunDetail,
  RunSummary,
  SessionCost,
  HomeFocusResponse,
  HomeVisitResponse,
  HomeTickRequest,
  HomeTickResponse,
} from "./types";

async function getJson(url: string): Promise<unknown> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`GET ${url} → ${res.status}`);
  return res.json();
}

/** Detail routes are project-scoped: run_ids are only unique within a project. */
function runBase(project: string, runId: string): string {
  return `/api/projects/${encodeURIComponent(project)}/runs/${encodeURIComponent(runId)}`;
}

export function fetchProjects(): Promise<ProjectInfo[]> {
  return getJson("/api/projects") as Promise<ProjectInfo[]>;
}

export function fetchRuns(): Promise<RunSummary[]> {
  return getJson("/api/runs") as Promise<RunSummary[]>;
}

/** The sidebar's 7-day spend-by-provider widget. */
export function fetchSpend(): Promise<ProviderSpend[]> {
  return getJson("/api/spend") as Promise<ProviderSpend[]>;
}

export async function fetchRun(
  project: string,
  runId: string,
): Promise<RunDetail> {
  const detail = (await getJson(runBase(project, runId))) as RunDetail;
  return {
    run: detail.run,
    usage: detail.usage ?? { read: 0, written: 0 },
    phases: detail.phases ?? [],
    sessions: detail.sessions ?? [],
  };
}

export async function fetchEvents(
  project: string,
  runId: string,
  after: number,
  limit = 500,
): Promise<EventsPage> {
  const page = (await getJson(
    `${runBase(project, runId)}/events?after=${after}&limit=${limit}`,
  )) as EventsPage;
  return {
    events: page.events ?? [],
    cursor: page.cursor ?? after,
    has_more: page.has_more ?? false,
  };
}

export function fetchEnvelopes(
  project: string,
  runId: string,
): Promise<Envelope[]> {
  return getJson(`${runBase(project, runId)}/envelopes`) as Promise<Envelope[]>;
}

export function fetchGates(
  project: string,
  runId: string,
): Promise<GateResult[]> {
  return getJson(`${runBase(project, runId)}/gates`) as Promise<GateResult[]>;
}

/** The cost page's rows. */
export function fetchCost(days: number): Promise<SessionCost[]> {
  return getJson(`/api/cost?days=${days}`) as Promise<SessionCost[]>;
}

/** Re-exported so components can type event arrays without a second import path. */
export type { EventRow };

// ── profiles + model catalog (ticket 87) ────────────────────────────────────

/** A 400 from PUT profiles — the server's per-rule messages, verbatim. */
export class ApiValidationError extends Error {
  constructor(
    message: string,
    public errors: string[],
  ) {
    super(message);
    this.name = "ApiValidationError";
  }
}

const profilesUrl = (project: string) =>
  `/api/projects/${encodeURIComponent(project)}/profiles`;

export function fetchProfiles(project: string): Promise<ProfilesResponse> {
  return getJson(profilesUrl(project)) as Promise<ProfilesResponse>;
}

/** Full replacement of the `agents:` + `profiles:` blocks. 400 keeps the file. */
export async function putProfiles(
  project: string,
  payload: ProfilesPayload,
): Promise<ProfilesResponse> {
  const url = profilesUrl(project);
  const res = await fetch(url, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.status === 400) {
    let body: { error?: string; errors?: unknown } = {};
    try {
      body = await res.json();
    } catch {
      body = { error: `PUT ${url} → 400` };
    }
    const msg = body.error ?? "invalid profiles payload";
    throw new ApiValidationError(
      msg,
      Array.isArray(body.errors) && body.errors.length
        ? (body.errors as string[])
        : [msg],
    );
  }
  if (!res.ok) throw new Error(`PUT ${url} → ${res.status}`);
  return res.json() as Promise<ProfilesResponse>;
}

export function fetchCatalog(): Promise<CatalogResponse> {
  return getJson("/api/models/catalog") as Promise<CatalogResponse>;
}

/**
 * Batch synchronize profile-model changes across multiple registered projects.
 * Encodes the source project, sends the shared request shape, resolves mixed
 * results normally.  A 400 from the server throws ApiValidationError, matching
 * putProfiles' behaviour; other transport failures throw a plain Error.
 */
export async function syncProfileModels(
  sourceProject: string,
  projects: string[],
  changes: ProfileModelChange[],
): Promise<ProfileSyncResponse> {
  const url = profilesUrl(sourceProject) + "/sync";
  const body: ProfileSyncRequest = { projects, changes };
  const res = await fetch(url, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 400) {
    let errorBody: { error?: string; errors?: unknown } = {};
    try {
      errorBody = await res.json();
    } catch {
      errorBody = { error: `PUT ${url} → 400` };
    }
    const msg = errorBody.error ?? "invalid sync payload";
    throw new ApiValidationError(
      msg,
      Array.isArray(errorBody.errors) && errorBody.errors.length
        ? (errorBody.errors as string[])
        : [msg],
    );
  }
  if (!res.ok) throw new Error(`PUT ${url} → ${res.status}`);
  return res.json() as Promise<ProfileSyncResponse>;
}

// ── project files (ticket 224) ──────────────────────────────────────────────

const filesUrl = (project: string) =>
  `/api/projects/${encodeURIComponent(project)}/files`;

/** A non-2xx file route — the server's message, status and known reason. */
export class FileApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public reason: FileRefusalReason | null,
  ) {
    super(message);
    this.name = "FileApiError";
  }
}

async function fileJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let body: { error?: unknown; reason?: unknown } = {};
    try {
      body = await res.json();
    } catch {
      body = {};
    }
    const message =
      typeof body.error === "string"
        ? body.error
        : `${init?.method ?? "GET"} ${url} → ${res.status}`;
    const reason = typeof body.reason === "string" ? body.reason : null;
    throw new FileApiError(message, res.status, reason as FileRefusalReason | null);
  }
  return res.json() as Promise<T>;
}

export function fetchDir(project: string, path: string): Promise<FileListing> {
  return fileJson(`${filesUrl(project)}?path=${encodeURIComponent(path)}`);
}

export function fetchFileIndex(project: string): Promise<FileIndex> {
  return fileJson(`${filesUrl(project)}/index`);
}

/** Create a file or folder in the project (missing parents are made server-side). */
export function createFileEntry(
  project: string,
  path: string,
  kind: FileKind,
): Promise<FileCreated> {
  return fileJson(filesUrl(project), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ path, kind } satisfies FileCreateRequest),
  });
}

/** Rename a project entry (file or folder) to a project-relative path. */
export function renameFileEntry(
  project: string,
  from: string,
  to: string,
): Promise<FileRenamed> {
  return fileJson(`${filesUrl(project)}/rename`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ from, to } satisfies FileRenameRequest),
  });
}

/** Hand a project entry (file or folder) to the Trash. */
export function trashFileEntry(
  project: string,
  path: string,
): Promise<FileTrashed> {
  return fileJson(`${filesUrl(project)}/trash`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ path } satisfies FileTrashRequest),
  });
}

// The dashboard's only client writer of file content. This overwrites existing
// files and never creates them: the server refuses when the target is missing.
const fileContentUrl = (project: string): string =>
  `/api/projects/${encodeURIComponent(project)}/files/content`;

export function fetchFileContent(
  project: string,
  path: string,
): Promise<FileContent> {
  return fileJson(`${fileContentUrl(project)}?path=${encodeURIComponent(path)}`);
}

export function saveFileContent(
  project: string,
  path: string,
  content: string,
): Promise<FileSaved> {
  return fileJson(fileContentUrl(project), {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ path, content } satisfies FileSaveRequest),
  });
}

// ── home: Focus tab (ticket 227) ────────────────────────────────────────────

/** GET /api/home/focus — the server spawns `lime status --json` and `lime focus list --json`. */
export function fetchHomeFocus(): Promise<HomeFocusResponse> {
  return getJson("/api/home/focus") as Promise<HomeFocusResponse>;
}

/** POST /api/home/visit — the server spawns `lime dash visit`. */
export async function postHomeVisit(): Promise<HomeVisitResponse> {
  const res = await fetch("/api/home/visit", { method: "POST" });
  if (!res.ok) {
    let body: { error?: unknown } = {};
    try {
      body = await res.json();
    } catch {
      body = {};
    }
    const msg =
      typeof body.error === "string" ? body.error : `POST /api/home/visit → ${res.status}`;
    throw new Error(msg);
  }
  return res.json() as Promise<HomeVisitResponse>;
}

/** POST /api/home/tick — the server spawns the matching owner write command. */
export async function postHomeTick(
  req: HomeTickRequest,
): Promise<HomeTickResponse> {
  const res = await fetch("/api/home/tick", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    let body: { error?: unknown } = {};
    try {
      body = await res.json();
    } catch {
      body = {};
    }
    const msg =
      typeof body.error === "string" ? body.error : `POST /api/home/tick → ${res.status}`;
    throw new Error(msg);
  }
  return res.json() as Promise<HomeTickResponse>;
}
