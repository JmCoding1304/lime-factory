// The inline-create rules for the file tree: naming, duplicate detection and
// the refusal-text mapping. Pure apart from the injected create call, so the
// UI layer (FileTree.vue) stays a thin shell over this module.
import { FileApiError, createFileEntry, renameFileEntry, trashFileEntry } from "./api";
import type {
  FileCreated, FileEntry, FileKind, FileRenamed, FileTrashed,
} from "./types";

export const BAD_NAME = "Enter a valid name";
export const TAKEN = "Already exists";

/** Join a parent dir and a raw name into a project-relative path, or refuse. */
export function entryPathFor(
  parent: string,
  raw: string,
): { path: string } | { error: string } {
  const clean = raw.trim().replace(/^\/+|\/+$/g, "");
  if (!clean) return { error: BAD_NAME };
  const segments = clean.split("/");
  if (
    segments.some((s) => !s || s === "." || s === ".." || s === ".git")
  ) {
    return { error: BAD_NAME };
  }
  return { path: parent ? `${parent}/${clean}` : clean };
}

/** Turn a thrown create error into the one inline line the tree shows. */
export function refusalText(e: unknown): string {
  if (e instanceof FileApiError) {
    if (e.reason === "exists") return TAKEN;
    if (
      e.reason === "bad_path" ||
      e.reason === "git_dir" ||
      e.reason === "outside_root"
    ) {
      return BAD_NAME;
    }
    if (e.reason === "not_a_dir") return "Parent is not a folder";
    return e.message;
  }
  if (e instanceof Error) return e.message;
  return "Create failed";
}

export interface CreateAsk {
  project: string;
  parent: string;
  raw: string;
  kind: FileKind;
  entries: readonly FileEntry[] | undefined;
}

/** Validate, de-duplicate against the loaded folder, then create exactly once. */
export async function submitCreate(
  ask: CreateAsk,
  create: (
    project: string,
    path: string,
    kind: FileKind,
  ) => Promise<FileCreated> = createFileEntry,
): Promise<{ created: FileCreated } | { error: string }> {
  const resolved = entryPathFor(ask.parent, ask.raw);
  if ("error" in resolved) return resolved;
  const path = resolved.path;
  if (ask.entries?.some((e) => e.path === path)) return { error: TAKEN };
  try {
    return { created: await create(ask.project, path, ask.kind) };
  } catch (e) {
    return { error: refusalText(e) };
  }
}

export interface RenameAsk {
  project: string;
  from: string;
  raw: string;
  entries: readonly FileEntry[] | undefined;
}

/**
 * Validate a new sibling name, de-duplicate against the loaded folder, then
 * rename exactly once. A failure is a `{error}` and nothing is renamed; an
 * unchanged name is a `{renamed: null}` no-op with no call.
 */
export async function submitRename(
  ask: RenameAsk,
  rename: (
    project: string,
    from: string,
    to: string,
  ) => Promise<FileRenamed> = renameFileEntry,
): Promise<{ renamed: FileRenamed | null } | { error: string }> {
  const clean = ask.raw.trim();
  if (!clean || clean.includes("/")) return { error: BAD_NAME };
  const cut = ask.from.lastIndexOf("/");
  const parent = cut < 0 ? "" : ask.from.slice(0, cut);
  const resolved = entryPathFor(parent, clean);
  if ("error" in resolved) return resolved;
  const path = resolved.path;
  if (path === ask.from) return { renamed: null };
  if (ask.entries?.some((e) => e.path === path)) return { error: TAKEN };
  try {
    return { renamed: await rename(ask.project, ask.from, path) };
  } catch (e) {
    return { error: refusalText(e) };
  }
}

/** Hand one project entry to the Trash. One call; a throw is an `{error}`. */
export async function submitTrash(
  project: string,
  path: string,
  trash: (project: string, path: string) => Promise<FileTrashed> = trashFileEntry,
): Promise<{ trashed: FileTrashed } | { error: string }> {
  try {
    return { trashed: await trash(project, path) };
  } catch (e) {
    return { error: refusalText(e) };
  }
}

/** The one confirm dialog's copy: a title naming the item, and a detail that
 *  warns about unsaved tabs or points at the Finder Trash. */
export function trashConfirmText(
  name: string,
  dirty: readonly string[],
): { title: string; detail: string } {
  return {
    title: `Move “${name}” to the Trash?`,
    detail: dirty.length
      ? `Unsaved changes in ${dirty.join(", ")} will be lost.`
      : "You can restore it from the Finder Trash.",
  };
}
