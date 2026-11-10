/**
 * The dashboard's project-file layer (ticket 222): listing, reading, atomic
 * save, create, rename and trash, all confined to a registered project root.
 *
 * It knows nothing about the registry — every function takes the project's
 * registered absolute `root`. Every request path is resolved through
 * `resolveInRoot`, which refuses traversal, absolute paths, symlink escapes
 * and any `.git` segment; every fs call acts on the resolved real path, never
 * the request string. Trash never unlinks: it hands the entry to the macOS
 * `trash` binary and refuses to report success while the entry is still in
 * place. This is the only non-test module under server/ that calls fs write
 * primitives; the guard test in files.test.ts enforces that.
 */
import {
  chmodSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { randomUUID } from "node:crypto";
import { basename, dirname, isAbsolute, join, relative, sep } from "node:path";
import { FILE_READ_CAP_BYTES } from "../shared/types.ts";
import type {
  FileContent,
  FileCreated,
  FileEntry,
  FileIndex,
  FileKind,
  FileListing,
  FileRefusalReason,
  FileRenamed,
  FileSaved,
  FileTrashed,
} from "../shared/types.ts";

/** reason → HTTP status (the table in the 222 spec). */
const STATUS: Record<FileRefusalReason, number> = {
  bad_path: 400,
  outside_root: 403,
  git_dir: 403,
  not_found: 404,
  not_a_file: 400,
  not_a_dir: 400,
  exists: 409,
  too_large: 413,
  binary: 415,
};

/** A refused project-file request: a reason the routes map to a status. */
export class FileRefused extends Error {
  constructor(readonly reason: FileRefusalReason, message: string) {
    super(message);
    this.name = "FileRefused";
  }

  get status(): number {
    return STATUS[this.reason];
  }
}

/** lstat that treats any failure (ENOENT, ENOTDIR) as "no entry". */
function lstatOrNull(p: string) {
  try {
    return lstatSync(p, { throwIfNoEntry: false });
  } catch {
    return undefined;
  }
}

/** stat that treats any failure (ENOENT, ENOTDIR) as "no entry". */
function statOrNull(p: string) {
  try {
    return statSync(p, { throwIfNoEntry: false });
  } catch {
    return undefined;
  }
}

interface Resolved {
  /** realpathSync(root) — the confinement anchor. */
  realRoot: string;
  /** The absolute, symlink-resolved path every fs call acts on. */
  real: string;
  /** The normalised, "/"-joined request path every response echoes. */
  rel: string;
}

/**
 * Resolve a request path inside `root`, or throw FileRefused. Checks run in
 * order: bad-path syntax, `..`/empty, a missing root, the deepest existing
 * ancestor's realpath (a dangling symlink is an escape), confinement, and the
 * `.git` rule.
 */
export function resolveInRoot(
  root: string,
  rel: unknown,
  { allowRoot = false }: { allowRoot?: boolean } = {},
): Resolved {
  // 1. Reject syntax before normalising: dropping "" would turn /etc/hosts
  //    into etc/hosts.
  if (typeof rel !== "string" || rel.includes("\0") || isAbsolute(rel)) {
    throw new FileRefused("bad_path", "path must be project-relative");
  }

  // 2. Normalise: drop empty and "." segments; refuse "..".
  const segs = rel.split("/").filter((s) => s !== "" && s !== ".");
  if (segs.some((s) => s === "..")) {
    throw new FileRefused("bad_path", "path may not contain ..");
  }
  if (segs.length === 0 && !allowRoot) {
    throw new FileRefused("bad_path", "path is empty");
  }
  const normRel = segs.join("/");

  // 3. The root itself must exist and resolve.
  let realRoot: string;
  try {
    realRoot = realpathSync(root);
  } catch {
    throw new FileRefused("not_found", "project folder missing");
  }

  // 4. Walk down to the deepest ancestor that exists (by lstat), realpath it,
  //    and re-join the still-missing tail. A dangling symlink makes realpath
  //    throw — that is an escape, not a not-found.
  let existing = segs.length;
  for (;;) {
    const p = join(realRoot, ...segs.slice(0, existing));
    if (lstatOrNull(p)) break;
    existing -= 1;
  }
  let realAncestor: string;
  try {
    realAncestor = realpathSync(join(realRoot, ...segs.slice(0, existing)));
  } catch {
    throw new FileRefused("outside_root", "broken symlink");
  }
  const tail = segs.slice(existing);
  const real = tail.length > 0 ? join(realAncestor, ...tail) : realAncestor;

  // 5. Confinement: the real path must be the root or live under it.
  if (real !== realRoot && !real.startsWith(realRoot + sep)) {
    throw new FileRefused("outside_root", "path escapes the project");
  }

  // 6. The `.git` rule: any requested or resolved segment is `.git`.
  const allSegs = [...segs, ...relative(realRoot, real).split(sep)];
  if (allSegs.some((s) => s.toLowerCase() === ".git")) {
    throw new FileRefused("git_dir", ".git is off limits");
  }

  return { realRoot, real, rel: normRel };
}

/** GET …/files?path= — the entries of a project directory. */
export function listDir(root: string, rel: string): FileListing {
  const { real, rel: normRel } = resolveInRoot(root, rel, { allowRoot: true });
  const st = statOrNull(real);
  if (!st) throw new FileRefused("not_found", "not found");
  if (!st.isDirectory()) throw new FileRefused("not_a_dir", "not a directory");

  const entries: FileEntry[] = [];
  for (const d of readdirSync(real, { withFileTypes: true })) {
    if (d.name.toLowerCase() === ".git") continue;
    let dir = d.isDirectory();
    if (d.isSymbolicLink()) {
      try {
        dir = statSync(join(real, d.name)).isDirectory();
      } catch {
        dir = false;
      }
    }
    entries.push({
      name: d.name,
      path: normRel ? normRel + "/" + d.name : d.name,
      dir,
    });
  }
  entries.sort(
    (a, b) =>
      (a.dir === b.dir ? 0 : a.dir ? -1 : 1) ||
      a.name.localeCompare(b.name, undefined, {
        sensitivity: "base",
        numeric: true,
      }),
  );
  return { path: normRel, entries };
}

/** GET …/files/content — a text file under the 2 MB cap. */
export function readText(root: string, rel: string): FileContent {
  const { real, rel: normRel } = resolveInRoot(root, rel);
  const st = statOrNull(real);
  if (!st) throw new FileRefused("not_found", "not found");
  if (!st.isFile()) throw new FileRefused("not_a_file", "not a file");
  if (st.size > FILE_READ_CAP_BYTES) {
    throw new FileRefused("too_large", "file is larger than 2 MB");
  }
  const buf = readFileSync(real);
  if (buf.length > FILE_READ_CAP_BYTES) {
    throw new FileRefused("too_large", "file is larger than 2 MB");
  }
  if (buf.includes(0)) throw new FileRefused("binary", "binary file — not shown");

  let content: string;
  try {
    content = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(buf);
  } catch {
    throw new FileRefused("binary", "binary file — not shown");
  }
  return { path: normRel, content, size: st.size, mtime_ms: st.mtimeMs };
}

/**
 * PUT …/files/content — overwrite an existing file atomically. Writes a temp
 * sibling, copies the original mode, then renames over the target; a throw
 * removes the temp and leaves the old bytes intact. Save never creates.
 */
export function saveText(root: string, rel: string, content: string): FileSaved {
  const { real, rel: normRel } = resolveInRoot(root, rel);
  const st = statOrNull(real);
  if (!st) throw new FileRefused("not_found", "not found");
  if (!st.isFile()) throw new FileRefused("not_a_file", "not a file");

  const tmp = join(
    dirname(real),
    "." + basename(real) + ".lime-save-" + process.pid + "-" + randomUUID().slice(0, 8),
  );
  try {
    writeFileSync(tmp, content, { flag: "wx" });
    chmodSync(tmp, st.mode & 0o7777);
    renameSync(tmp, real);
  } catch (e) {
    try {
      unlinkSync(tmp);
    } catch {
      // temp already gone (or never created) — keep the original error
    }
    throw e;
  }

  const after = statSync(real);
  return { path: normRel, size: after.size, mtime_ms: after.mtimeMs };
}

/** POST …/files — create a file or directory, making missing parents. */
export function createEntry(root: string, rel: string, kind: FileKind): FileCreated {
  if (kind !== "file" && kind !== "dir") {
    throw new FileRefused("bad_path", 'kind must be "file" or "dir"');
  }
  const { real, rel: normRel } = resolveInRoot(root, rel);

  // Nothing is touched until we know the target is new and its parent is a dir.
  if (lstatOrNull(real)) {
    throw new FileRefused("exists", "already exists");
  }
  let ancestor = dirname(real);
  for (;;) {
    if (lstatOrNull(ancestor)) break;
    const parent = dirname(ancestor);
    if (parent === ancestor) break;
    ancestor = parent;
  }
  const anc = statOrNull(ancestor);
  if (!anc || !anc.isDirectory()) {
    throw new FileRefused("not_a_dir", "parent is not a directory");
  }

  mkdirSync(dirname(real), { recursive: true });
  try {
    if (kind === "file") writeFileSync(real, "", { flag: "wx" });
    else mkdirSync(real);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "EEXIST") {
      throw new FileRefused("exists", "already exists");
    }
    throw e;
  }
  return { path: normRel, kind };
}

/**
 * Resolve a request path to the entry itself, or throw FileRefused. The entry
 * is the parent's realpath joined with the final name, so the verb acts on the
 * entry (a symlink stays a link, never its target). A path that resolves to
 * the project root itself — e.g. a `self -> .` link — is refused.
 */
function resolveEntry(root: string, rel: unknown): { entry: string; rel: string } {
  const r = resolveInRoot(root, rel);
  if (r.real === r.realRoot) {
    throw new FileRefused("bad_path", "the project root cannot be renamed or trashed");
  }
  const cut = r.rel.lastIndexOf("/");
  const parent = resolveInRoot(root, cut < 0 ? "" : r.rel.slice(0, cut), {
    allowRoot: true,
  });
  return { entry: join(parent.real, r.rel.slice(cut + 1)), rel: r.rel };
}

/** POST …/files/rename — move an entry within the project. */
export function renameEntry(root: string, from: unknown, to: unknown): FileRenamed {
  const src = resolveEntry(root, from);
  const dst = resolveEntry(root, to);

  const st = lstatOrNull(src.entry);
  if (!st) throw new FileRefused("not_found", "not found");

  const pst = statOrNull(dirname(dst.entry));
  if (!pst) throw new FileRefused("not_found", "destination folder not found");
  if (!pst.isDirectory()) {
    throw new FileRefused("not_a_dir", "destination parent is not a folder");
  }
  if (dst.entry.startsWith(src.entry + sep)) {
    throw new FileRefused("bad_path", "cannot move a folder into itself");
  }

  // A case-only rename of the same entry (same dev+ino, equal ignoring case)
  // is allowed; any other existing destination is refused.
  const there = lstatOrNull(dst.entry);
  if (
    there &&
    !(
      there.dev === st.dev &&
      there.ino === st.ino &&
      src.rel.toLowerCase() === dst.rel.toLowerCase()
    )
  ) {
    throw new FileRefused("exists", "already exists");
  }

  try {
    renameSync(src.entry, dst.entry);
  } catch (e) {
    const err = e as NodeJS.ErrnoException;
    throw new Error(`could not rename ${src.rel}: ${err.code ?? err.message}`);
  }
  return { from: src.rel, to: dst.rel };
}

/** The gist of `trash`'s stderr: the NSError reason, else the trimmed text. */
function reasonOf(stderr: string, code: number): string {
  const m = stderr.match(/Code=\d+ "([^"]*)"/);
  if (m) return m[1];
  const trimmed = stderr.trim();
  return trimmed ? trimmed.slice(0, 300) : `trash exited ${code}`;
}

/**
 * Hand `abs` to the macOS `trash` binary. Never unlinks: a non-zero exit (or a
 * spawn failure) throws, so the caller knows the entry may still be in place.
 */
export async function moveToTrash(abs: string): Promise<void> {
  const spawnTrash = () =>
    Bun.spawn(["trash", abs], { stdout: "ignore" as const, stderr: "pipe" as const });
  let proc: ReturnType<typeof spawnTrash>;
  try {
    proc = spawnTrash();
  } catch (e) {
    throw new Error("could not move to the Trash: " + (e as Error).message);
  }
  const [stderr] = await Promise.all([
    new Response(proc.stderr).text(),
    proc.exited,
  ]);
  if (proc.exitCode !== 0) {
    throw new Error(
      "could not move to the Trash: " + reasonOf(stderr, proc.exitCode ?? 0),
    );
  }
}

/** POST …/files/trash — hand the entry itself to the Trash mover. */
export async function trashEntry(
  root: string,
  rel: unknown,
  mover: (abs: string) => Promise<void> = moveToTrash,
): Promise<FileTrashed> {
  const target = resolveEntry(root, rel);
  if (!lstatOrNull(target.entry)) {
    throw new FileRefused("not_found", "not found");
  }
  await mover(target.entry);
  if (lstatOrNull(target.entry)) {
    throw new Error(`could not move ${target.rel} to the Trash: it is still in place`);
  }
  return { path: target.rel };
}

/** process.env without git's own overrides, so `-C` picks the repo. */
export function gitEnv(): NodeJS.ProcessEnv {
  const env = { ...process.env };
  delete env.GIT_DIR;
  delete env.GIT_WORK_TREE;
  delete env.GIT_INDEX_FILE;
  return env;
}

/** GET …/files/index — tracked + untracked, non-ignored paths. */
export async function fileIndex(root: string): Promise<FileIndex> {
  const { realRoot } = resolveInRoot(root, "", { allowRoot: true });
  const proc = Bun.spawn(
    [
      "git",
      "-C",
      realRoot,
      "ls-files",
      "-z",
      "--cached",
      "--others",
      "--exclude-standard",
    ],
    { stdout: "pipe", stderr: "pipe", env: gitEnv() },
  );
  const [stdout, stderr] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ]);
  if (proc.exitCode !== 0) {
    throw new Error("git ls-files failed: " + stderr.trim());
  }
  const paths = [...new Set(stdout.split("\0").filter((p) => p !== ""))];
  return { paths };
}
