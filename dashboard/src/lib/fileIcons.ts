// Language-logo file icons, ported from proto-workspace/b/icons.js. No network:
// the stroke glyphs are inlined here and the Simple Icons marks are vendored in
// ./fileIconPaths.
import { FILE_ICON_PATHS } from "./fileIconPaths";

const FILE = '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>';
const FOLDER = '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>';

export const GLYPHS: Record<string, string> = {
  file: FILE,
  fileText: FILE + '<path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
  fileCode: FILE + '<path d="m10 13-2 2 2 2"/><path d="m14 17 2-2-2-2"/>',
  fileData: FILE + '<path d="M10 12a1 1 0 0 0-1 1v1a1 1 0 0 1-1 1 1 1 0 0 1 1 1v1a1 1 0 0 0 1 1"/><path d="M14 18a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1 1 1 0 0 1-1-1v-1a1 1 0 0 0-1-1"/>',
  fileImage: FILE + '<circle cx="10" cy="12" r="2"/><path d="m20 17-1.296-1.296a2.41 2.41 0 0 0-3.408 0L9 22"/>',
  folder: FOLDER,
  folderOpen: '<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>',
  chevronRight: '<path d="m9 18 6-6-6-6"/>',
  plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  pencil: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
  filePlus: FILE + '<path d="M9 15h6"/><path d="M12 18v-6"/>',
  folderPlus: FOLDER + '<path d="M12 10v6"/><path d="M9 13h6"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  alert: '<circle cx="12" cy="12" r="9"/><path d="M12 8v4.5"/><path d="M12 16h.01"/>',
};

export function glyph(name: string, size = 16, sw = 1.75): string {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${GLYPHS[name] ?? ""}</svg>`;
}

// ---- file icons: real language logos from Simple Icons (CC0), tinted with our own colours.
// Bundled from jsDelivr (pinned), inlined with fill=currentColor so CSS colour applies.
const BRAND_BY_EXT: Record<string, [string, string]> = {
  py: ["python", "ft-py"], md: ["markdown", "ft-md"], mdc: ["markdown", "ft-md"], markdown: ["markdown", "ft-md"],
  ts: ["typescript", "ft-ts"], tsx: ["typescript", "ft-ts"], js: ["javascript", "ft-js"], mjs: ["javascript", "ft-js"],
  cjs: ["javascript", "ft-js"], jsx: ["react", "ft-ts"], vue: ["vuedotjs", "ft-vue"], yaml: ["yaml", "ft-yaml"],
  yml: ["yaml", "ft-yaml"], json: ["json", "ft-data"], jsonl: ["json", "ft-data"], toml: ["toml", "ft-toml"],
  html: ["html5", "ft-html"], css: ["css", "ft-css"], sh: ["gnubash", "ft-sh"], bash: ["gnubash", "ft-sh"],
  zsh: ["gnubash", "ft-sh"], rs: ["rust", "ft-html"], go: ["go", "ft-css"], sql: ["sqlite", "ft-sql"],
  db: ["sqlite", "ft-sql"], svg: ["svg", "ft-img"], excalidraw: ["excalidraw", "ft-img"],
};
const BRAND_BY_NAME: Record<string, [string, string]> = {
  dockerfile: ["docker", "ft-docker"], ".dockerignore": ["docker", "ft-docker"], ".gitignore": ["git", "ft-git"],
  ".gitattributes": ["git", "ft-git"], ".gitkeep": ["git", "ft-git"], ".gitmodules": ["git", "ft-git"],
  "package.json": ["npm", "ft-npm"], "bun.lock": ["bun", "ft-plain"], "bun.lockb": ["bun", "ft-plain"],
  "uv.lock": ["uv", "ft-yaml"], "claude.md": ["claude", "ft-claude"], ".env": ["dotenv", "ft-js"],
  "docker-compose.yml": ["docker", "ft-docker"], "docker-compose.yaml": ["docker", "ft-docker"],
};
const GENERIC_BY_EXT: Record<string, [string, string]> = {
  txt: ["fileText", "ft-plain"], log: ["fileText", "ft-plain"], csv: ["fileData", "ft-plain"], tsv: ["fileData", "ft-plain"],
  lock: ["fileData", "ft-plain"], png: ["fileImage", "ft-img"], jpg: ["fileImage", "ft-img"], jpeg: ["fileImage", "ft-img"],
  gif: ["fileImage", "ft-img"], webp: ["fileImage", "ft-img"], pdf: ["fileText", "ft-img"], xlsx: ["fileData", "ft-yaml"],
};

export const BRAND_SLUGS: string[] = [
  "python", "markdown", "typescript", "javascript", "react", "vuedotjs", "yaml", "json", "toml", "html5", "css",
  "gnubash", "rust", "go", "sqlite", "svg", "excalidraw", "docker", "git", "npm", "bun", "uv", "claude", "dotenv",
  "vite", "githubactions",
];

export function extOf(path: string): string {
  const b = path.split("/").pop() ?? "";
  const i = b.lastIndexOf(".");
  return i > 0 ? b.slice(i + 1).toLowerCase() : "";
}

function brandOf(path: string): [string, string] | null {
  const base = path.split("/").pop()!.toLowerCase();
  if (BRAND_BY_NAME[base]) return BRAND_BY_NAME[base];
  if (base.startsWith(".env")) return BRAND_BY_NAME[".env"];
  if (base.startsWith("vite.config.")) return ["vite", "ft-vue"];
  if (/(^|\/)\.github\/workflows\/[^/]+\.ya?ml$/.test(path)) return ["githubactions", "ft-css"];
  return BRAND_BY_EXT[extOf(path)] || null;
}

export interface FileKindInfo {
  slug: string | null;
  glyph: string;
  tint: string;
}

export function fileKind(path: string): FileKindInfo {
  const b = brandOf(path);
  if (b) {
    const [slug, tint] = b;
    const glyphName = /^(md|mdc|markdown)$/.test(extOf(path)) ? "fileText" : "fileCode";
    return { slug, tint, glyph: glyphName };
  }
  const g = GENERIC_BY_EXT[extOf(path)] || ["file", "ft-plain"];
  return { slug: null, glyph: g[0], tint: g[1] };
}

export function fileIconSvg(path: string, size = 15): string {
  const kind = fileKind(path);
  if (kind.slug) {
    const d = FILE_ICON_PATHS[kind.slug];
    if (d) {
      return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true" class="brand-ic"><path fill="currentColor" transform="translate(2.4 2.4) scale(.8)" d="${d}"/></svg>`;
    }
  }
  return glyph(kind.glyph, size);
}
