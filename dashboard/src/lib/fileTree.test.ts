import { describe, expect, test } from "bun:test";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { FileApiError } from "./api";
import {
  BAD_NAME,
  refusalText,
  submitCreate,
  submitRename,
  submitTrash,
  TAKEN,
  trashConfirmText,
  entryPathFor,
} from "./fileTree";
import type { CreateAsk, RenameAsk } from "./fileTree";
import type { FileCreated, FileEntry, FileKind, FileRenamed, FileTrashed } from "./types";

/** A recording stand-in for createFileEntry: captures calls, yields/rejects. */
function spyCreate(outcome: FileCreated | Error) {
  const calls: Array<[string, string, FileKind]> = [];
  const fn = async (project: string, path: string, kind: FileKind) => {
    calls.push([project, path, kind]);
    if (outcome instanceof Error) throw outcome;
    return outcome;
  };
  return { calls, fn };
}

function ask(over: Partial<CreateAsk> = {}): CreateAsk {
  return {
    project: "p",
    parent: "",
    raw: "new.ts",
    kind: "file",
    entries: [],
    ...over,
  };
}

/** A recording stand-in for renameFileEntry. */
function spyRename(outcome: FileRenamed | Error) {
  const calls: Array<[string, string, string]> = [];
  const fn = async (project: string, from: string, to: string) => {
    calls.push([project, from, to]);
    if (outcome instanceof Error) throw outcome;
    return outcome;
  };
  return { calls, fn };
}

/** A recording stand-in for trashFileEntry. */
function spyTrash(outcome: FileTrashed | Error) {
  const calls: Array<[string, string]> = [];
  const fn = async (project: string, path: string) => {
    calls.push([project, path]);
    if (outcome instanceof Error) throw outcome;
    return outcome;
  };
  return { calls, fn };
}

function renameAsk(over: Partial<RenameAsk> = {}): RenameAsk {
  return {
    project: "p",
    from: "src/app.ts",
    raw: "main.ts",
    entries: [],
    ...over,
  };
}

const ENTRIES: FileEntry[] = [
  { name: "app.ts", path: "src/app.ts", dir: false },
];

describe("entryPathFor", () => {
  test("entryPathFor joins the parent and cleans the name", () => {
    expect(entryPathFor("src", "app.ts")).toEqual({ path: "src/app.ts" });
    expect(entryPathFor("", " notes.md ")).toEqual({ path: "notes.md" });
    expect(entryPathFor("src", "/lib/")).toEqual({ path: "src/lib" });
    expect(entryPathFor("", "a/b/c.md")).toEqual({ path: "a/b/c.md" });
  });

  test("entryPathFor refuses empty, dot and .git names", () => {
    for (const raw of ["", "   ", ".", "..", "a/../b", "a//b", ".git", "x/.git/y"]) {
      expect(entryPathFor("src", raw)).toEqual({ error: BAD_NAME });
    }
  });
});

describe("refusalText", () => {
  test("other refusals map to inline text", () => {
    expect(refusalText(new FileApiError("forbidden", 403, "git_dir"))).toBe(
      BAD_NAME,
    );
    expect(refusalText(new Error("boom"))).toBe("boom");
  });
});

describe("submitCreate", () => {
  test("an invalid name is refused inline without calling the API", async () => {
    const { calls, fn } = spyCreate({ path: "x", kind: "file" });
    const res = await submitCreate(ask({ raw: "a/../b" }), fn);
    expect(res).toEqual({ error: BAD_NAME });
    expect(calls).toHaveLength(0);
  });

  test("a name already in the loaded folder is refused without calling the API", async () => {
    const { calls, fn } = spyCreate({ path: "src/app.ts", kind: "file" });
    const res = await submitCreate(
      ask({ parent: "src", raw: "app.ts", entries: ENTRIES }),
      fn,
    );
    expect(res).toEqual({ error: TAKEN });
    expect(calls).toHaveLength(0);
  });

  test("a 409 from the server reads Already exists", async () => {
    const { calls, fn } = spyCreate(
      new FileApiError("already exists", 409, "exists"),
    );
    const res = await submitCreate(ask({ parent: "src", raw: "new.ts" }), fn);
    expect(res).toEqual({ error: TAKEN });
    expect(calls).toHaveLength(1);
  });

  test("a valid name creates once and returns the created entry", async () => {
    const { calls, fn } = spyCreate({ path: "src/new.ts", kind: "file" });
    const res = await submitCreate(ask({ parent: "src", raw: "new.ts" }), fn);
    expect(res).toEqual({ created: { path: "src/new.ts", kind: "file" } });
    expect(calls).toEqual([["p", "src/new.ts", "file"]]);
  });

  test("client file writes live only in api.ts and fileTree.ts", () => {
    const dir = join(import.meta.dir, "..");
    const files = (readdirSync(dir, { recursive: true }) as string[])
      .map((f) => f.split("\\").join("/"))
      .filter((f) => (f.endsWith(".ts") || f.endsWith(".vue")) && !f.endsWith(".test.ts"));
    for (const symbol of ["createFileEntry", "renameFileEntry", "trashFileEntry"]) {
      const hits = files.filter((f) =>
        readFileSync(join(dir, f), "utf8").includes(symbol),
      );
      expect(hits.sort()).toEqual(["lib/api.ts", "lib/fileTree.ts"]);
    }
  });
});

describe("submitRename", () => {
  test("a bad or slash-bearing name is refused without calling the API", async () => {
    for (const raw of ["", "   ", "a/b", "..", ".git"]) {
      const { calls, fn } = spyRename({ from: "src/app.ts", to: "x" });
      const res = await submitRename(renameAsk({ raw }), fn);
      expect(res).toEqual({ error: BAD_NAME });
      expect(calls).toHaveLength(0);
    }
  });

  test("an unchanged name is a no-op with no call", async () => {
    const { calls, fn } = spyRename({ from: "a.md", to: "a.md" });
    const res = await submitRename(renameAsk({ from: "a.md", raw: "a.md" }), fn);
    expect(res).toEqual({ renamed: null });
    expect(calls).toHaveLength(0);
  });

  test("a sibling already in the loaded folder is refused without a call", async () => {
    const { calls, fn } = spyRename({ from: "src/app.ts", to: "src/notes.md" });
    const res = await submitRename(
      renameAsk({ raw: "notes.md", entries: [{ name: "notes.md", path: "src/notes.md", dir: false }] }),
      fn,
    );
    expect(res).toEqual({ error: TAKEN });
    expect(calls).toHaveLength(0);
  });

  test("a nested rename calls the API exactly once", async () => {
    const { calls, fn } = spyRename({ from: "src/app.ts", to: "src/main.ts" });
    const res = await submitRename(renameAsk(), fn);
    expect(res).toEqual({ renamed: { from: "src/app.ts", to: "src/main.ts" } });
    expect(calls).toEqual([["p", "src/app.ts", "src/main.ts"]]);
  });

  test("a root-level rename builds a root path", async () => {
    const { calls, fn } = spyRename({ from: "a.md", to: "b.md" });
    const res = await submitRename(renameAsk({ from: "a.md", raw: "b.md" }), fn);
    expect(res).toEqual({ renamed: { from: "a.md", to: "b.md" } });
    expect(calls).toEqual([["p", "a.md", "b.md"]]);
  });

  test("a 409 from the server reads Already exists", async () => {
    const { calls, fn } = spyRename(
      new FileApiError("already exists", 409, "exists"),
    );
    const res = await submitRename(renameAsk(), fn);
    expect(res).toEqual({ error: TAKEN });
    expect(calls).toHaveLength(1);
  });

  test("a plain failure surfaces its message", async () => {
    const { fn } = spyRename(new Error("could not rename x: EACCES"));
    const res = await submitRename(renameAsk(), fn);
    expect(res).toEqual({ error: "could not rename x: EACCES" });
  });
});

describe("submitTrash", () => {
  test("a success calls the API exactly once", async () => {
    const { calls, fn } = spyTrash({ path: "notes.md" });
    const res = await submitTrash("p", "notes.md", fn);
    expect(res).toEqual({ trashed: { path: "notes.md" } });
    expect(calls).toEqual([["p", "notes.md"]]);
  });

  test("a failed move surfaces the server's message", async () => {
    const msg = "could not move notes.md to the Trash: nope";
    const { fn } = spyTrash(new FileApiError(msg, 500, null));
    const res = await submitTrash("p", "notes.md", fn);
    expect(res).toEqual({ error: msg });
  });
});

describe("trashConfirmText", () => {
  test("a clean entry points at the Finder Trash", () => {
    const { title, detail } = trashConfirmText("notes.md", []);
    expect(title).toContain("notes.md");
    expect(title).toContain("Trash");
    expect(detail).toBe("You can restore it from the Finder Trash.");
  });

  test("a dirty entry warns and lists every name", () => {
    const { detail } = trashConfirmText("src", ["a.ts", "b.ts"]);
    expect(detail).toContain("a.ts");
    expect(detail).toContain("b.ts");
  });
});

describe("tree rename and trash reach the editor", () => {
  const component = (name: string) =>
    readFileSync(join(import.meta.dir, "../components", name), "utf8");

  test("FileTree drives rename and trash through the pure rules", () => {
    const src = component("FileTree.vue");
    expect(src).toContain("@contextmenu");
    expect(src).toContain("submitRename(");
    expect(src).toContain("submitTrash(");
    expect(src).toContain("tabindex");
    expect(src).not.toContain("renameFileEntry");
    expect(src).not.toContain("trashFileEntry");
    expect(src).not.toContain("useKeys");
  });

  test("Workspace wires the tree to the editor", () => {
    const src = component("Workspace.vue");
    expect(src).toContain("@renamed=");
    expect(src).toContain("@trashed=");
    expect(src).toContain(":dirty-under=");
  });

  test("Editor exposes the tab-follow API", () => {
    const src = component("Editor.vue");
    const expose = src.slice(src.indexOf("defineExpose"));
    expect(expose).toContain("renamePath");
    expect(expose).toContain("closePath");
    expect(expose).toContain("dirtyUnder");
  });
});
