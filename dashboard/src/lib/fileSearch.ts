// Live file-name search over a project's index — pure, no Vue, no DOM.
// One linear pass per keystroke with indexOf only, so 20k paths stay cheap.

export const SEARCH_LIMIT = 50;

/** Parallel arrays over the deduped, path-length-sorted index. */
export interface SearchIndex {
  paths: string[];
  names: string[];
  lowerNames: string[];
  lowerPaths: string[];
}

export interface SearchHit {
  path: string;
  name: string;
  dir: string;
  /** Match start inside `name`, or -1 for a whole-path match. */
  at: number;
  /** Query length. */
  len: number;
}

export interface SearchResult {
  hits: SearchHit[];
  total: number;
}

function basename(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? path : path.slice(i + 1);
}

function dirname(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? "" : path.slice(0, i);
}

/**
 * Dedupe and sort once. The sort is length-first then plain lexicographic
 * (never localeCompare: far too slow across 20k paths).
 */
export function buildSearchIndex(paths: readonly string[]): SearchIndex {
  const sorted = Array.from(new Set(paths)).sort(
    (a, b) => a.length - b.length || (a < b ? -1 : a > b ? 1 : 0),
  );
  const names: string[] = new Array(sorted.length);
  const lowerNames: string[] = new Array(sorted.length);
  const lowerPaths: string[] = new Array(sorted.length);
  for (let i = 0; i < sorted.length; i++) {
    const name = basename(sorted[i]);
    names[i] = name;
    lowerNames[i] = name.toLowerCase();
    lowerPaths[i] = sorted[i].toLowerCase();
  }
  return { paths: sorted, names, lowerNames, lowerPaths };
}

/** One pass; buckets exact name, then prefix, then substring, then path. */
export function searchFiles(
  index: SearchIndex,
  query: string,
  limit = SEARCH_LIMIT,
): SearchResult {
  const q = query.trim().toLowerCase();
  if (!q) return { hits: [], total: 0 };

  const b0: SearchHit[] = [];
  const b1: SearchHit[] = [];
  const b2: SearchHit[] = [];
  const b3: SearchHit[] = [];
  const byPath = q.includes("/");
  let total = 0;

  for (let i = 0; i < index.paths.length; i++) {
    const path = index.paths[i];
    const name = index.names[i];
    let bucket: SearchHit[] | null = null;
    let at = -1;

    if (byPath) {
      if (index.lowerPaths[i].includes(q)) {
        bucket = b3;
        at = -1;
      }
    } else {
      const lower = index.lowerNames[i];
      if (lower === q) {
        bucket = b0;
        at = 0;
      } else if (lower.startsWith(q)) {
        bucket = b1;
        at = 0;
      } else {
        const j = lower.indexOf(q);
        if (j >= 0) {
          bucket = b2;
          at = j;
        }
      }
    }

    if (!bucket) continue;
    total++;
    if (bucket.length < limit) {
      bucket.push({ path, name, dir: dirname(path), at, len: q.length });
    }
  }

  return { hits: [...b0, ...b1, ...b2, ...b3].slice(0, limit), total };
}

/** Every ancestor dir of a path, outermost first. */
export function ancestorsOf(path: string): string[] {
  const out: string[] = [];
  let at = path.indexOf("/");
  while (at >= 0) {
    out.push(path.slice(0, at));
    at = path.indexOf("/", at + 1);
  }
  return out;
}
