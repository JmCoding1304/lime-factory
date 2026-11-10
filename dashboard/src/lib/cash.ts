import type { EventRow, ProviderSpend, Session } from "./types";
import { isCashModel } from "./types";
import { parsePayload } from "./events";
import { fmtCost } from "./format";

/**
 * The sidebar's Spend · 7 days line: the cash rows summed, and every
 * subscription row collapsed into one faint `+sub` whose title lists each by
 * provider and its notional figure. `spend` already carries the subscription
 * flag per row (one cash rule, `isCashModel`).
 */
export function spendLine(spend: ProviderSpend[]): {
  cash: number;
  sub: boolean;
  subTitle: string;
} {
  const subRows = spend.filter((p) => p.subscription);
  const cash = spend
    .filter((p) => !p.subscription)
    .reduce((sum, p) => sum + p.cost, 0);
  const subTitle = subRows.length
    ? `${subRows
        .map((p) => `${p.provider} $${p.cost.toFixed(2)}`)
        .join(" · ")} — notional, not billed`
    : "";
  return {
    cash: Math.round(cash * 100) / 100,
    sub: subRows.length > 0,
    subTitle,
  };
}

/**
 * The Run header's cost line: the Run's own turns folded under the one cash
 * rule (`isCashModel`), the client mirror of the server's Step 3 fold. A turn's
 * session comes from `sessions.session_id → model`; a turn whose `name` matches
 * no session, or a session with a non-cash model, marks the Run as subscription.
 */
export function runCash(
  sessions: Session[],
  events: EventRow[],
): { cash: number; sub: boolean } {
  const modelBy = new Map<string, string | null>();
  for (const s of sessions) modelBy.set(s.session_id, s.model);

  let cash = 0;
  let sub = sessions.some((s) => !isCashModel(s.model));
  for (const e of events) {
    if (e.type !== "turn") continue;
    const cost = parsePayload(e.payload_json)?.cost;
    if (typeof cost !== "number") continue;
    if (isCashModel(modelBy.get(e.name ?? ""))) cash += cost;
    else sub = true;
  }

  return { cash: Math.round(cash * 100) / 100, sub };
}

/**
 * One phase's Cost fact. Null when the phase has neither a Session nor any
 * turn events (a code or engineer phase) — there is nothing to price. The
 * text follows the one cash rule (`runCash`): a cash figure (with ` +sub`
 * when a subscription Session also ran), the bare word `subscription` when
 * only a subscription ran, and `$0.00 · cash` for a cash model that has not
 * spent yet.
 */
export function phaseCost(
  sessions: Session[],
  events: EventRow[],
): { text: string; title: string } | null {
  if (sessions.length === 0 && !events.some((e) => e.type === "turn")) {
    return null;
  }
  const c = runCash(sessions, events);
  if (c.cash > 0) {
    let text = `${fmtCost(c.cash)} · cash`;
    if (c.sub) text += " +sub";
    return { text, title: "billed in cash via OpenRouter" };
  }
  if (c.sub) {
    return { text: "subscription", title: "rides a subscription, not billed" };
  }
  return { text: `${fmtCost(0)} · cash`, title: "billed in cash via OpenRouter" };
}

/**
 * The Run line's cost segment: the cash figure when there is cash, else the
 * word `subscription`. Only `openrouter/*` turns count (via `runCash`), so a
 * Run with no sessions, a subscription-only Run and an openrouter Run that has
 * spent nothing all read `subscription` — never `$0.00`.
 */
export function cashWord(cost: { cash: number }): string {
  return cost.cash > 0 ? fmtCost(cost.cash) : "subscription";
}

/**
 * The Run tab's cost chip: the cash figure plus its booking context, or the
 * subscription placeholder. Never `$0.00` in the text; the title's booked
 * figure is the Run's full cost, cash or notional.
 */
export function cashDetail(
  cost: { cash: number; sub: boolean },
  booked: number | null,
  live: boolean,
): { text: string; title: string } {
  if (cost.cash > 0) {
    let text = `${fmtCost(cost.cash)} cash`;
    if (cost.sub) text += " +sub";
    if (live) text += " so far";
    return {
      text,
      title: `cash ${fmtCost(cost.cash)} · booked ${fmtCost(booked ?? 0)}`,
    };
  }
  return { text: "subscription", title: "subscription — no cash spend" };
}
