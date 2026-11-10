/**
 * The shell's one keymap.
 *
 * Pages declare their keys here (via `useKeys`). A duplicate combo throws at
 * registration time, so two features can never silently shadow each other.
 * Nobody else adds a `keydown` listener — a test enforces it — so the capture
 * phase in `installKeymap` is the app's single entry point for key events.
 */
import { onMounted, onUnmounted } from "vue";

export interface KeyEventLike {
  key: string;
  code: string;
  altKey: boolean;
  metaKey: boolean;
  ctrlKey: boolean;
  target: unknown;
  preventDefault(): void;
  stopPropagation(): void;
}

export interface Binding {
  id: string; // owner + action, named in the duplicate error ("shell: left pane")
  code?: string; // KeyboardEvent.code — use for ⌥ combos (⌥ rewrites e.key on macOS)
  key?: string; // KeyboardEvent.key — single keys ("p", "/", "Escape")
  alt?: boolean; // ⌥ held, default false
  mod?: boolean; // ⌘ or Ctrl held, default false
  whileTyping?: boolean; // default false: skipped while focus is in input/textarea/select/contenteditable
  prevent?: boolean; // default true: preventDefault + stopPropagation once handled
  run: (e: KeyEventLike) => boolean | void; // false = not handled, event goes on untouched
}

/** Exactly one of `code`/`key` is required; shift is never part of a combo. */
export function comboOf(b: Binding): string {
  const hasCode = b.code !== undefined;
  const hasKey = b.key !== undefined;
  if (hasCode === hasKey) {
    throw new Error(`binding "${b.id}" needs exactly one of code or key`);
  }
  return `${b.alt ? "alt+" : ""}${b.mod ? "mod+" : ""}${
    hasCode ? `code:${b.code}` : `key:${b.key}`
  }`;
}

/** True when focus is in a text field (CodeMirror's `.cm-content` is contenteditable). */
export function isTyping(target: unknown): boolean {
  const t = target as { tagName?: string; isContentEditable?: boolean } | null;
  if (!t) return false;
  return (
    t.tagName === "INPUT" ||
    t.tagName === "TEXTAREA" ||
    t.tagName === "SELECT" ||
    t.isContentEditable === true
  );
}

export interface Keymap {
  register(bindings: Binding[]): () => void;
  dispatch(e: KeyEventLike): boolean;
}

export function createKeymap(): Keymap {
  const byCombo = new Map<string, Binding>();
  return {
    register(bindings) {
      const combos = bindings.map(comboOf);
      // validate the whole batch first: a clash adds none of it
      const seen = new Map<string, string>();
      for (let i = 0; i < combos.length; i++) {
        const combo = combos[i];
        const old = byCombo.get(combo)?.id ?? seen.get(combo);
        if (old !== undefined) {
          throw new Error(
            `duplicate key binding ${combo}: "${old}" and "${bindings[i].id}"`,
          );
        }
        seen.set(combo, bindings[i].id);
      }
      combos.forEach((combo, i) => byCombo.set(combo, bindings[i]));
      return () => {
        combos.forEach((combo, i) => {
          if (byCombo.get(combo) === bindings[i]) byCombo.delete(combo);
        });
      };
    },
    dispatch(e) {
      const alt = e.altKey;
      const mod = e.metaKey || e.ctrlKey;
      const prefix = `${alt ? "alt+" : ""}${mod ? "mod+" : ""}`;
      const b =
        byCombo.get(`${prefix}code:${e.code}`) ??
        byCombo.get(`${prefix}key:${e.key}`);
      if (!b) return false;
      if (!b.whileTyping && isTyping(e.target)) return false;
      if (b.run(e) === false) return false;
      if (b.prevent !== false) {
        e.preventDefault();
        e.stopPropagation();
      }
      return true;
    },
  };
}

export const shellKeymap: Keymap = createKeymap();

const installed = new WeakSet<object>();

/** The app's only keydown listener; capture phase so the editor never sees a pane combo first. */
export function installKeymap(
  target: Pick<Window, "addEventListener"> = window,
  map: Keymap = shellKeymap,
): void {
  if (installed.has(target as object)) return;
  installed.add(target as object);
  target.addEventListener(
    "keydown",
    (e) => map.dispatch(e as KeyEventLike),
    true,
  );
}

/** Register page bindings for this component's lifetime. */
export function useKeys(bindings: Binding[]): void {
  let off: (() => void) | null = null;
  onMounted(() => {
    off = shellKeymap.register(bindings);
  });
  onUnmounted(() => {
    off?.();
    off = null;
  });
}
