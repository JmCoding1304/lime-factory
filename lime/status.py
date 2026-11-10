"""Ticket 220 — `lime status`, the report (local part).

Spec: `specs/dashboard-workspace.md` › Report, plus
`specs/assets/bridge-home-report-signals.md` (202) and
`specs/assets/bridge-home-needs-you.md` (204). This module reads only: the
registry, ticket files, `docs/adr/*.md`, the home repo and the focus list. It
never writes any of them. It reads the ledger (`lime.db`) too, through a
read-only connection.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from lime import focus, git_helper, home, tracer, tracker, worktree

ITEM_KEYS = (
    "group",
    "reason",
    "project",
    "ticket",
    "title",
    "run_id",
    "branch",
    "since",
    "text",
    "cause",
    "count",
    "kind",
    "focus",
    "actions",
    "pr",
    "note",
    "summary",
    "summary_state",
)

PERSON_KINDS = ("task", "grilling", "prototype")
_GROUP_TITLES = {
    "needs_you": "Needs you",
    "failed": "Failed",
    "ready": "Ready to deploy",
    "done": "Done since last visit",
}
GROUPS = ("needs_you", "failed", "ready", "done")
GH_TIMEOUT_S = 20
GIT_FETCH_TIMEOUT_S = 30
_TYPE_WORD = re.compile(r"[A-Za-z]+")
_LEADING_DIGITS = re.compile(r"^\d+")
CAUSE_TYPES = ("crash_detected", "timeout_kill", "budget_stop", "repeat_stop")
SUMMARY_GROUPS = ("needs_you", "failed", "ready")
COUNT_REASONS = ("adr_proposed", "not_launched")

# Mirror lime/batch.py's two values (importing that module pulls in runner/config).
BATCH_KIND = "batch"  # lime/batch.py KIND
_QUEUED = "queued"  # lime/batch.py QUEUED — only a queued child counts as not started
DEAD_BATCH = "dead_batch"
_RETRY_COLUMNS = ("heartbeat_at", "retried_at", "retry_of")

BREACH_RE = re.compile(
    r"^[\w.-]+ is (read-only|limited to .+|barred from .+) but modified \d+ path\(s\):"
)
BLOCKED_RE = re.compile(r"^[\w.-]+: blocked: ")


def registered(registry: Path) -> list[tuple[str, Path]]:
    """The registry's projects as `(name, path)`, in file order.

    One project path per line; blank and `#` lines are skipped. A missing or
    unreadable registry gives []."""
    try:
        lines = registry.read_text().splitlines()
    except OSError:
        return []
    out: list[tuple[str, Path]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        path = Path(stripped).expanduser()
        out.append((path.name, path))
    return out


def _item(**fields) -> dict:
    """One report item: every key in `ITEM_KEYS`, `None` except `actions`, then
    overridden by `fields`."""
    item = {key: None for key in ITEM_KEYS}
    item["actions"] = []
    item.update(fields)
    return item


def _tickets(root: Path) -> list[tracker.Ticket]:
    """Every parseable ticket under `.lime/tickets/`, in filename order. A file
    that raises while reading or parsing is skipped, as is one with no
    `Status:` line."""
    tickets: list[tracker.Ticket] = []
    for path in sorted((root / ".lime" / "tickets").glob("*.md")):
        try:
            text = path.read_text()
            if not any(line.startswith("Status:") for line in text.splitlines()):
                continue
            tickets.append(tracker.parse(text, path))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    return tickets


def _kind(ticket: tracker.Ticket) -> str | None:
    """The first `[A-Za-z]+` word of `Type:`, lowercased; None when absent."""
    match = _TYPE_WORD.search(ticket.type or "")
    return match.group(0).lower() if match else None


def ticket_items(
    name: str,
    root: Path,
    tickets: list[tracker.Ticket],
    run_keys=frozenset(),
) -> list[dict]:
    """One `needs_you`/`ticket` item per open, unblocked person-ticket."""
    items: list[dict] = []
    for t in tickets:
        kind = _kind(t)
        if kind not in PERSON_KINDS:
            continue
        if not t.status.startswith("open"):
            continue
        if tracker.blocking(t, root):
            continue
        if t.number in run_keys:
            continue
        try:
            mtime = Path(t.path).stat().st_mtime
        except OSError:
            continue
        since = datetime.fromtimestamp(mtime, UTC).isoformat()
        items.append(
            _item(
                group="needs_you",
                reason="ticket",
                project=name,
                ticket=t.slug,
                title=t.title,
                since=since,
                kind=kind,
            )
        )
    return items


def closed_ticket_items(
    name: str,
    root: Path,
    tickets: list[tracker.Ticket],
    run_keys=frozenset(),
    previous: datetime | None = None,
) -> list[dict]:
    """One `done`/`closed` item per person-ticket whose `Status:` is closed and
    whose file changed after `previous` (the last visit). No last visit gives []."""
    if previous is None:
        return []
    items: list[dict] = []
    for t in tickets:
        kind = _kind(t)
        if kind not in PERSON_KINDS:
            continue
        if not t.status.startswith("closed"):
            continue
        if t.number in run_keys:
            continue
        try:
            mtime = Path(t.path).stat().st_mtime
        except OSError:
            continue
        changed = datetime.fromtimestamp(mtime, UTC)
        if changed <= previous:
            continue
        items.append(
            _item(
                group="done",
                reason="closed",
                project=name,
                ticket=t.slug,
                title=t.title,
                since=changed.isoformat(),
                kind=kind,
            )
        )
    return items


def adr_proposed(root: Path) -> int:
    """How many `docs/adr/*.md` (not recursive) declare `Status: proposed`."""
    count = 0
    for path in sorted((root / "docs" / "adr").glob("*.md")):
        try:
            lines = path.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            cleaned = line.replace("*", "").strip()
            if not cleaned.startswith("Status:"):
                continue
            value = cleaned[len("Status:"):].strip().lower()
            if value.startswith("proposed"):
                count += 1
            break
    return count


def classify(run_status: str, error: str | None) -> tuple[str, str] | None:
    """Map a Run's status (and its current failure text) to `(group, reason)`.

    A `fail` whose stored error names a permission breach is `needs_you`/`breach`;
    one that names a blocked partial is `needs_you`/`blocked`; everything else is
    `failed`/`fail`. `budget_exhausted` is `failed`/`budget_exhausted`. Any other
    status is None — `running` is decided by the caller, through the pid.
    """
    if run_status == "fail":
        text = error or ""
        if BREACH_RE.match(text):
            return ("needs_you", "breach")
        if BLOCKED_RE.match(text):
            return ("needs_you", "blocked")
        return ("failed", "fail")
    if run_status == "budget_exhausted":
        return ("failed", "budget_exhausted")
    return None


def _key_of(ticket: str | None) -> int | None:
    """The leading digits of `Path(ticket).name` as an int; None when there are
    none."""
    if not ticket:
        return None
    match = _LEADING_DIGITS.match(Path(ticket).name)
    return int(match.group(0)) if match else None


def _parse_dt(value: str | None) -> datetime:
    """Parse an ISO timestamp; a missing or naive one is read as UTC/most-old."""
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _first_line(text: str) -> str:
    return (text.splitlines() or [text])[0]


def _actions(run_id: str) -> list[str]:
    return [f"lime run --resume {run_id}", f"lime runs rm {run_id}"]


def summarizable(item: dict) -> bool:
    """Whether `lime focus summarize` may write a summary for this item.

    A dead-batch line is worked out on every call, so no summary row is stored
    for it.
    """
    return (
        item["group"] in SUMMARY_GROUPS
        and item["reason"] not in COUNT_REASONS
        and item["reason"] != DEAD_BATCH
    )


def summary_key(item: dict) -> str:
    """One stable identity per item: its ticket, else its Run, else reason@since."""
    return item["ticket"] or item["run_id"] or f'{item["reason"]}@{item["since"]}'


def summary_source(item: dict, root: Path) -> str:
    """The text a summarizer reads for one item. `since`, `note` and today's
    date stay out, so the fingerprint changes only when the source changes."""
    lines = [
        f'Project: {item["project"]}',
        f'Group: {_GROUP_TITLES[item["group"]]} ({item["reason"]})',
    ]
    if item["ticket"]:
        lines.append(f'Ticket: {item["ticket"]}')
    title = item["title"]
    if title:
        lines.append(f'Title: {title[2:] if title.startswith("# ") else title}')
    if item["run_id"]:
        lines.append(f'Run: {item["run_id"]}')
    if item["actions"]:
        lines.append(f'Commands: {" · ".join(item["actions"])}')
    if item["reason"] == "ticket":
        lines.append("")
        lines.append("Ticket file:")
        ticket_path = root / ".lime" / "tickets" / f'{item["ticket"]}.md'
        try:
            lines.append(ticket_path.read_text())
        except (OSError, UnicodeDecodeError):
            lines.append("")
    elif item["pr"]:
        pr = item["pr"]
        lines.append(f'PR: #{pr["number"]} {pr["state"]} · CI {pr["ci"]} · {pr["url"]}')
        if item["text"]:
            lines.append(f'Detail: {item["text"]}')
    else:
        lines.append(f'Error: {item["text"] or "(none recorded)"}')
        lines.append(f'Cause: {item["cause"] or "(none recorded)"}')
    return "\n".join(lines)


def fingerprint(source: str) -> str:
    """The sha256 hex digest a `summaries` row stores for its source."""
    return hashlib.sha256(source.encode()).hexdigest()


def _summary_rows(con: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """The `summaries` rows keyed by item_key; `{}` when the table is absent or
    any read fails, so a summaries problem never hides Run items."""
    try:
        found = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='summaries'"
        ).fetchone()
        if found is None:
            return {}
        rows = con.execute(
            "SELECT item_key, fingerprint, state, summary FROM summaries"
        ).fetchall()
    except sqlite3.Error:
        return {}
    return {row["item_key"]: row for row in rows}


def _clock(stamp: datetime | None) -> str:
    """` HH:MM` in system local time, or `""` when `stamp` is None."""
    return f" {stamp.astimezone():%H:%M}" if stamp is not None else ""


def _picked_up(
    child: str,
    runs: list[sqlite3.Row],
    open_keys: set[int],
    open_slugs: set[str],
    cutoff: datetime | None,
) -> bool:
    """A child is picked up once its ticket is closed (header or file gone) or a
    Run of it started after `cutoff`, whatever that Run's status."""
    key = _key_of(child)
    if key is not None:
        if key not in open_keys:
            return True
    elif child not in open_slugs:
        return True
    if cutoff is None:
        return False
    for row in runs:
        started = tracer._stamp(row["started_at"])
        if started is None or started <= cutoff:
            continue
        if key is not None:
            if _key_of(row["ticket"]) == key:
                return True
        elif row["ticket"] and Path(row["ticket"]).stem == child:
            return True
    return False


def _left_names(
    con: sqlite3.Connection,
    batch_id: str,
    runs: list[sqlite3.Row],
    open_keys: set[int],
    open_slugs: set[str],
    cutoff: datetime | None,
) -> list[str]:
    """The names of a batch's queued children still not picked up, in position
    order; `_key_of` digits, else the raw ticket text."""
    rows = con.execute(
        "SELECT ticket FROM batch_children WHERE batch_id=? AND state=?"
        " ORDER BY position",
        (batch_id, _QUEUED),
    ).fetchall()
    names: list[str] = []
    for row in rows:
        ticket = row["ticket"]
        if _picked_up(ticket, runs, open_keys, open_slugs, cutoff):
            continue
        key = _key_of(ticket)
        names.append(str(key) if key is not None else ticket)
    return names


def batch_items(
    name: str, con: sqlite3.Connection, now: datetime, tickets: list[tracker.Ticket]
) -> list[dict]:
    """Ticket 257 — one Failed item per dead batch whose story ended badly, worked
    out from the rows on every call; nothing is stored.

    Ticket 280 — a queued child drops off once picked up: a non-discarded Run of
    its ticket started after the dead batch's last beat (or its start, never
    having beaten), or its ticket is closed (header or file gone). No child left
    means no item; still pure reads, nothing stored."""
    try:
        present = {row[1] for row in con.execute("PRAGMA table_info(processes)")}
        picked = ", ".join(
            c if c in present else f"NULL AS {c}" for c in _RETRY_COLUMNS
        )
        rows = con.execute(
            f"SELECT run_id, started_at, ended_at, {picked} FROM processes"
            " WHERE kind=? ORDER BY started_at, id",
            (BATCH_KIND,),
        ).fetchall()
        has_children = (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table'"
                " AND name='batch_children'"
            ).fetchone()
            is not None
        )
        runs = con.execute(
            "SELECT ticket, status, started_at FROM runs WHERE kind='run'"
        ).fetchall()
        open_keys = {t.number for t in tickets if not t.status.startswith("closed")}
        open_slugs = {t.slug for t in tickets if not t.status.startswith("closed")}
        retries = {
            row["retry_of"]: row for row in rows if row["retry_of"] is not None
        }
        items: list[dict] = []
        for row in rows:
            if row["retry_of"] is not None:
                continue
            if row["ended_at"] is not None:
                continue
            if tracer.liveness(con, row["run_id"], now, kind=BATCH_KIND) != "dead":
                continue
            died = tracer._stamp(row["heartbeat_at"])
            cutoff = died or tracer._stamp(row["started_at"])
            text = "batch died" + _clock(died)
            retry = retries.get(row["run_id"])
            if retry is not None:
                if retry["ended_at"] is not None:
                    continue
                if (
                    tracer.liveness(
                        con, retry["run_id"], now, kind=BATCH_KIND
                    )
                    != "dead"
                ):
                    continue
                text += " · retry died" + _clock(
                    tracer._stamp(retry["heartbeat_at"])
                )
                source = retry["run_id"]
            elif row["retried_at"] is not None:
                text += " · retry failed to start"
                source = row["run_id"]
            else:
                text += " · not retried"
                source = row["run_id"]
            if has_children:
                left = _left_names(
                    con, source, runs, open_keys, open_slugs, cutoff
                )
                if not left:
                    continue
                text += " · not started: " + ", ".join(left)
            items.append(
                _item(
                    group="failed",
                    reason=DEAD_BATCH,
                    project=name,
                    run_id=row["run_id"],
                    since=row["heartbeat_at"] if died is not None else None,
                    text=text,
                )
            )
        return items
    except sqlite3.Error:
        return []


def _attach_summaries(
    items: list[dict], rows: dict[str, sqlite3.Row], root: Path
) -> None:
    """Set `summary_state`/`summary` per item from `rows`. Non-summarizable items
    keep both `None`; a missing, changed or `working` row reads `pending`."""
    for item in items:
        if not summarizable(item):
            continue
        row = rows.get(summary_key(item))
        if (
            row is not None
            and row["state"] != "working"
            and row["fingerprint"] == fingerprint(summary_source(item, root))
        ):
            if row["state"] == "ok":
                item["summary_state"] = "ok"
                item["summary"] = row["summary"]
            else:
                item["summary_state"] = "failed"
        else:
            item["summary_state"] = "pending"


def _cause(con: sqlite3.Connection, run_id: str) -> str | None:
    row = con.execute(
        "SELECT type FROM events WHERE run_id=? AND type IN"
        f" ({','.join('?' for _ in CAUSE_TYPES)})"
        " ORDER BY event_id DESC LIMIT 1",
        (run_id, *CAUSE_TYPES),
    ).fetchone()
    return row["type"] if row is not None else None


def _run_item(
    name: str,
    con: sqlite3.Connection,
    titles: dict[int, str],
    key,
    row: sqlite3.Row,
    previous: datetime | None = None,
) -> dict | None:
    """The item for one Run, or None when the Run needs no attention."""
    run_id = row["run_id"]
    status = row["status"]
    ticket = Path(row["ticket"]).stem if row["ticket"] else None
    title = titles.get(key) if isinstance(key, int) else None
    base = dict(
        project=name,
        ticket=ticket,
        title=title,
        run_id=run_id,
        branch=row["branch"],
        actions=_actions(run_id),
    )
    if status == "deployed":
        if previous is None:
            return None
        deploy_row = con.execute(
            "SELECT max(ended_at) AS t FROM phases WHERE run_id=? AND kind='deploy'",
            (run_id,),
        ).fetchone()
        event_row = con.execute(
            "SELECT max(started_at) AS t FROM events WHERE run_id=?", (run_id,)
        ).fetchone()
        stamp = (
            (deploy_row["t"] if deploy_row is not None else None)
            or (event_row["t"] if event_row is not None else None)
            or row["ended_at"]
            or row["started_at"]
        )
        if _parse_dt(stamp) > previous:
            item = _item(group="done", reason="deployed", since=stamp, **base)
            item["actions"] = []
            return item
        return None
    if status == "fail":
        error_row = con.execute(
            "SELECT error FROM phases WHERE run_id=? AND status='fail'"
            " ORDER BY ended_at DESC, seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        text = error_row["error"] if error_row is not None else None
        group, reason = classify("fail", text)
        since = row["ended_at"] or row["started_at"]
        cause = _cause(con, run_id) if group == "failed" else None
        return _item(group=group, reason=reason, text=text, since=since, cause=cause, **base)
    if status == "budget_exhausted":
        group, reason = classify("budget_exhausted", None)
        since = row["ended_at"] or row["started_at"]
        return _item(
            group=group, reason=reason, since=since, cause=_cause(con, run_id), **base
        )
    if status == "running":
        if tracer.liveness(con, run_id, datetime.now(UTC)) != "dead":
            return None
        since_row = con.execute(
            "SELECT max(started_at) AS t FROM events WHERE run_id=?", (run_id,)
        ).fetchone()
        since = (since_row["t"] if since_row is not None else None) or row["started_at"]
        return _item(
            group="failed",
            reason="stale_running",
            since=since,
            cause=_cause(con, run_id),
            **base,
        )
    return None


def run_items(
    name: str,
    con: sqlite3.Connection,
    titles: dict[int, str],
    previous: datetime | None = None,
) -> tuple[list[dict], set[int], set[int], dict[int, dict]]:
    """The Run items (newest Run alone speaks for its ticket), plus the ticket keys
    that have a Run, the keys with a non-discarded Run, and the newest row per int
    key."""
    rows = con.execute(
        "SELECT run_id, ticket, status, branch, started_at, ended_at,"
        " base_sha, pr_number, pr_url FROM runs"
        " WHERE kind='run' ORDER BY started_at DESC, run_id DESC"
    ).fetchall()
    items: list[dict] = []
    run_keys: set[int] = set()
    launched: set[int] = set()
    newest: dict[int, dict] = {}
    seen: set = set()
    for row in rows:
        key = _key_of(row["ticket"])
        if key is None:
            key = ("run", row["run_id"])
        else:
            run_keys.add(key)
            if row["status"] != "discarded":
                launched.add(key)
        if key in seen:
            continue
        seen.add(key)
        if isinstance(key, int):
            newest[key] = row
        item = _run_item(name, con, titles, key, row, previous)
        if item is not None:
            items.append(item)
    return items, run_keys, launched, newest


def refusal_items(
    name: str, con: sqlite3.Connection, newest: dict[int, dict], titles: dict[int, str]
) -> list[dict]:
    """One `needs_you`/`refusal` item per key with an uncleared refusal."""
    rows = con.execute(
        "SELECT event_id, run_id, name, payload_json, started_at FROM events"
        " WHERE type='refusal' ORDER BY started_at DESC, event_id DESC"
    ).fetchall()
    items: list[dict] = []
    seen: set = set()
    for row in rows:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        ticket = payload.get("ticket")
        key = _key_of(ticket)
        if key is None:
            key = ("refusal", row["event_id"])
        if key in seen:
            continue
        seen.add(key)
        if isinstance(key, int):
            run = newest.get(key)
            if run is not None:
                if run["status"] == "deployed":
                    continue
                if _parse_dt(run["started_at"]) > _parse_dt(row["started_at"]):
                    continue
        detail = {k: v for k, v in payload.items() if k != "ticket"}
        text = row["name"]
        if detail:
            text += " " + json.dumps(detail, sort_keys=True)
        items.append(
            _item(
                group="needs_you",
                reason="refusal",
                project=name,
                ticket=ticket,
                title=titles.get(key) if isinstance(key, int) else None,
                run_id=None if row["run_id"] == tracer.NO_RUN else row["run_id"],
                since=row["started_at"],
                text=text,
            )
        )
    return items


def _select(run_list: list[dict], ref_list: list[dict]) -> list[dict]:
    """Per key, first match wins (breach > blocked > refusal > Failed)."""
    run_by_key: dict[int, dict] = {}
    ref_by_key: dict[int, dict] = {}
    extra: list[dict] = []
    for item in run_list:
        key = _key_of(item["ticket"])
        if isinstance(key, int):
            run_by_key[key] = item
        else:
            extra.append(item)
    for item in ref_list:
        key = _key_of(item["ticket"])
        if isinstance(key, int):
            ref_by_key[key] = item
        else:
            extra.append(item)
    out: list[dict] = []
    for key in sorted(set(run_by_key) | set(ref_by_key)):
        run_item = run_by_key.get(key)
        if run_item is not None and run_item["group"] == "needs_you":
            out.append(run_item)
        elif key in ref_by_key:
            out.append(ref_by_key[key])
        elif run_item is not None:
            out.append(run_item)
    out.extend(extra)
    return out


def not_launched(
    root: Path, tickets: list[tracker.Ticket], launched: set[int]
) -> int:
    """How many workflow, non-person, open, unblocked tickets have not launched."""
    count = 0
    for t in tickets:
        if not t.workflow:
            continue
        if _kind(t) in PERSON_KINDS:
            continue
        if not t.status.startswith("open"):
            continue
        if tracker.blocking(t, root):
            continue
        if t.number in launched:
            continue
        count += 1
    return count


def cost(projects: list[tuple[str, Path]], since: str | None) -> dict:
    """Cash vs subscription spend across the selected projects, since `since`."""
    cash = 0.0
    subscription = 0.0
    by_provider: dict[str, float] = {}
    base_sql = (
        "SELECT s.runtime, s.model, sum(json_extract(e.payload_json,'$.cost')) AS cost"
        " FROM events e JOIN sessions s ON s.session_id = e.name"
        " WHERE e.type='turn'"
    )
    for _name, path in projects:
        db_path = path / ".lime" / "runs" / "lime.db"
        if not db_path.exists():
            continue
        con = None
        try:
            con = tracer.connect(db_path, readonly=True)
            if since is not None:
                rows = con.execute(
                    base_sql + " AND e.started_at >= ? GROUP BY s.runtime, s.model",
                    (since,),
                ).fetchall()
            else:
                rows = con.execute(base_sql + " GROUP BY s.runtime, s.model").fetchall()
        except sqlite3.Error:
            continue
        finally:
            if con is not None:
                con.close()
        for row in rows:
            amount = row["cost"] or 0.0
            model = row["model"] or ""
            if model.startswith("openrouter/"):
                cash += amount
            else:
                subscription += amount
            provider = (
                "claude_code" if row["runtime"] == "claude_code" else model.split("/")[0]
            )
            by_provider[provider] = by_provider.get(provider, 0.0) + amount
    return {
        "window": "since_last_visit" if since is not None else "all",
        "since": since,
        "cash_usd": round(cash, 4),
        "subscription_usd_equiv": round(subscription, 4),
        "by_provider": {k: round(v, 4) for k, v in by_provider.items()},
    }


def home_block() -> dict:
    """The home repo's report card: `ahead`, `push_error`, `last_visit`. Never
    creates home."""
    h = home.home_dir()
    ahead = 0
    if home._has_commit(h):
        try:
            ahead = int(home._pending_count(h))
        except ValueError:
            ahead = 0
    push_error = None
    error_path = h / "push-error"
    if error_path.exists():
        try:
            push_error = home._first_line(error_path.read_text()) or None
        except (OSError, UnicodeDecodeError):
            push_error = None
    last_visit = None
    visit = home._read_visit(h)
    if visit is not None:
        previous, current = visit
        last_visit = {"previous": home._stamp(previous), "current": home._stamp(current)}
    return {"ahead": ahead, "push_error": push_error, "last_visit": last_visit}


def rank(items: list[dict], rows: list[dict]) -> list[dict]:
    """Set each item's `focus` from `rows` and sort by group, focus rank, age,
    then the tie-breakers."""
    best: dict[str, tuple[int, str]] = {}
    for i, row in enumerate(rows):
        if row.get("done"):
            continue
        best.setdefault(row["project"], (i, row["id"]))

    for item in items:
        match = best.get(item["project"])
        item["focus"] = match[1] if match else None

    def key(item: dict):
        match = best.get(item["project"])
        focus_rank = match[0] if match else len(rows)
        since = item["since"]
        if since is None:
            age_flag, age = 1, datetime.min.replace(tzinfo=UTC)
        else:
            age = datetime.fromisoformat(since)
            if age.tzinfo is None:
                age = age.replace(tzinfo=UTC)
            age_flag = 0
        group_rank = GROUPS.index(item["group"])
        if item["group"] == "done":
            return (
                group_rank,
                0,
                0,
                -age.timestamp(),
                item["project"] or "",
                item["ticket"] or "",
                item["reason"] or "",
            )
        return (
            group_rank,
            focus_rank,
            age_flag,
            age,
            item["project"] or "",
            item["ticket"] or "",
            item["reason"] or "",
        )

    items.sort(key=key)
    return items


def _gh_reads(jobs: dict[str, tuple[Path, list]]) -> tuple[dict, str | None]:
    """Read every candidate PR through `git_helper.pr_view`, one worker per project.

    `jobs` maps a project name to `(path, candidates)` where candidates is a list of
    `(key, row)` pairs. Returns `{(project, key): pr_dict}` and the first `RuntimeError`
    message (or None). Calls within one project run in order and stop at their first
    error; only a `RuntimeError` is caught — anything else propagates via `future.result()`.
    """
    work = {name: (path, cands) for name, (path, cands) in jobs.items() if cands}
    if not work:
        return {}, None
    results: dict = {}
    first_error: str | None = None

    def read(name: str, path: Path, cands: list) -> tuple[str, dict, str | None]:
        out: dict = {}
        for key, row in cands:
            try:
                out[(name, key)] = git_helper.pr_view(
                    row["pr_url"], cwd=path, timeout=GH_TIMEOUT_S
                )
            except RuntimeError as error:
                return name, out, str(error)
        return name, out, None

    with ThreadPoolExecutor(max_workers=len(work)) as pool:
        futures = {
            name: pool.submit(read, name, path, cands)
            for name, (path, cands) in work.items()
        }
    for name, future in futures.items():
        _name, out, error = future.result()
        results.update(out)
        if error is not None and first_error is None:
            first_error = error
    return results, first_error


def _git_reads(jobs: dict[str, tuple[Path, list]], prs: dict) -> tuple[dict, list[str]]:
    """Resolve conflicts and base movement for every OPEN PR, one worker per project.

    `jobs` maps a project name to `(path, candidates)`; only candidates whose `prs`
    read is OPEN are considered. Returns `{(project, key): {"conflicts": [...],
    "movement": BaseMovement}}` and a note per project whose git read raised.
    """
    work: dict[str, tuple[Path, list]] = {}
    for name, (path, cands) in jobs.items():
        open_rows = [
            (key, row)
            for key, row in cands
            if (prs.get((name, key)) or {}).get("state") == "OPEN"
        ]
        if open_rows:
            work[name] = (path, open_rows)
    if not work:
        return {}, []
    results: dict = {}
    notes: list[str] = []

    def read(name: str, path: Path, rows: list) -> tuple[str, dict, str | None]:
        try:
            base = worktree.default_branch(path)
            git_helper.fetch_origin(path, timeout=GIT_FETCH_TIMEOUT_S)
            default_sha = git_helper.resolve_remote_tip(
                path, f"origin/{base}", label="default branch"
            )
            out: dict = {}
            for key, row in rows:
                head = git_helper.resolve_remote_tip(
                    path, f"origin/{row['branch']}", label="PR head"
                )
                conflicts = git_helper.merge_tree_conflicts(path, default_sha, head)
                movement = git_helper.base_movement(path, row["base_sha"], default_sha)
                out[key] = {"conflicts": conflicts, "movement": movement}
            return name, out, None
        except RuntimeError as error:
            return name, {}, str(error)

    with ThreadPoolExecutor(max_workers=len(work)) as pool:
        futures = {
            name: pool.submit(read, name, path, rows)
            for name, (path, rows) in work.items()
        }
    for name, future in futures.items():
        _name, out, error = future.result()
        if error is not None:
            notes.append(f"git: skipped ({name}: {_first_line(error)})")
        else:
            results.update({(name, key): value for key, value in out.items()})
    return results, notes


def pr_item(
    name: str,
    key,
    row: sqlite3.Row,
    titles: dict[int, str],
    pr: dict,
    git: dict | None,
    previous: datetime | None,
) -> dict | None:
    """The item for one success Run with a persisted PR, or None (decision 5).

    OPEN: CI red -> `needs_you`/`ci_red`; a merge-tree conflict -> `needs_you`/`conflict`;
    CI green and clean -> `ready` (note `base moved N` when the base is behind); CI
    pending and clean -> no item. MERGED after the previous visit -> `done`/`merged`;
    anything else -> no item. A failed git read leaves `git` None: red CI still speaks
    (`ci_red`), but Ready is only ever asserted from a successful clean merge-tree read,
    so a missing git read stays silent rather than guessing.
    """
    ticket = Path(row["ticket"]).stem if row["ticket"] else None
    title = titles.get(key) if isinstance(key, int) else None
    ci = git_helper.ci_status(pr.get("statusCheckRollup") or [])
    pr_field = {
        "number": row["pr_number"],
        "url": pr.get("url") or row["pr_url"],
        "state": pr.get("state"),
        "ci": ci,
    }
    base = dict(
        project=name,
        ticket=ticket,
        title=title,
        run_id=row["run_id"],
        branch=row["branch"],
        pr=pr_field,
    )
    state = pr.get("state")
    if state == "OPEN":
        since = row["ended_at"] or row["started_at"]
        if ci == "red":
            return _item(group="needs_you", reason="ci_red", since=since, **base)
        # A Ready verdict asserts a clean merge onto the default tip; without a
        # successful git read that is unproven, so never emit Ready on a failed read.
        if git is None:
            return None
        if git["conflicts"]:
            return _item(
                group="needs_you",
                reason="conflict",
                text="conflict in: " + ", ".join(git["conflicts"]),
                since=since,
                **base,
            )
        if ci == "green":
            movement = git["movement"]
            note = f"base moved {movement.merges}" if movement.status == "behind" else None
            return _item(
                group="ready",
                reason="ready",
                since=since,
                note=note,
                actions=[f"lime deploy {ticket}"],
                **base,
            )
        return None
    if state == "MERGED":
        merged_at = pr.get("mergedAt")
        if previous is None or not merged_at:
            return None
        if _parse_dt(merged_at) > previous:
            return _item(
                group="done", reason="merged", since=merged_at, **base
            )
        return None
    return None


def report(registry: Path, project: str | None = None, *, gh: bool = True) -> dict:
    """The whole report: `generated_at`, per-project rows, ranked `items`, `cost`
    and the `home` block."""
    t0 = time.monotonic()
    visit = home._read_visit(home.home_dir())
    previous = visit[0] if visit is not None else None
    notes: list[str] = []
    if not gh:
        notes.append("gh: skipped (--no-gh)")
    read_ms = {"db": 0, "gh": 0, "git": 0, "total": 0}
    all_items: list[dict] = []
    projects: list[dict] = []
    selected: list[tuple[str, Path]] = []
    jobs: dict[str, dict] = {}
    db_t0 = time.monotonic()
    for name, path in registered(registry):
        if project is not None and name != project:
            continue
        selected.append((name, path))
        exists = path.exists()
        db_path = path / ".lime" / "runs" / "lime.db"
        db = db_path.exists()
        tickets = _tickets(path) if exists else []
        titles = {t.number: t.title for t in tickets}
        error = None
        run_keys: set[int] = set()
        launched: set[int] = set()
        run_list: list[dict] = []
        ref_list: list[dict] = []
        batch_list: list[dict] = []
        newest: dict[int, dict] = {}
        summary_rows: dict[str, sqlite3.Row] = {}
        if exists and db:
            con = None
            try:
                con = tracer.connect(db_path, readonly=True)
                run_list, run_keys, launched, newest = run_items(
                    name, con, titles, previous
                )
                ref_list = refusal_items(name, con, newest, titles)
                summary_rows = _summary_rows(con)
                batch_list = batch_items(
                    name, con, datetime.now(UTC), tickets
                )
            except sqlite3.Error as e:
                error = str(e)
                run_keys = set()
                launched = set()
                run_list = []
                ref_list = []
                batch_list = []
                newest = {}
                summary_rows = {}
            finally:
                if con is not None:
                    con.close()
        candidates = [
            (key, row)
            for key, row in newest.items()
            if row["status"] == "success" and row["pr_url"]
        ]
        jobs[name] = {
            "name": name,
            "path": path,
            "db": db,
            "exists": exists,
            "tickets": tickets,
            "titles": titles,
            "error": error,
            "run_list": run_list,
            "ref_list": ref_list,
            "batch_list": batch_list,
            "run_keys": run_keys,
            "launched": launched,
            "newest": newest,
            "candidates": candidates,
            "summary_rows": summary_rows,
        }
    read_ms["db"] = int((time.monotonic() - db_t0) * 1000)

    prs: dict = {}
    git_info: dict = {}
    remote = {name: (job["path"], job["candidates"]) for name, job in jobs.items()}
    if gh:
        gh_t0 = time.monotonic()
        prs, gh_error = _gh_reads(remote)
        read_ms["gh"] = int((time.monotonic() - gh_t0) * 1000)
        if gh_error is not None:
            prs = {}
            notes.append("gh: skipped (" + _first_line(gh_error) + ")")
        else:
            git_t0 = time.monotonic()
            git_info, git_notes = _git_reads(remote, prs)
            read_ms["git"] = int((time.monotonic() - git_t0) * 1000)
            notes.extend(git_notes)

    for name, job in jobs.items():
        path = job["path"]
        tickets = job["tickets"]
        titles = job["titles"]
        pr_items: list[dict] = []
        resolved_refusals: set = set()
        for key, row in job["candidates"]:
            pr = prs.get((name, key))
            if pr is None:
                continue
            if pr.get("state") in ("MERGED", "CLOSED"):
                # The PR's own outcome now speaks for the ticket: a hand merge is
                # Done, a closed-unmerged PR is no item — either way a stale local
                # refusal must not outrank that verdict.
                resolved_refusals.add(key)
            item = pr_item(
                name, key, row, titles, pr, git_info.get((name, key)), previous
            )
            if item is not None:
                pr_items.append(item)
        ref_list = [
            item
            for item in job["ref_list"]
            if _key_of(item.get("ticket")) not in resolved_refusals
        ]
        ledger_items = _select(job["run_list"] + pr_items, ref_list)
        items: list[dict] = []
        if job["exists"]:
            covered = set(job["run_keys"])
            for item in ledger_items:
                key = _key_of(item.get("ticket"))
                if isinstance(key, int):
                    covered.add(key)
            items = ticket_items(name, path, tickets, covered)
            items.extend(closed_ticket_items(name, path, tickets, covered, previous))
            n_adr = adr_proposed(path)
            if n_adr > 0:
                items.append(
                    _item(
                        group="needs_you",
                        reason="adr_proposed",
                        project=name,
                        count=n_adr,
                    )
                )
            items.extend(ledger_items)
            items.extend(job["batch_list"])
            if job["error"] is None:
                n_nl = not_launched(path, tickets, job["launched"])
                if n_nl > 0:
                    items.append(
                        _item(
                            group="needs_you",
                            reason="not_launched",
                            project=name,
                            count=n_nl,
                        )
                    )
        _attach_summaries(items, job["summary_rows"], path)
        counts = {g: 0 for g in GROUPS}
        for item in items:
            counts[item["group"]] += 1
        all_items.extend(items)
        projects.append(
            {
                "name": name,
                "path": str(path),
                "db": job["db"],
                "error": job["error"],
                "counts": counts,
            }
        )
    ranked = rank(all_items, focus.load())
    since = previous.astimezone(UTC).isoformat() if previous is not None else None
    result = {
        "generated_at": home._stamp(home._now()),
        "projects": projects,
        "items": ranked,
        "cost": cost(selected, since),
        "home": home_block(),
        "notes": notes,
        "read_ms": read_ms,
    }
    read_ms["total"] = int((time.monotonic() - t0) * 1000)
    return result


def _print_plain(data: dict) -> None:
    any_items = False
    for group in GROUPS:
        group_items = [item for item in data["items"] if item["group"] == group]
        if not group_items:
            continue
        any_items = True
        print(_GROUP_TITLES[group])
        for item in group_items:
            label = (
                item["ticket"]
                if item["ticket"]
                else f'{item["count"]} waiting'
                if item["count"] is not None
                else item["run_id"]
            )
            line = f'  {item["project"]} · {item["reason"]} · {label}'
            if item["text"]:
                line += f' · {item["text"].splitlines()[0][:80]}'
            if item["note"]:
                line += f' · {item["note"]}'
            if item["focus"]:
                line += f' · {item["focus"]}'
            print(line)
    if not any_items:
        print("nothing needs you")
    c = data["cost"]
    print(
        f'cost since {c["since"] or "ever"}: cash ${c["cash_usd"]:.2f}'
        f' · subscription ${c["subscription_usd_equiv"]:.2f}'
    )
    h = data["home"]
    line = f'home: {h["ahead"]} ahead'
    if h["push_error"]:
        line += f' · push error: {h["push_error"]}'
    print(line)
    for note in data["notes"]:
        print(note)


def command(args, registry: Path) -> int:
    """Dispatch `lime status`: render the report, plain or `--json`."""
    name = getattr(args, "project", None)
    if name is not None and name not in {project_name for project_name, _ in registered(registry)}:
        print(f'lime status: project "{name}" is not registered in {registry}', file=sys.stderr)
        return 2
    data = report(registry, project=name, gh=not getattr(args, "no_gh", False))
    if args.json:
        print(json.dumps(data))
    else:
        _print_plain(data)
    return 0
