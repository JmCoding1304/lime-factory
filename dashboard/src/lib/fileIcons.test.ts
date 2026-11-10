import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { BRAND_SLUGS, fileIconSvg, fileKind, GLYPHS } from "./fileIcons";
import { FILE_ICON_PATHS } from "./fileIconPaths";

describe("fileKind", () => {
  test("language files map to their logo and tint", () => {
    expect(fileKind("src/app.ts")).toMatchObject({ slug: "typescript", tint: "ft-ts" });
    expect(fileKind("x.py")).toMatchObject({ slug: "python", tint: "ft-py" });
    expect(fileKind("App.vue")).toMatchObject({ slug: "vuedotjs", tint: "ft-vue" });
    expect(fileKind("README.md")).toMatchObject({ slug: "markdown", tint: "ft-md" });
  });

  test("special names win over extensions", () => {
    expect(fileKind("dashboard/package.json").slug).toBe("npm");
    expect(fileKind("Dockerfile").slug).toBe("docker");
    expect(fileKind(".env.local").slug).toBe("dotenv");
    expect(fileKind("vite.config.ts").slug).toBe("vite");
    expect(fileKind(".github/workflows/ci.yml").slug).toBe("githubactions");
    expect(fileKind(".gitignore").slug).toBe("git");
  });

  test("files with no logo get a generic glyph", () => {
    expect(fileKind("notes.txt")).toMatchObject({ slug: null, glyph: "fileText" });
    expect(fileKind("shot.png")).toMatchObject({ slug: null, glyph: "fileImage" });
    expect(fileKind("Makefile")).toEqual({ slug: null, glyph: "file", tint: "ft-plain" });
    expect(fileKind("a.zzz")).toEqual({ slug: null, glyph: "file", tint: "ft-plain" });
  });

  test("every brand slug is bundled", () => {
    for (const slug of BRAND_SLUGS) {
      const d = FILE_ICON_PATHS[slug];
      expect(d).toBeTruthy();
      // Simple Icons paths start with an absolute (M) or relative (m) moveto.
      expect(/^[Mm]/.test(d)).toBe(true);
    }
    expect(BRAND_SLUGS.length).toBe(26);
  });

  test("fileIconSvg draws the logo or the glyph", () => {
    const py = fileIconSvg("a.py");
    expect(py).toContain('fill="currentColor"');
    expect(py).toContain(FILE_ICON_PATHS.python);
    const z = fileIconSvg("a.zzz");
    expect(z).toContain(GLYPHS.file);
    expect(z).not.toContain('class="brand-ic"');
  });
});

describe("bundling", () => {
  test("icons are bundled, never fetched", () => {
    for (const file of ["fileIcons.ts", "fileIconPaths.ts"]) {
      const src = readFileSync(join(import.meta.dir, file), "utf8");
      expect(src).not.toContain("fetch(");
      expect(src).not.toContain("http");
    }
  });
});
