import { describe, expect, test } from "bun:test";
import { clean } from "./fetch-logos";

describe("clean", () => {
  test("strips the title and namespaces lobehub ids", () => {
    const input =
      '<svg height="1em"><title>Meta</title><defs><linearGradient id="lobe-icons-meta-0-_R_0_"/></defs><path fill="url(#lobe-icons-meta-0-_R_0_)"/></svg>';
    const result = clean(input, "meta");
    expect(result).not.toContain("<title");
    expect(result).not.toContain("lobe-icons-");
    expect(result).toContain('id="lf-meta-0-_R_0_"');
    expect(result).toContain("url(#lf-meta-0-_R_0_)");
    expect(result.startsWith("<svg")).toBe(true);
    expect(result.endsWith("</svg>")).toBe(true);
  });

  test("trims surrounding whitespace", () => {
    expect(clean('\n<svg></svg>\n', "x")).toBe('<svg></svg>');
  });

  test("rejects anything that is not a whole svg", () => {
    expect(() => clean("<html></html>", "x")).toThrow("x");
    expect(() => clean("<svg>", "x")).toThrow();
  });
});