/**
 * Model → provider logo, by contains-check on the model name.
 *
 * Logos come from src/lib/logos.ts (generated from lobehub by `bun run logos`),
 * providers/needles from src/lib/providers.ts. First matching needle wins;
 * unknown models fall back to the lime asterisk.
 */

export const LIME_ICON =
  '<svg viewBox="0 0 500 500" xmlns="http://www.w3.org/2000/svg"><path d="M249.969 0C111.927 0 0 111.878 0 250C0 388.122 111.927 500 249.969 500C388.01 500 500 388.122 500 250C500 111.878 388.073 0 249.969 0ZM424.922 360.748C418.205 372.238 406.277 378.453 394.099 378.453C386.503 378.453 378.845 376.005 372.316 370.982L269.366 291.436L284.557 421.459C287.068 442.68 270.37 461.326 249.027 461.2C229.19 461.138 213.497 444.94 213.497 425.603C213.497 423.97 213.622 422.401 213.81 420.768L231.262 291.813L126.303 369.977C109.165 382.722 84.683 377.637 74.0741 359.053C70.8726 353.466 69.366 347.376 69.366 341.474C69.366 327.536 77.5895 314.164 91.4626 308.388L211.802 259.041L91.651 207.245C72.0653 198.769 64.2812 175.038 75.0157 156.517C81.6698 145.028 93.6598 138.812 105.775 138.812C113.371 138.812 121.03 141.261 127.558 146.283L230.508 225.829L215.317 95.9317C212.806 74.7112 229.504 56.0648 250.91 56.1276C270.747 56.2531 286.378 72.451 286.378 91.7253C286.378 93.3576 286.252 94.9272 286.064 96.5595L268.675 225.515L373.572 147.351C390.709 134.606 415.191 139.691 425.8 158.275C429.002 163.925 430.508 169.952 430.508 175.979C430.508 189.854 422.348 203.227 408.412 208.94L288.01 258.35L408.223 310.146C427.809 318.621 435.656 342.353 424.922 360.811V360.748Z" fill="#C1F610"/></svg>';

import { LOGOS } from "./logos";
import { PROVIDERS } from "./providers";

/** Command icons for code-phase runners, classified by first token. */
const CMD_ICONS: Record<string, string> = {
  python:
    '<svg height="1em" style="flex:none;line-height:1" viewBox="0 0 24 24" width="1em" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M14.25.18l.9.2.73.26.59.3.45.32.34.34.25.34.16.33.1.3.04.26.02.2-.01.13V8.5l-.05.63-.13.55-.21.46-.26.38-.3.31-.33.25-.35.19-.35.14-.33.1-.3.07-.26.04-.21.02H8.77l-.69.05-.59.14-.5.22-.41.27-.33.32-.27.35-.2.36-.15.37-.1.35-.07.32-.04.27-.02.21v3.06H3.17l-.21-.03-.28-.07-.32-.12-.35-.18-.36-.26-.36-.36-.35-.46-.32-.59-.28-.73-.21-.88-.14-1.05-.05-1.23.06-1.22.16-1.04.24-.87.32-.71.36-.57.4-.44.42-.33.42-.24.4-.16.36-.1.32-.05.24-.01h.16l.06.01h8.16v-.83H6.18l-.01-2.75-.02-.37.05-.34.11-.31.17-.28.25-.26.31-.23.38-.2.44-.18.51-.15.58-.12.64-.1.71-.06.77-.04.84-.02 1.27.05zm-6.3 1.98l-.23.33-.08.41.08.41.23.34.33.22.41.09.41-.09.33-.22.23-.34.08-.41-.08-.41-.23-.33-.33-.22-.41-.09-.41.09zm13.09 3.95l.28.06.32.12.35.18.36.27.36.35.35.47.32.59.28.73.21.88.14 1.04.05 1.23-.06 1.23-.16 1.04-.24.86-.32.71-.36.57-.4.45-.42.33-.42.24-.4.16-.36.09-.32.05-.24.02-.16-.01h-8.22v.82h5.84l.01 2.76.02.36-.05.34-.11.31-.17.29-.25.25-.31.24-.38.2-.44.17-.51.15-.58.13-.64.09-.71.07-.77.04-.84.01-1.27-.04-1.07-.14-.9-.2-.73-.25-.59-.3-.45-.33-.34-.34-.25-.34-.16-.33-.1-.3-.04-.25-.02-.2.01-.13v-5.34l.05-.64.13-.54.21-.46.26-.38.3-.32.33-.24.35-.2.35-.14.33-.1.3-.06.26-.04.21-.02.13-.01h5.84l.69-.05.59-.14.5-.21.41-.28.33-.32.27-.35.2-.36.15-.36.1-.35.07-.32.04-.28.02-.21V6.07h2.09l.14.01z"/></svg>',
  bash:
    '<svg height="1em" style="flex:none;line-height:1" viewBox="0 0 24 24" width="1em" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" xmlns="http://www.w3.org/2000/svg"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
};

const PYTHON_TOKENS = new Set(["uv", "python", "python3", "pytest", "pip", "pip3"]);

/**
 * Command icon for a phase's runner command, classified by its first token.
 * Falls back to the terminal (bash) icon for command-less code phases
 * (commit/changes) and unknown runners. New runners are one needle line:
 * add the token to PYTHON_TOKENS or extend this classifier.
 */
export function commandIcon(command: string | null | undefined): string {
  if (command) {
    const first = command.trim().split(/\s+/)[0].toLowerCase();
    if (PYTHON_TOKENS.has(first)) return CMD_ICONS.python;
  }
  return CMD_ICONS.bash;
}

/** Provider logo SVG for a model name; the lime asterisk when unknown. */
export function modelLogo(model: string | null | undefined): string {
  if (model) {
    const m = model.toLowerCase();
    for (const p of PROVIDERS) {
      if (p.needles.some((n) => m.includes(n))) return LOGOS[p.key] ?? LIME_ICON;
    }
  }
  return LIME_ICON;
}

const PALETTE = ["#57a639","#d5438a","#2ea3a3","#b07d00","#7a4de0","#c1451d"];

const FAMILY_COLORS: Record<string, string> = {
  fable: "#d97757",
  sonnet: "#9c6bff",
  opus: "#b04a2b",
  haiku: "#e8a58a",
};

const PROVIDER_COLORS: Record<string, string> = {
  claude: "#d97757",
  deepseek: "#4d6bfe",
  qwen: "#6f69f7",
  gemini: "#3186ff",
  grok: "#e8940a",
  kimi: "#1783ff",
};

function hashStr(s: string): number {
  let hash = 0;
  for (let i = 0; i < s.length; i++) {
    hash = (31 * hash + s.charCodeAt(i)) >>> 0;
  }
  return hash;
}

/** one stable colour per model; siblings of a family share it, the icon + full name tell them apart */
export function modelColor(model: string | null | undefined): string {
  if (!model) return PALETTE[0];
  const m = model.toLowerCase();

  // Family needles first
  for (const [family, color] of Object.entries(FAMILY_COLORS)) {
    if (m.includes(family)) return color;
  }

  // Provider via PROVIDERS table
  for (const p of PROVIDERS) {
    if (p.key in PROVIDER_COLORS && p.needles.some((n) => m.includes(n))) {
      return PROVIDER_COLORS[p.key];
    }
  }

  // Hashed palette fallback
  return PALETTE[hashStr(m) % PALETTE.length];
}

/** Keep provider-qualified IDs compact while preserving the full ID in titles. */
export function modelName(model: string | null | undefined): string {
  if (!model) return "";
  return model.split("/").findLast(Boolean) ?? model;
}