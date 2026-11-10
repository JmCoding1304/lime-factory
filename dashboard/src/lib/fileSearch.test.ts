import { describe, expect, test } from "bun:test";
import {
  ancestorsOf,
  buildSearchIndex,
  searchFiles,
  SEARCH_LIMIT,
  type SearchIndex,
  type SearchResult,
} from "./fileSearch";

function syntheticProject(): { index: SearchIndex; queries: string[] } {
  const EXT = ["ts", "vue", "md", "py", "json", "css"];
  const paths: string[] = [];
  for (let i = 0; i < 20000; i++) {
    paths.push(
      `pkg${i % 37}/mod${i % 211}/sub${i % 7}/file${i}.${EXT[i % 6]}`,
    );
  }

  const letters = "file1999";
  const queries: string[] = [];
  for (let k = 1; k <= letters.length; k++) queries.push(letters.slice(0, k));
  queries.push("mod12/", "zzz", "README");

  return { index: buildSearchIndex(paths), queries };
}

type Search = (index: SearchIndex, query: string) => SearchResult;

const MIN_KEYSTROKES_PER_REBUILD = 2.5;
const ROUNDS = 7;

/**
 * Load slows both measurements alike, so the ratio carries the verdict, not a
 * millisecond budget. The fastest round drops rounds another process
 * interrupted. A search that rebuilds the index can never reach the threshold
 * because each keystroke contains a whole rebuild.
 */
function keystrokesPerRebuild(
  search: Search,
  index: SearchIndex,
  queries: string[],
  now: () => number = () => performance.now(),
): number {
  for (const q of queries) search(index, q); // warm-up
  buildSearchIndex(index.paths);

  let rebuild = Infinity;
  let keystroke = Infinity;
  for (let round = 0; round < ROUNDS; round++) {
    let t0 = now();
    buildSearchIndex(index.paths);
    rebuild = Math.min(rebuild, now() - t0);

    t0 = now();
    for (const q of queries) search(index, q);
    keystroke = Math.min(keystroke, (now() - t0) / queries.length);
  }
  return rebuild / keystroke;
}

// Deliberately slow: same results as searchFiles, but re-sorts the index on
// every keystroke.
const rebuildEveryCall: Search = (index, query) =>
  searchFiles(buildSearchIndex(index.paths), query);

describe("searchFiles", () => {
  test("an empty or blank query matches nothing", () => {
    const index = buildSearchIndex(["a/x.ts", "b/y.md"]);
    for (const q of ["", "   "]) {
      const res = searchFiles(index, q);
      expect(res.hits).toHaveLength(0);
      expect(res.total).toBe(0);
    }
  });

  test("matching is case-insensitive", () => {
    const index = buildSearchIndex(["docs/readme.md"]);
    const res = searchFiles(index, "READ");
    expect(res.total).toBe(1);
    expect(res.hits[0].path).toBe("docs/readme.md");
  });

  test("it filters file names, not directory names", () => {
    const index = buildSearchIndex(["src/app.ts", "lib/src.ts"]);
    const res = searchFiles(index, "src");
    expect(res.hits.map((h) => h.path)).toEqual(["lib/src.ts"]);
  });

  test("exact name, then prefix, then substring, shorter path first", () => {
    const index = buildSearchIndex([
      "a/domain.ts",
      "a/main.tsx",
      "deep/er/main.ts",
      "z/main.ts",
    ]);
    const res = searchFiles(index, "main.ts");
    expect(res.hits.map((h) => h.path)).toEqual([
      "z/main.ts",
      "deep/er/main.ts",
      "a/main.tsx",
      "a/domain.ts",
    ]);
  });

  test("a query with a slash matches the whole path", () => {
    const index = buildSearchIndex(["dashboard/src/lib/fileSearch.ts"]);
    const res = searchFiles(index, "lib/fi");
    expect(res.total).toBe(1);
    expect(res.hits[0].path).toBe("dashboard/src/lib/fileSearch.ts");
    expect(res.hits[0].at).toBe(-1);
  });

  test("at and len mark the match inside the name", () => {
    const index = buildSearchIndex(["src/lib/fileSearch.ts"]);
    const res = searchFiles(index, "search");
    const hit = res.hits[0];
    expect(hit.at).toBe(4);
    expect(hit.len).toBe(6);
    expect(hit.dir).toBe("src/lib");
    expect(hit.name).toBe("fileSearch.ts");
  });

  test("hits are capped at the limit and total counts every match", () => {
    const paths = Array.from({ length: 120 }, (_, i) => `dir/file${i}.txt`);
    const index = buildSearchIndex(paths);
    const res = searchFiles(index, "f");
    expect(res.hits).toHaveLength(SEARCH_LIMIT);
    expect(res.total).toBe(120);
  });

  test("duplicate paths are indexed once", () => {
    const index = buildSearchIndex(["a/x.ts", "a/x.ts", "b/y.ts"]);
    expect(index.paths).toHaveLength(2);
  });

  // The 30_000 timeout is a hang guard only: a busy machine stretches these
  // tests but never decides them.
  test(
    "filtering a synthetic 20,000-path index costs well under one index rebuild per keystroke",
    () => {
      const { index, queries } = syntheticProject();
      expect(
        keystrokesPerRebuild(searchFiles, index, queries),
      ).toBeGreaterThanOrEqual(MIN_KEYSTROKES_PER_REBUILD);

      const f = searchFiles(index, "f");
      expect(f.total).toBe(20000);
      expect(f.hits).toHaveLength(SEARCH_LIMIT);
    },
    30_000,
  );

  test(
    "the speed check fails a search that rebuilds the index on every keystroke",
    () => {
      const { index, queries } = syntheticProject();
      expect(
        keystrokesPerRebuild(rebuildEveryCall, index, queries),
      ).toBeLessThan(MIN_KEYSTROKES_PER_REBUILD);
      expect(rebuildEveryCall(index, "file1")).toEqual(
        searchFiles(index, "file1"),
      );
    },
    30_000,
  );

  test(
    "a keystroke stalled 60 ms by a busy machine does not change the speed verdict",
    () => {
      const { index, queries } = syntheticProject();
      let calls = 0;
      const stalled = () => performance.now() + (++calls >= 4 ? 60 : 0);
      expect(
        keystrokesPerRebuild(searchFiles, index, queries, stalled),
      ).toBeGreaterThanOrEqual(MIN_KEYSTROKES_PER_REBUILD);
    },
    30_000,
  );
});

describe("ancestorsOf", () => {
  test("ancestorsOf lists outer dirs first", () => {
    expect(ancestorsOf("a/b/c.md")).toEqual(["a", "a/b"]);
    expect(ancestorsOf("x.md")).toEqual([]);
  });
});
