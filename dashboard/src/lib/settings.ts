/**
 * The Settings modal's pure helpers: the section table, the section-key
 * bindings, the shell's one Escape binding and the focus-restore helper.
 * Type-only import from `./keymap`, so bun never has to load vue for this file.
 */
import type { Binding, KeyEventLike } from "./keymap";

export type SettingsSection = "cost" | "profiles" | "appearance";

/** One source for the nav's `<kbd>` and the section bindings. */
export const SETTINGS_SECTIONS: {
  id: SettingsSection;
  label: string;
  key: string;
}[] = [
  { id: "cost", label: "Cost", key: "1" },
  { id: "profiles", label: "Profiles", key: "2" },
  { id: "appearance", label: "Appearance", key: "3" },
];

/**
 * The section keys. Plain digits, default `whileTyping: false`, so they are
 * skipped in text fields; default `prevent: true`, so they don't leak into the
 * page. They do not collide with the pane combos (⌥1–4 bind by code).
 */
export function settingsBindings(
  select: (s: SettingsSection) => void,
): Binding[] {
  return SETTINGS_SECTIONS.map((s) => ({
    id: `settings: ${s.id}`,
    key: s.key,
    run: () => select(s.id),
  }));
}

/**
 * The shell's only Escape. New overlays (228's Runs sheet) extend
 * `Workspace.closeTop`; never register a second Escape. `whileTyping: true` so
 * Escape still reaches a focused input's own `@keydown.esc`; `prevent: false`
 * so the event goes on untouched when nothing is open.
 */
export function shellEscape(closeTop: (e: KeyEventLike) => boolean): Binding {
  return {
    id: "shell: escape",
    key: "Escape",
    whileTyping: true,
    prevent: false,
    run: (e) => closeTop(e),
  };
}

/**
 * Capture the currently focused element and return a restore function. The
 * restore focuses it only while it still has a `focus` method and is still
 * connected; otherwise it does nothing.
 */
export function rememberFocus(
  doc: { activeElement: unknown } = document,
): () => void {
  const el = doc.activeElement as {
    focus?: () => void;
    isConnected?: boolean;
  } | null;
  return () => {
    if (el && typeof el.focus === "function" && el.isConnected !== false) {
      el.focus();
    }
  };
}
