// The one model vocabulary every list on the profiles page draws from (fix 4):
// ledger models (most sessions, then cheapest) → the four claude_code aliases
// not already listed → the catalog. Catalog ids arrive as a config writes
// them (`provider/model-id`, ticket 92), so nothing is prefixed here.

import type { CatalogResponse, SessionCost } from "./types";
import { modelName } from "./models";
import { CC_ALIASES, sessionAvg } from "./profiles";
import { modelStats } from "./cost";

export interface TrayModel {
  id: string;
  name: string;
  provider: string;
  cc: boolean;
  runs: number;
  avg: number | null;
  note: string;
  group: "ledger" | "alias" | "catalog";
}

/** Metered API usage, as opposed to a provider pi holds an account with. */
const viaOpenRouter = (id: string): boolean => id.startsWith("openrouter/");

export function rankModels(
  rows: SessionCost[],
  catalog: CatalogResponse | null,
): TrayModel[] {
  const out: TrayModel[] = [];
  const seen = new Set<string>();

  const stats = modelStats(rows, true);
  const avgs = new Map<string, number | null>(
    stats.map((s) => [s.model, sessionAvg(rows, s.model)] as const),
  );
  const ledger = stats
    .toSorted(
      (a, b) =>
        b.runs - a.runs ||
        (avgs.get(a.model) ?? 9) - (avgs.get(b.model) ?? 9),
    )
    .map((st) => ({
      id: st.model,
      name: modelName(st.model),
      provider: st.sub ? "claude_code" : st.model.split("/")[0],
      cc: st.sub,
      runs: st.runs,
      avg: avgs.get(st.model) ?? null,
      note: `${st.runs} sessions`,
      group: "ledger" as const,
    }));
  for (const m of ledger) {
    seen.add(m.id);
    out.push(m);
  }

  for (const alias of CC_ALIASES) {
    if (seen.has(alias)) continue;
    seen.add(alias);
    out.push({
      id: alias,
      name: alias,
      provider: "claude_code",
      cc: true,
      runs: 0,
      avg: null,
      note: "claude_code alias — rides the subscription",
      group: "alias",
    });
  }

  if (catalog) {
    // pi's direct providers first (the accounts you are authenticated on —
    // `openai-codex` rides the ChatGPT plan the way an alias rides Claude's),
    // then the metered OpenRouter pool; provider, then name, inside each.
    const catalogModels = catalog.models
      .filter((e) => !seen.has(e.id))
      .toSorted(
        (a, b) =>
          Number(viaOpenRouter(a.id)) - Number(viaOpenRouter(b.id)) ||
          a.provider.localeCompare(b.provider) ||
          a.name.localeCompare(b.name),
      );
    for (const e of catalogModels) {
      seen.add(e.id);
      out.push({
        id: e.id,
        name: e.name,
        provider: e.provider,
        cc: e.provider === "claude_code",
        runs: 0,
        avg: null,
        note: e.context_length
          ? `${e.provider} · ${Math.round(e.context_length / 1000)}k ctx`
          : e.provider,
        group: "catalog",
      });
    }
  }

  return out;
}

export function filterTray(models: TrayModel[], q: string): TrayModel[] {
  const t = q.trim().toLowerCase();
  if (!t) return models;
  return models.filter(
    (m) =>
      m.id.toLowerCase().includes(t) ||
      m.name.toLowerCase().includes(t) ||
      m.provider.toLowerCase().includes(t) ||
      m.note.toLowerCase().includes(t),
  );
}

export const trayModels = (
  rows: SessionCost[],
  catalog: CatalogResponse | null,
  q = "",
): TrayModel[] => filterTray(rankModels(rows, catalog), q);