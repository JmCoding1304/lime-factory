import { ref } from "vue";

// Three-state theme (spec §11): dark / light / system, persisted. "system"
// clears the data-theme attribute so the CSS prefers-color-scheme block
// decides. index.html applies the stored value before first paint; this
// module owns it from then on.

export type Theme = "system" | "light" | "dark";

export const THEME_KEY = "lf-theme";
const KEY = THEME_KEY;
const ORDER: Theme[] = ["system", "light", "dark"];

function stored(): Theme {
  try {
    const t = localStorage.getItem(KEY);
    return t === "light" || t === "dark" ? t : "system";
  } catch {
    return "system";
  }
}

export const theme = ref<Theme>(stored());

function apply(t: Theme): void {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
}

export function setTheme(t: Theme): void {
  theme.value = t;
  apply(t);
  try {
    localStorage.setItem(KEY, t);
  } catch {
    /* private windows still get the in-page toggle, just not persistence */
  }
}

export function cycleTheme(): void {
  setTheme(ORDER[(ORDER.indexOf(theme.value) + 1) % ORDER.length]!);
}
