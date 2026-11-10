export function ts(iso: string | null | undefined): number {
  if (!iso) return NaN;
  return new Date(iso).getTime();
}

export function fmtClock(iso: string | null | undefined): string {
  const t = ts(iso);
  if (!Number.isFinite(t)) return "—";
  return new Date(t).toLocaleTimeString([], { hour12: false });
}

export function fmtDate(iso: string | null | undefined): string {
  const t = ts(iso);
  if (!Number.isFinite(t)) return "—";
  const d = new Date(t);
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${fmtClock(iso)}`;
}

/** L1 "started" column: today → clock, this week → weekday, older → date. */
export function fmtWhen(iso: string | null | undefined, nowMs: number): string {
  const t = ts(iso);
  if (!Number.isFinite(t)) return "—";
  const d = new Date(t);
  if (d.toDateString() === new Date(nowMs).toDateString()) {
    return d.toLocaleTimeString([], {
      hour12: false,
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  if (nowMs - t < 7 * 24 * 3600 * 1000) {
    return d.toLocaleDateString([], { weekday: "short" });
  }
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

/** The locked design prices everything in cents ($0.96); exact sub-cent
 *  figures stay in tooltips and `lime trace`. */
export function fmtCost(n: number | null | undefined): string {
  if (n == null) return "—";
  return `$${n.toFixed(2)}`;
}

/** Context-bar label unit: 61300 → "61k". */
export function fmtK(n: number): string {
  return `${Math.round(n / 1000)}k`;
}

/** A call's duration: "340ms", "1.2s", then fmtMs ("47s", "4m 07s"). */
export function fmtCallMs(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) ms = 0;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`;
  return fmtMs(ms);
}

/** A byte count: "340 B", "12.3 KB", "3.3 MB". */
export function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) n = 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

/** Clamp < 0 to 0, format ms as "47s", "4m 07s", "2h 05m". */
export function fmtMs(ms: number): string {
  if (ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const sec = s % 60;
  if (s < 3600) return `${m}m ${String(sec).padStart(2, '0')}s`;
  const h = Math.floor(m / 60);
  const min = m % 60;
  return `${h}h ${String(min).padStart(2, '0')}m`;
}

/** Runs-row and Run-header run time. An open run (no valid ended_at) is
 *  measured against nowMs, so the time ticks with the poll and keeps padded
 *  seconds (`9m 05s`). A finished run drops the seconds (`41m`, `2h 5m`). */
export function fmtDuration(
  startIso: string | null | undefined,
  endIso: string | null | undefined,
  nowMs: number,
): string {
  const start = ts(startIso);
  if (!Number.isFinite(start)) return "—";
  const endT = ts(endIso);
  const finished = Number.isFinite(endT);
  const end = finished ? endT : nowMs;
  let ms = end - start;
  if (ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (s < 3600) {
    if (!finished) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
    return `${m}m`;
  }
  const h = Math.floor(m / 60);
  const min = m % 60;
  return `${h}h ${min}m`;
}

/** Relative time of a timestamp. Invalid or missing → "—"; under a minute
 *  (including a future time) → "just now"; then floored minutes, hours, days. */
export function fmtAgo(
  iso: string | null | undefined,
  nowMs: number,
): string {
  const t = ts(iso);
  if (!Number.isFinite(t)) return "—";
  const d = nowMs - t;
  if (d < 60_000) return "just now";
  if (d < 3_600_000) return `${Math.floor(d / 60_000)}m ago`;
  if (d < 86_400_000) return `${Math.floor(d / 3_600_000)}h ago`;
  return `${Math.floor(d / 86_400_000)}d ago`;
}

// New-tracer tool_call payloads carry ok:false on tool errors; older payloads
// have no ok key and count as ok.
export function payloadOk(raw: string | null | undefined): boolean {
  if (!raw) return true;
  try {
    const p: unknown = JSON.parse(raw);
    if (p && typeof p === "object" && "ok" in p)
      return (p as { ok?: unknown }).ok !== false;
  } catch {
    /* not JSON — treat as ok */
  }
  return true;
}
