/**
 * Unit tests for the pure file layer (ticket 222, step 1). No server, no
 * registry: every case drives files.ts with a temp project root and proves the
 * confinement, atomicity and `.git` rules the HTTP step will inherit.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import { FILE_READ_CAP_BYTES } from "../shared/types.ts";
import {
  FileRefused,
  createEntry,
  fileIndex,
  gitEnv,
  listDir,
  readText,
  renameEntry,
  saveText,
  trashEntry,
} from "./files.ts";

/** The refusal reason of the FileRefused `fn` throws (rethrows anything else). */
function refusal(fn: () => unknown): string {
  try {
    fn();
  } catch (e) {
    if (e instanceof FileRefused) return e.reason;
    throw e;
  }
  throw new Error("expected a FileRefused");
}

describe("file API core", () => {
  let tmp = "";
  let root = "";
  let outside = "";

  beforeAll(() => {
    tmp = mkdtempSync(join(tmpdir(), "lime-files-"));
    root = join(tmp, "proj");
    outside = join(tmp, "outside");
    mkdirSync(join(root, "src"), { recursive: true });
    mkdirSync(join(root, "node_modules", "pkg"), { recursive: true });
    mkdirSync(outside, { recursive: true });

    writeFileSync(join(root, "README.md"), "# readme\n");
    writeFileSync(join(root, "src", "a.ts"), "export const a = 1;\n");
    writeFileSync(join(root, "notes.md"), "notes\n");
    writeFileSync(join(root, ".gitignore"), "node_modules/\n");
    writeFileSync(join(root, "node_modules", "pkg", "index.js"), "x\n");
    writeFileSync(join(root, "bin.dat"), Buffer.from([0x89, 0x50, 0x00, 0x01]));
    writeFileSync(
      join(root, "latin1.txt"),
      Buffer.from([0x63, 0x61, 0xe9, 0x0a]),
    );
    writeFileSync(join(root, "big.txt"), Buffer.alloc(FILE_READ_CAP_BYTES + 1, 0x61));
    writeFileSync(join(root, "cap.txt"), Buffer.alloc(FILE_READ_CAP_BYTES, 0x61));
    writeFileSync(join(outside, "secret.txt"), "secret");

    symlinkSync(outside, join(root, "out"));
    symlinkSync(join(outside, "secret.txt"), join(root, "leak.txt"));

    const init = Bun.spawnSync(["git", "init", "-q"], { cwd: root, env: gitEnv() });
    expect(init.exitCode).toBe(0);
    const add = Bun.spawnSync(["git", "add", "README.md", "src/a.ts", ".gitignore"], {
      cwd: root,
      env: gitEnv(),
    });
    expect(add.exitCode).toBe(0);

    symlinkSync(".git", join(root, "gitlink"));
  });

  afterAll(() => {
    rmSync(tmp, { recursive: true, force: true });
  });

  // ── Listing and index ─────────────────────────────────────────────────────

  test("lists the root dirs-first and omits .git", () => {
    const listing = listDir(root, "");
    const names = listing.entries.map((e) => e.name);
    expect(names).not.toContain(".git");

    const firstFile = listing.entries.findIndex((e) => !e.dir);
    const lastDir = listing.entries.reduce((acc, e, i) => (e.dir ? i : acc), -1);
    expect(lastDir).toBeLessThan(firstFile);

    expect(names.indexOf("src")).toBeLessThan(names.indexOf("README.md"));
  });

  test("lists a subdir and refuses a file or a missing path", () => {
    expect(listDir(root, "src")).toEqual({
      path: "src",
      entries: [{ name: "a.ts", path: "src/a.ts", dir: false }],
    });
    expect(refusal(() => listDir(root, "README.md"))).toBe("not_a_dir");
    expect(refusal(() => listDir(root, "nope"))).toBe("not_found");
  });

  test("indexes tracked and untracked but no ignored paths", async () => {
    const { paths } = await fileIndex(root);
    expect(paths).toContain("README.md");
    expect(paths).toContain("src/a.ts");
    expect(paths).toContain("notes.md");
    expect(paths.some((p) => p.startsWith("node_modules/"))).toBe(false);
    expect(paths.some((p) => p.startsWith(".git/"))).toBe(false);
  });

  // ── Reads ─────────────────────────────────────────────────────────────────

  test("reads a text file with its size", () => {
    const read = readText(root, "README.md");
    expect(read.path).toBe("README.md");
    expect(read.content).toBe("# readme\n");
    expect(read.size).toBe(Buffer.byteLength("# readme\n"));
  });

  test("refuses binary content", () => {
    expect(refusal(() => readText(root, "bin.dat"))).toBe("binary");
    expect(refusal(() => readText(root, "latin1.txt"))).toBe("binary");
  });

  test("enforces the read cap", () => {
    expect(refusal(() => readText(root, "big.txt"))).toBe("too_large");
    expect(readText(root, "cap.txt").size).toBe(FILE_READ_CAP_BYTES);
  });

  // ── Path refusals ─────────────────────────────────────────────────────────

  test("refuses `..` traversal everywhere", () => {
    expect(refusal(() => readText(root, "../outside/secret.txt"))).toBe("bad_path");
    expect(refusal(() => readText(root, "src/../../outside/secret.txt"))).toBe("bad_path");
    expect(refusal(() => saveText(root, "../outside/secret.txt", "x"))).toBe("bad_path");
    expect(refusal(() => createEntry(root, "../outside/secret.txt", "file"))).toBe("bad_path");
    expect(refusal(() => listDir(root, "../outside/secret.txt"))).toBe("bad_path");
    expect(readFileSync(join(outside, "secret.txt"), "utf8")).toBe("secret");
  });

  test("refuses absolute paths", () => {
    expect(refusal(() => readText(root, "/etc/hosts"))).toBe("bad_path");
    expect(refusal(() => readText(root, join(outside, "secret.txt")))).toBe("bad_path");
    expect(refusal(() => saveText(root, "/etc/hosts", "x"))).toBe("bad_path");
    expect(refusal(() => saveText(root, join(outside, "secret.txt"), "x"))).toBe("bad_path");
    expect(refusal(() => createEntry(root, "/etc/hosts", "file"))).toBe("bad_path");
    expect(refusal(() => createEntry(root, join(outside, "secret.txt"), "file"))).toBe("bad_path");
  });

  test("refuses symlink escapes", () => {
    expect(refusal(() => readText(root, "out/secret.txt"))).toBe("outside_root");
    expect(refusal(() => readText(root, "leak.txt"))).toBe("outside_root");
    expect(refusal(() => listDir(root, "out"))).toBe("outside_root");
    expect(refusal(() => saveText(root, "out/secret.txt", "x"))).toBe("outside_root");
    expect(readFileSync(join(outside, "secret.txt"), "utf8")).toBe("secret");
    expect(refusal(() => createEntry(root, "out/new.txt", "file"))).toBe("outside_root");
    expect(existsSync(join(outside, "new.txt"))).toBe(false);
  });

  // ── Saves ─────────────────────────────────────────────────────────────────

  test("saves atomically, preserving mode and leaving no temp file", () => {
    const target = join(root, "save.md");
    writeFileSync(target, "old");
    const before = statSync(target).ino;

    const saved = saveText(root, "save.md", "new content");
    expect(saved.size).toBe(Buffer.byteLength("new content"));
    expect(readFileSync(target, "utf8")).toBe("new content");
    expect(statSync(target).ino).not.toBe(before);
    expect(readdirSync(root).some((n) => n.includes(".lime-save-"))).toBe(false);

    const script = join(root, "run.sh");
    writeFileSync(script, "#!/bin/sh\n");
    chmodSync(script, 0o755);
    saveText(root, "run.sh", "#!/bin/sh\necho hi\n");
    expect(statSync(script).mode & 0o777).toBe(0o755);
  });

  test.skipIf(process.getuid?.() === 0)(
    "a failed save leaves the old bytes and no temp file",
    () => {
      const dir = join(root, "src");
      chmodSync(dir, 0o555);
      try {
        expect(() => saveText(root, "src/a.ts", "clobber")).toThrow();
      } finally {
        chmodSync(dir, 0o755);
      }
      expect(readFileSync(join(root, "src", "a.ts"), "utf8")).toBe("export const a = 1;\n");
      expect(readdirSync(dir).some((n) => n.includes(".lime-save-"))).toBe(false);
    },
  );

  test("save never creates and refuses a directory", () => {
    expect(refusal(() => saveText(root, "missing.md", "x"))).toBe("not_found");
    expect(refusal(() => saveText(root, "src", "x"))).toBe("not_a_file");
  });

  // ── Creates ───────────────────────────────────────────────────────────────

  test("creates files, parents and directories", () => {
    expect(createEntry(root, "new.md", "file")).toEqual({ path: "new.md", kind: "file" });
    expect(readFileSync(join(root, "new.md"), "utf8")).toBe("");
    expect(createEntry(root, "deep/er/x.ts", "file")).toEqual({
      path: "deep/er/x.ts",
      kind: "file",
    });
    expect(existsSync(join(root, "deep", "er", "x.ts"))).toBe(true);
    expect(createEntry(root, "newdir", "dir")).toEqual({ path: "newdir", kind: "dir" });
    expect(statSync(join(root, "newdir")).isDirectory()).toBe(true);

    const names = listDir(root, "").entries.map((e) => e.name);
    expect(names).toContain("new.md");
    expect(names).toContain("newdir");
  });

  test("refuses an existing name without changing it", () => {
    expect(refusal(() => createEntry(root, "README.md", "file"))).toBe("exists");
    expect(readFileSync(join(root, "README.md"), "utf8")).toBe("# readme\n");
    expect(refusal(() => createEntry(root, "src", "file"))).toBe("exists");
    expect(statSync(join(root, "src")).isDirectory()).toBe(true);
    expect(refusal(() => createEntry(root, "src", "dir"))).toBe("exists");
    expect(refusal(() => createEntry(root, "README.md/x", "file"))).toBe("not_a_dir");
    expect(existsSync(join(root, "README.md", "x"))).toBe(false);
  });

  // ── .git ──────────────────────────────────────────────────────────────────

  test("refuses every .git write, including aliases", () => {
    const config = join(root, ".git", "config");
    const before = readFileSync(config, "utf8");

    expect(refusal(() => saveText(root, ".git/config", "x"))).toBe("git_dir");
    expect(readFileSync(config, "utf8")).toBe(before);
    expect(refusal(() => createEntry(root, ".git/hooks/evil", "file"))).toBe("git_dir");
    expect(refusal(() => createEntry(root, ".GIT/evil", "file"))).toBe("git_dir");
    expect(refusal(() => createEntry(root, "sub/.git/x", "file"))).toBe("git_dir");
    expect(refusal(() => createEntry(root, ".git", "dir"))).toBe("git_dir");
    expect(refusal(() => createEntry(root, "gitlink/evil", "file"))).toBe("git_dir");

    expect(existsSync(join(root, ".git", "hooks", "evil"))).toBe(false);
    expect(existsSync(join(root, ".git", "evil"))).toBe(false);
  });

  // ── Writer guard ──────────────────────────────────────────────────────────

  test("server files other than files.ts never call fs write primitives", () => {
    const dir = import.meta.dir;
    const scan = readdirSync(dir).filter(
      (n) => n.endsWith(".ts") && !n.endsWith(".test.ts") && n !== "files.ts",
    );
    const re =
      /\b(writeFileSync|appendFileSync|renameSync|mkdirSync|unlinkSync|rmSync|copyFileSync|symlinkSync|chmodSync|createWriteStream)\s*\(|Bun\.write\s*\(|["']node:fs\/promises["']/;
    for (const f of scan) {
      expect(re.test(readFileSync(join(dir, f), "utf8")), f).toBe(false);
    }
  });
});

/** The refusal reason of the FileRefused `p` rejects with (rethrows anything else). */
async function refusalOf(p: Promise<unknown>): Promise<string> {
  try {
    await p;
  } catch (e) {
    if (e instanceof FileRefused) return e.reason;
    throw e;
  }
  throw new Error("expected a FileRefused");
}

/** A fake Trash mover: records each abs path and renames it into `dir`. */
function trashInto(dir: string): { calls: string[]; fn: (abs: string) => Promise<void> } {
  const calls: string[] = [];
  const fn = async (abs: string) => {
    calls.push(abs);
    renameSync(abs, join(dir, calls.length + "-" + basename(abs)));
  };
  return { calls, fn };
}

describe("rename and trash", () => {
  let tmp = "";
  let root = "";
  let outside = "";

  beforeEach(() => {
    tmp = mkdtempSync(join(tmpdir(), "lime-files-rt-"));
    root = join(tmp, "proj");
    outside = join(tmp, "outside");
    mkdirSync(join(root, "src"), { recursive: true });
    mkdirSync(join(root, "docs"), { recursive: true });
    mkdirSync(outside, { recursive: true });

    writeFileSync(join(root, "README.md"), "# readme\n");
    writeFileSync(join(root, "notes.md"), "notes\n");
    writeFileSync(join(root, "src", "a.ts"), "export const a = 1;\n");
    writeFileSync(join(root, "src", "b.ts"), "export const b = 2;\n");
    writeFileSync(join(root, "docs", "x.md"), "# x\n");
    writeFileSync(join(outside, "secret.txt"), "secret");

    symlinkSync(outside, join(root, "out"));
    symlinkSync(join(outside, "secret.txt"), join(root, "leak.txt"));

    const init = Bun.spawnSync(["git", "init", "-q"], { cwd: root, env: gitEnv() });
    expect(init.exitCode).toBe(0);
    symlinkSync(".git", join(root, "gitlink"));
    symlinkSync(".", join(root, "self"));
  });

  afterEach(() => {
    rmSync(tmp, { recursive: true, force: true });
  });

  test("renames a file and a folder within the project", () => {
    expect(renameEntry(root, "notes.md", "notes2.md")).toEqual({
      from: "notes.md",
      to: "notes2.md",
    });
    expect(readFileSync(join(root, "notes2.md"), "utf8")).toBe("notes\n");
    expect(existsSync(join(root, "notes.md"))).toBe(false);

    expect(renameEntry(root, "src", "lib")).toEqual({ from: "src", to: "lib" });
    expect(existsSync(join(root, "lib", "a.ts"))).toBe(true);
    expect(existsSync(join(root, "src"))).toBe(false);

    expect(renameEntry(root, "docs/x.md", "y.md")).toEqual({
      from: "docs/x.md",
      to: "y.md",
    });
    expect(existsSync(join(root, "y.md"))).toBe(true);
    expect(existsSync(join(root, "docs", "x.md"))).toBe(false);
  });

  test("renaming onto an existing name is refused and overwrites nothing", () => {
    expect(refusal(() => renameEntry(root, "notes.md", "README.md"))).toBe("exists");
    expect(readFileSync(join(root, "README.md"), "utf8")).toBe("# readme\n");
    expect(readFileSync(join(root, "notes.md"), "utf8")).toBe("notes\n");

    expect(refusal(() => renameEntry(root, "src", "docs"))).toBe("exists");
    expect(readFileSync(join(root, "docs", "x.md"), "utf8")).toBe("# x\n");

    expect(refusal(() => renameEntry(root, "notes.md", "src"))).toBe("exists");
  });

  test("a case-only rename is allowed", () => {
    writeFileSync(join(root, "Case.txt"), "case\n");
    expect(renameEntry(root, "Case.txt", "case.txt")).toEqual({
      from: "Case.txt",
      to: "case.txt",
    });
    const names = readdirSync(root);
    expect(names).toContain("case.txt");
    expect(names).not.toContain("Case.txt");
    expect(readFileSync(join(root, "case.txt"), "utf8")).toBe("case\n");
  });

  test("rename refuses `..`, the project root, `.git` and symlink escapes", () => {
    expect(refusal(() => renameEntry(root, "../outside/secret.txt", "../x"))).toBe("bad_path");
    expect(refusal(() => renameEntry(root, "", ""))).toBe("bad_path");
    expect(refusal(() => renameEntry(root, ".", ""))).toBe("bad_path");
    expect(refusal(() => renameEntry(root, "self", "self2"))).toBe("bad_path");
    expect(refusal(() => renameEntry(root, ".git", "git2"))).toBe("git_dir");
    expect(refusal(() => renameEntry(root, "README.md", ".git"))).toBe("git_dir");
    expect(refusal(() => renameEntry(root, "README.md", "src/.git"))).toBe("git_dir");
    expect(refusal(() => renameEntry(root, "gitlink", "gl2"))).toBe("git_dir");
    expect(refusal(() => renameEntry(root, "out/secret.txt", "out/moved.md"))).toBe("outside_root");
    expect(refusal(() => renameEntry(root, "notes.md", "out/moved.md"))).toBe("outside_root");
    expect(refusal(() => renameEntry(root, "leak.txt", "leak2.txt"))).toBe("outside_root");

    mkdirSync(join(root, "src", "inner"));
    expect(refusal(() => renameEntry(root, "src", "src/inner/src"))).toBe("bad_path");

    expect(readFileSync(join(root, "README.md"), "utf8")).toBe("# readme\n");
    expect(readFileSync(join(root, "notes.md"), "utf8")).toBe("notes\n");
    expect(readFileSync(join(outside, "secret.txt"), "utf8")).toBe("secret");
    expect(existsSync(join(outside, "moved.md"))).toBe(false);
  });

  test("rename reports a missing source, folder or root", () => {
    expect(refusal(() => renameEntry(root, "nope.md", "x.md"))).toBe("not_found");
    expect(refusal(() => renameEntry(root, "README.md", "nodir/x.md"))).toBe("not_found");
    expect(refusal(() => renameEntry(root, "README.md", "README.md/x"))).toBe("not_a_dir");
    expect(refusal(() => renameEntry(join(tmp, "gone"), "a.md", "b.md"))).toBe("not_found");
  });

  test.skipIf(process.getuid?.() === 0)(
    "a failed rename reports the error and leaves the file in place",
    () => {
      const dir = join(root, "src");
      chmodSync(dir, 0o555);
      try {
        let caught: unknown;
        try {
          renameEntry(root, "src/a.ts", "src/c.ts");
        } catch (e) {
          caught = e;
        }
        expect(caught).toBeInstanceOf(Error);
        expect(caught instanceof FileRefused).toBe(false);
        expect((caught as Error).message).toContain("could not rename src/a.ts");
      } finally {
        chmodSync(dir, 0o755);
      }
      expect(readFileSync(join(root, "src", "a.ts"), "utf8")).toBe("export const a = 1;\n");
      expect(existsSync(join(root, "src", "c.ts"))).toBe(false);
    },
  );

  test("trash hands the entry itself to the mover", async () => {
    const t1 = trashInto(join(tmp, "trash1"));
    mkdirSync(join(tmp, "trash1"));
    expect(await trashEntry(root, "notes.md", t1.fn)).toEqual({ path: "notes.md" });
    expect(t1.calls).toEqual([join(realpathSync(root), "notes.md")]);
    expect(existsSync(join(root, "notes.md"))).toBe(false);
    expect(readFileSync(join(tmp, "trash1", "1-notes.md"), "utf8")).toBe("notes\n");

    const t2 = trashInto(join(tmp, "trash2"));
    mkdirSync(join(tmp, "trash2"));
    expect(await trashEntry(root, "src", t2.fn)).toEqual({ path: "src" });
    expect(existsSync(join(root, "src"))).toBe(false);
    expect(existsSync(join(tmp, "trash2", "1-src", "a.ts"))).toBe(true);

    symlinkSync(join(root, "README.md"), join(root, "alias.md"));
    const t3 = trashInto(join(tmp, "trash3"));
    mkdirSync(join(tmp, "trash3"));
    expect(await trashEntry(root, "alias.md", t3.fn)).toEqual({ path: "alias.md" });
    expect(existsSync(join(root, "alias.md"))).toBe(false);
    expect(existsSync(join(root, "README.md"))).toBe(true);
  });

  test("trash refuses `..`, the project root, `.git`, symlink escapes and a missing root, without calling the mover", async () => {
    const { calls, fn } = trashInto(join(tmp, "trash"));
    mkdirSync(join(tmp, "trash"));
    const before = readFileSync(join(root, ".git", "config"), "utf8");

    expect(await refusalOf(trashEntry(root, "../outside/secret.txt", fn))).toBe("bad_path");
    expect(await refusalOf(trashEntry(root, "", fn))).toBe("bad_path");
    expect(await refusalOf(trashEntry(root, ".", fn))).toBe("bad_path");
    expect(await refusalOf(trashEntry(root, "self", fn))).toBe("bad_path");
    expect(await refusalOf(trashEntry(root, ".git", fn))).toBe("git_dir");
    expect(await refusalOf(trashEntry(root, ".git/config", fn))).toBe("git_dir");
    expect(await refusalOf(trashEntry(root, "gitlink", fn))).toBe("git_dir");
    expect(await refusalOf(trashEntry(root, "out", fn))).toBe("outside_root");
    expect(await refusalOf(trashEntry(root, "out/secret.txt", fn))).toBe("outside_root");
    expect(await refusalOf(trashEntry(root, "leak.txt", fn))).toBe("outside_root");
    expect(await refusalOf(trashEntry(root, "nope.md", fn))).toBe("not_found");
    expect(await refusalOf(trashEntry(join(tmp, "gone"), "a.md", fn))).toBe("not_found");

    expect(calls.length).toBe(0);
    expect(readFileSync(join(root, ".git", "config"), "utf8")).toBe(before);
    expect(readFileSync(join(outside, "secret.txt"), "utf8")).toBe("secret");
  });

  test("a failed Trash move reports the error and leaves the file in place", async () => {
    const boom = async () => {
      throw new Error("boom");
    };
    await expect(trashEntry(root, "notes.md", boom)).rejects.toThrow("boom");
    expect(existsSync(join(root, "notes.md"))).toBe(true);

    const noop = async () => {};
    await expect(trashEntry(root, "notes.md", noop)).rejects.toThrow(/still in place/);
    expect(existsSync(join(root, "notes.md"))).toBe(true);
  });

  test("files.ts never unlinks a user path", () => {
    const src = readFileSync(join(import.meta.dir, "files.ts"), "utf8");
    expect([...src.matchAll(/\bunlinkSync\s*\(/g)].length).toBe(1);
    expect(/\b(rmSync|rmdirSync)\s*\(|["']node:fs\/promises["']/.test(src)).toBe(false);

    const spawnRe = /spawn(Sync)?\(\s*\[\s*["']trash["']/;
    const matchers = readdirSync(import.meta.dir)
      .filter((n) => n.endsWith(".ts") && !n.endsWith(".test.ts"))
      .filter((f) => spawnRe.test(readFileSync(join(import.meta.dir, f), "utf8")));
    expect(matchers).toEqual(["files.ts"]);
  });
});

describe("file API over HTTP", () => {
  let proc: Bun.Subprocess<"ignore", "ignore", "pipe">;
  let port = 0;
  let tmp = "";
  let project = "";

  const base = (name: string) =>
    `http://127.0.0.1:${port}/api/projects/${name}`;

  beforeAll(async () => {
    tmp = mkdtempSync(join(tmpdir(), "lime-files-http-"));
    const registryFile = join(tmp, "projects");
    project = join(tmp, "web");
    mkdirSync(project, { recursive: true });
    writeFileSync(join(project, "README.md"), "hello");
    writeFileSync(join(project, "bin.dat"), Buffer.from([0x68, 0x00, 0x69]));

    const init = Bun.spawnSync(["git", "init", "-q"], {
      cwd: project,
      env: gitEnv(),
    });
    expect(init.exitCode).toBe(0);
    const add = Bun.spawnSync(["git", "add", "README.md"], {
      cwd: project,
      env: gitEnv(),
    });
    expect(add.exitCode).toBe(0);

    writeFileSync(registryFile, `# repos registered by lime init\n${project}\n`);

    // Never 4600/4601: bind an ephemeral port, learn it, release it, and hand
    // it to the server as LIME_PORT the way a Run does.
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

  // ── Served ────────────────────────────────────────────────────────────────

  test("the routes serve", async () => {
    const list = await fetch(`${base("web")}/files`);
    expect(list.status).toBe(200);
    const listing = (await list.json()) as { entries: { name: string }[] };
    expect(listing.entries.map((e) => e.name)).toContain("README.md");

    const idx = await fetch(`${base("web")}/files/index`);
    expect(idx.status).toBe(200);
    expect(((await idx.json()) as { paths: string[] }).paths).toContain("README.md");

    const content = await fetch(`${base("web")}/files/content?path=README.md`);
    expect(content.status).toBe(200);
    expect(((await content.json()) as { content: string }).content).toBe("hello");
  });

  test("save and create work over HTTP with a local Origin", async () => {
    const origin = `http://127.0.0.1:${port}`;
    const save = await fetch(`${base("web")}/files/content`, {
      method: "PUT",
      headers: { origin, "content-type": "application/json" },
      body: JSON.stringify({ path: "README.md", content: "bye" }),
    });
    expect(save.status).toBe(200);
    expect(readFileSync(join(project, "README.md"), "utf8")).toBe("bye");

    const create = await fetch(`${base("web")}/files`, {
      method: "POST",
      headers: { origin, "content-type": "application/json" },
      body: JSON.stringify({ path: "docs/new.md", kind: "file" }),
    });
    expect(create.status).toBe(201);
    expect(existsSync(join(project, "docs", "new.md"))).toBe(true);

    const dup = await fetch(`${base("web")}/files`, {
      method: "POST",
      headers: { origin, "content-type": "application/json" },
      body: JSON.stringify({ path: "docs/new.md", kind: "file" }),
    });
    expect(dup.status).toBe(409);
    expect(((await dup.json()) as { reason: string }).reason).toBe("exists");
  });

  test("rename works over HTTP with a local Origin", async () => {
    const origin = `http://127.0.0.1:${port}`;
    mkdirSync(join(project, "moves"), { recursive: true });
    writeFileSync(join(project, "moves", "old.md"), "move\n");

    const rename = await fetch(`${base("web")}/files/rename`, {
      method: "POST",
      headers: { origin, "content-type": "application/json" },
      body: JSON.stringify({ from: "moves/old.md", to: "moves/new.md" }),
    });
    expect(rename.status).toBe(200);
    expect(await rename.json()).toEqual({ from: "moves/old.md", to: "moves/new.md" });
    expect(existsSync(join(project, "moves", "new.md"))).toBe(true);
    expect(existsSync(join(project, "moves", "old.md"))).toBe(false);

    const before = readFileSync(join(project, "README.md"), "utf8");
    const onto = await fetch(`${base("web")}/files/rename`, {
      method: "POST",
      headers: { origin, "content-type": "application/json" },
      body: JSON.stringify({ from: "moves/new.md", to: "README.md" }),
    });
    expect(onto.status).toBe(409);
    expect(((await onto.json()) as { reason: string }).reason).toBe("exists");
    expect(readFileSync(join(project, "README.md"), "utf8")).toBe(before);
  });

  // ── Refused ───────────────────────────────────────────────────────────────

  test("an unregistered project gets a 404 on every route", async () => {
    const b = base("nope");
    const pairs: [string, RequestInit][] = [
      ["/files", {}],
      ["/files/index", {}],
      ["/files/content?path=README.md", {}],
      ["/files/content", { method: "PUT", body: JSON.stringify({ path: "x", content: "y" }) }],
      ["/files", { method: "POST", body: JSON.stringify({ path: "x", kind: "file" }) }],
      ["/files/rename", { method: "POST", body: JSON.stringify({ from: "README.md", to: "x.md" }) }],
      ["/files/trash", { method: "POST", body: JSON.stringify({ path: "README.md" }) }],
    ];
    for (const [p, init] of pairs) {
      const res = await fetch(b + p, init);
      expect(res.status, p).toBe(404);
    }
  });

  test("a foreign Host is refused on every route/method pair", async () => {
    const b = base("web");
    const evil = { host: `evil.example:${port}` };
    const before = readFileSync(join(project, "README.md"), "utf8");
    const pairs: [string, RequestInit][] = [
      ["/files", { headers: evil }],
      ["/files/index", { headers: evil }],
      ["/files/content?path=README.md", { headers: evil }],
      ["/files/content", { method: "PUT", headers: evil, body: JSON.stringify({ path: "README.md", content: "HACKED" }) }],
      ["/files", { method: "POST", headers: evil, body: JSON.stringify({ path: "docs/evil.md", kind: "file" }) }],
      ["/files/rename", { method: "POST", headers: evil, body: JSON.stringify({ from: "README.md", to: "x.md" }) }],
      ["/files/trash", { method: "POST", headers: evil, body: JSON.stringify({ path: "README.md" }) }],
    ];
    for (const [p, init] of pairs) {
      const res = await fetch(b + p, init);
      expect(res.status, p).toBe(403);
    }
    expect(existsSync(join(project, "docs", "evil.md"))).toBe(false);
    expect(existsSync(join(project, "README.md"))).toBe(true);
    expect(existsSync(join(project, "x.md"))).toBe(false);
    expect(readFileSync(join(project, "README.md"), "utf8")).toBe(before);
  });

  test("a foreign Origin is refused on writes", async () => {
    const b = base("web");
    const origin = "http://evil.example";
    const before = readFileSync(join(project, "README.md"), "utf8");

    const save = await fetch(`${b}/files/content`, {
      method: "PUT",
      headers: { origin, "content-type": "application/json" },
      body: JSON.stringify({ path: "README.md", content: "HACKED" }),
    });
    expect(save.status).toBe(403);

    const create = await fetch(`${b}/files`, {
      method: "POST",
      headers: { origin, "content-type": "application/json" },
      body: JSON.stringify({ path: "docs/evil2.md", kind: "file" }),
    });
    expect(create.status).toBe(403);

    const rename = await fetch(`${b}/files/rename`, {
      method: "POST",
      headers: { origin, "content-type": "application/json" },
      body: JSON.stringify({ from: "README.md", to: "evil.md" }),
    });
    expect(rename.status).toBe(403);

    const trash = await fetch(`${b}/files/trash`, {
      method: "POST",
      headers: { origin, "content-type": "application/json" },
      body: JSON.stringify({ path: "README.md" }),
    });
    expect(trash.status).toBe(403);

    expect(readFileSync(join(project, "README.md"), "utf8")).toBe(before);
    expect(existsSync(join(project, "docs", "evil2.md"))).toBe(false);
    expect(existsSync(join(project, "evil.md"))).toBe(false);
  });

  test("refusals map to their HTTP status and reason", async () => {
    const b = base("web");

    const bad = await fetch(`${b}/files/content?path=../x`);
    expect(bad.status).toBe(400);
    expect(((await bad.json()) as { reason: string }).reason).toBe("bad_path");

    const bin = await fetch(`${b}/files/content?path=bin.dat`);
    expect(bin.status).toBe(415);
    expect(((await bin.json()) as { reason: string }).reason).toBe("binary");

    const git = await fetch(`${b}/files/content`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ path: ".git/config", content: "x" }),
    });
    expect(git.status).toBe(403);
    expect(((await git.json()) as { reason: string }).reason).toBe("git_dir");

    const del = await fetch(`${b}/files`, { method: "DELETE" });
    expect(del.status).toBe(405);

    const badJson = await fetch(`${b}/files/content`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: "not json",
    });
    expect(badJson.status).toBe(400);

    const trashGit = await fetch(`${b}/files/trash`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ path: ".git/config" }),
    });
    expect(trashGit.status).toBe(403);
    expect(((await trashGit.json()) as { reason: string }).reason).toBe("git_dir");

    const renameBad = await fetch(`${b}/files/rename`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ from: "README.md", to: "../x" }),
    });
    expect(renameBad.status).toBe(400);
    expect(((await renameBad.json()) as { reason: string }).reason).toBe("bad_path");

    const trashEmpty = await fetch(`${b}/files/trash`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ path: "" }),
    });
    expect(trashEmpty.status).toBe(400);
    expect(((await trashEmpty.json()) as { reason: string }).reason).toBe("bad_path");

    const trashGet = await fetch(`${b}/files/trash`);
    expect(trashGet.status).toBe(405);

    const renameBadJson = await fetch(`${b}/files/rename`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "not json",
    });
    expect(renameBadJson.status).toBe(400);

    const renameShape = await fetch(`${b}/files/rename`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ from: "README.md" }),
    });
    expect(renameShape.status).toBe(400);
  });

  test.skipIf(process.getuid?.() === 0)(
    "a failed Trash move over HTTP reports the error and leaves the file",
    async () => {
      const origin = `http://127.0.0.1:${port}`;
      mkdirSync(join(project, "locked"), { recursive: true });
      writeFileSync(join(project, "locked", "keep.txt"), "keep\n");
      chmodSync(join(project, "locked"), 0o555);
      try {
        const res = await fetch(`${base("web")}/files/trash`, {
          method: "POST",
          headers: { origin, "content-type": "application/json" },
          body: JSON.stringify({ path: "locked/keep.txt" }),
        });
        expect(res.status).toBe(500);
        const body = (await res.json()) as { error: string };
        expect(typeof body.error).toBe("string");
        expect(body.error.length).toBeGreaterThan(0);
        expect(body.error).toContain("Trash");
      } finally {
        chmodSync(join(project, "locked"), 0o755);
      }
      expect(existsSync(join(project, "locked", "keep.txt"))).toBe(true);
    },
  );

  // ── Route guard ─────────────────────────────────────────────────────────

  test("every file route goes through the local-only guard", () => {
    const text = readFileSync(join(import.meta.dir, "index.ts"), "utf8");
    const re = /"\/api\/projects\/:project\/files[^"]*":\s*(\w+)\(/g;
    const wrappers = [...text.matchAll(re)].map((m) => m[1]);
    expect(wrappers.length).toBeGreaterThanOrEqual(5);
    for (const w of wrappers) expect(w).toBe("withProjectRoot");
    const body = text.slice(text.indexOf("function withProjectRoot"));
    expect(body).toContain("safely(");
  });
});
