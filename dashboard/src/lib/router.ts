import { ref } from "vue";

// Hash routes:
//   #/                                   → home.
//   #/<project>/<run_id>[/<phase>]       → home with that Run's tab active in the
//                                          current project's strip (phase panel open).
// The phase segment may be a phase id or a phase name; an unknown one is
// replaced (not pushed) without the phase, so a stale link never sticks.
// `#/runs` (228), `#/cost` and `#/profiles` (229) are removed, not redirected.
// Every other hash opens home. The project segment exists because run_ids are
// only unique per project.
export interface Route {
  project: string | null;
  runId: string | null;
  phaseId: string | null;
}

/** Pure hash parser — testable without a DOM. */
export function parseHash(hash: string): Route {
  const parts = hash
    .replace(/^#\/?/, "")
    .split("/")
    .filter(Boolean)
    .map(decodeURIComponent);
  const home: Route = { project: null, runId: null, phaseId: null };
  if (parts.length < 2 || parts.length > 3) return home;
  // two or three segments are a Run link
  return {
    project: parts[0],
    runId: parts[1],
    phaseId: parts[2] ?? null,
  };
}

/** True when the link names exactly this Run. */
export function namesRun(
  route: Route,
  run: { project: string; runId: string },
): boolean {
  return route.project === run.project && route.runId === run.runId;
}

/** The phase id in `route` when it names `run`, else null. */
export function phaseFor(
  route: Route,
  run: { project: string; runId: string },
): string | null {
  return namesRun(route, run) ? route.phaseId : null;
}

/**
 * The phase one Run tab shows. A phase belongs to one Run (PLACEMENT row 81),
 * so the link only speaks for the Run it names: when it names this Run its
 * phase wins (a phase-less link closes the card), and when it names another Run
 * the tab keeps the phase it last selected, so switching through another Run
 * never discards this tab's selection or open card.
 */
export function tabPhase(
  route: Route,
  run: { project: string; runId: string },
  stored: string | null,
): string | null {
  return namesRun(route, run) ? route.phaseId : stored;
}

/** The phase a link segment names, and whether the link must be rewritten. */
export interface LinkPhase {
  phaseId: string | null;
  rewrite: boolean;
}

/**
 * Resolve a link's phase segment against the Run's phases. A null segment or a
 * Run with no `run` frame yet resolves to nothing and rewrites nothing. An id
 * matches directly; else the LAST phase in the given order whose name matches
 * wins, so `build_1` resolves and a retried name resolves to its latest
 * attempt. Anything else is unknown: no phase, and the link is rewritten.
 */
export function resolveLinkPhase(
  segment: string | null,
  phases: readonly { phase_id: string; name: string | null }[] | null,
): LinkPhase {
  if (segment === null || phases === null) return { phaseId: null, rewrite: false };
  const byId = phases.find((p) => p.phase_id === segment);
  if (byId) return { phaseId: byId.phase_id, rewrite: false };
  for (let i = phases.length - 1; i >= 0; i--) {
    if (phases[i].name === segment) {
      return { phaseId: phases[i].phase_id, rewrite: false };
    }
  }
  return { phaseId: null, rewrite: true };
}

function parse(): Route {
  return parseHash(
    typeof window === "undefined" ? "" : window.location.hash,
  );
}

const route = ref<Route>(parse());

if (typeof window !== "undefined") {
  window.addEventListener("hashchange", () => {
    route.value = parse();
  });
}

export function useRoute() {
  return route;
}

export function hrefFor(
  project?: string | null,
  runId?: string | null,
  phaseId?: string | null,
): string {
  let h = "#/";
  if (project && runId)
    h += `${encodeURIComponent(project)}/${encodeURIComponent(runId)}`;
  if (project && runId && phaseId) h += `/${encodeURIComponent(phaseId)}`;
  return h;
}

export function navigate(
  project?: string | null,
  runId?: string | null,
  phaseId?: string | null,
): void {
  window.location.hash = hrefFor(project, runId, phaseId);
}

/**
 * Rewrite the current history entry instead of pushing a new one, then refresh
 * `route` by hand: `replaceState` fires no `hashchange`. A no-op without a
 * window, so a pure caller stays safe.
 */
export function replaceRoute(
  project?: string | null,
  runId?: string | null,
  phaseId?: string | null,
): void {
  if (typeof window === "undefined") return;
  history.replaceState(
    null,
    "",
    location.pathname + location.search + hrefFor(project, runId, phaseId),
  );
  route.value = parse();
}
