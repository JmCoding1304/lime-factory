"""`lime` entry point. `init`, `run`, `runs`, `trace`, `cost`, `dash`, `dash init`, `dash visit`, `check`,
`deploy`, `research`, `scout`, `prototype`, `ticket next`, `ticket close`, `ticket reopen`, `hook budget`,
`guard`, `focus` and `status` are live."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path

from lime import batch, budget, config, docs_merge, focus, gates, git_helper, home, numbering, overlap, permissions, quality, session, summaries, tracer, tracker, wizard, worktree
from lime import status as status_report
from lime.console import Console
from lime.data_types import AgentCall, AgentConfig, DesignOutput, PhaseParams, ResearchOutput
from lime.utils import engineer_name, new_id, operator_env, resolve_prompt
from lime.workflows import feature, phases

COMMANDS = {
    "init": "stamp .lime/ and the lime skill into a project",
    "run": "launch a Run per ticket",
    "batch": "plumbing: `watch <batch_id>` is the watchdog a batch starts beside itself — retries the batch once if it dies",
    "ticket": "ticket plumbing: `next` prints the next free ticket number; `close`/`reopen` <project> <NN> set its Status: line (owner-only)",
    "runs": "list Runs / discard one",
    "trace": "phase table + gates + events for a Run",
    "cost": "spend by provider then role",
    "dash": "dashboard over every registered project; `init` creates the home repo ~/.lime/dashboard/, `visit` records an open of home",
    "focus": "the owner's goal list in the home repo: list [--json] / add / done / edit (writes are owner-only) / summarize (AI 'what to do' per report item, written to lime.db)",
    "status": "the report: Needs you / Failed items over every registered project (read-only); --json for the dashboard",
    "check": "full validation + resolved-roster print",
    "deploy": "merge PR, clean up, run the deploy commands",
    "research": "on-demand researcher Session",
    "scout": "on-demand scout Session: a repo question, answered as paths + notes",
    "prototype": "designer loop: start / continue / lock",
    "hook": "Orchestrator UserPromptSubmit budget hook",
    "guard": "per-tool-call budget messenger",
    "profiles": "read/write agents: and profiles: blocks of .lime/config.yaml (dashboard profiles page)",
    "models": "every model pi can run, as JSON (dashboard model tray)",
}

RUNS_DIR = Path(".lime/runs")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lime",
        description="limefactory — ticket in, reviewed PR out.",
    )
    parser.add_argument(
        "--version", action="version", version=f"lime {version('limefactory')}"
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name, summary in COMMANDS.items():
        sub = subparsers.add_parser(name, help=summary)
        if name == "hook":  # spec §3 names the plumbing row `lime hook budget`
            budget_p = sub.add_subparsers(metavar="budget").add_parser("budget")
            budget_p.add_argument("--tokens", type=int, help="pi: context tokens from budget.ts, instead of a Claude Code transcript on stdin")
            budget_p.add_argument("--path", action="store_true", help="pi: print the budget.ts extension path for -e")
        elif name == "trace":
            sub.add_argument("run_id", nargs="?", help="defaults to the latest Run")
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument("--follow", action="store_true", help="tail new events")
            mode.add_argument("--summary", action="store_true", help="one screen, ≤20 lines")
        elif name == "cost":
            sub.add_argument(
                "run_id", nargs="?", help="one Run's whole life (default: last 7 days)"
            )
        elif name == "init":
            sub.add_argument("--no-input", action="store_true", help="never prompt (the setup wizard runs only on a terminal)")
        elif name == "check":
            sub.add_argument("--profile", help="resolve the roster under this profile")
        elif name == "dash":
            sub.add_argument("--port", type=int, help="API server port for both the server and Vite (default: $PORT, else $LIME_PORT, else 4600)")
            dash_sub = sub.add_subparsers(dest="dash_command", metavar="<dash-command>")
            dash_sub.add_parser("init", help="create the private home repo ~/.lime/dashboard/ and its <gh account>/lime-dashboard origin (owner-only, idempotent)")
            dash_sub.add_parser("visit", help="record an open of home: previous ← current after a 30-min gap; prints both stamps (owner-only)")
        elif name == "focus":
            focus_sub = sub.add_subparsers(dest="focus_command", metavar="<focus-command>", required=True)
            focus_list = focus_sub.add_parser("list", help="grouped rows from the home repo (read-only)")
            focus_list.add_argument("--json", action="store_true", help="print a flat JSON array")
            focus_add = focus_sub.add_parser("add", help="append a new row (owner-only)")
            focus_add.add_argument("title", help="the row's title")
            focus_add.add_argument("--due", help="YYYY-MM-DD")
            focus_add.add_argument("--project", help="a registered project name")
            focus_done = focus_sub.add_parser("done", help="check a row's box (owner-only)")
            focus_done.add_argument("id", help="the row id, e.g. f3")
            focus_edit = focus_sub.add_parser("edit", help="change a row's fields (owner-only)")
            focus_edit.add_argument("id", help="the row id, e.g. f3")
            focus_edit.add_argument("--title", help="a new title")
            focus_edit.add_argument("--due", help="YYYY-MM-DD, or \"\" to clear")
            focus_edit.add_argument("--project", help="a registered project name, or \"\" to clear")
            focus_edit.add_argument("--reopen", action="store_true", help="uncheck the row")
            focus_sum = focus_sub.add_parser("summarize", help="write a short 'what to do' for each changed Needs-you, Failed and Ready item (lime.db only; not owner-only)")
            focus_sum.add_argument("--project", help="one registered project name")
        elif name == "status":
            sub.add_argument("--project", help="one registered project name")
            sub.add_argument("--json", action="store_true", help="print the report as one JSON object")
            sub.add_argument("--no-gh", action="store_true", help="skip the gh reads; the local report never calls gh")
        elif name == "run":
            sub.add_argument(
                "ticket", nargs="*", help="ticket number(s) — more than one runs as a batch"
            )
            sub.add_argument("--batch-id", help=argparse.SUPPRESS)
            sub.add_argument("--retry-of", metavar="BATCH_ID", help=argparse.SUPPRESS)
            sub.add_argument("--profile", help="resolve the roster under this profile")
            sub.add_argument(
                "--resume", metavar="RUN_ID", help="restart the failed Phase of an existing Run"
            )
            sub.add_argument(
                "--replan", action="store_true",
                help="with --resume: re-plan against the current ticket (required when the ticket or plan changed)",
            )
            sub.add_argument(
                "--rereview", action="store_true",
                help="with --resume: re-verify and re-review a Run that reached review — invalidates from the last verify before the first review; scout, plan and builds replay",
            )
            sub.add_argument(
                "--allow-overlap", action="store_true",
                help="single fresh-ticket escape hatch: skip the unmerged-origin-branch overlap check"
                     " (never batches or --resume; never blockers or reservation constraints)",
            )
            sub.add_argument(
                "--deploy", action="store_true",
                help="batch: carry each child that ends success through `lime deploy`, then launch"
                     " what it unblocks (needs auto_deploy: staging or prod)",
            )
        elif name == "batch":
            batch_sub = sub.add_subparsers(dest="batch_command", metavar="<batch-command>", required=True)
            watch_p = batch_sub.add_parser("watch", help="block on stdin until the batch ends; retry it once if its row is still open")
            watch_p.add_argument("batch_id")
            watch_p.add_argument("--deploy", action="store_true")
            watch_p.add_argument("--profile")
        elif name == "ticket":
            ticket_sub = sub.add_subparsers(dest="ticket_command", metavar="<ticket-command>", required=True)
            ticket_sub.add_parser(
                "next", help="next free ticket number, derived from git (origin branches, PRs, ticket commits, reservations) plus .lime/tickets/")
            for verb, value in (("close", "closed"), ("reopen", "open")):
                ticket_status = ticket_sub.add_parser(verb, help=f"owner-only: set the ticket's header Status: line to {value}")
                ticket_status.add_argument("project", help="registered project name (~/.lime/projects)")
                ticket_status.add_argument("number", metavar="NN", help="ticket number; matched by leading digits")
                ticket_status.add_argument("--json", action="store_true", help='print {"project", "ticket", "status"}')
        elif name == "runs":
            sub.add_argument("--all", action="store_true", help="include deployed/discarded Runs")
            runs_sub = sub.add_subparsers(dest="runs_command", metavar="<runs-command>")
            rm = runs_sub.add_parser(
                "rm", help="discard a Run: teardown + worktree + branch + CC session dir; Trace kept"
            )
            rm.add_argument("run_id", help="Run to discard")
        elif name == "deploy":
            sub.add_argument("ticket", nargs="?", help="ticket number (or stem) of a successful Run")
            sub.add_argument(
                "--check", action="store_true",
                help="fetch origin and report only: can the default branch fast-forward, and which"
                     " merged PRs left their ticket file behind — changes nothing",
            )
        elif name == "profiles":
            profiles_sub = sub.add_subparsers(
                dest="profiles_command", metavar="<profiles-command>", required=True
            )
            profiles_sub.add_parser(
                "get", help="full agents:/profiles: blocks + the merged roster per profile, as JSON"
            )
            profiles_sub.add_parser(
                "set", help="replace both blocks from {agents, profiles} JSON on stdin;"
                            " an invalid payload leaves the file untouched"
            )
            profiles_sub.add_parser(
                "merge", help="apply profile-model deltas from {changes} JSON on stdin;"
                               " returns {ok, wrote} or {ok: false, errors}"
            )
        elif name == "research":
            sub.add_argument("question", help="the question to research")
            sub.add_argument("--profile", help="resolve the roster under this profile")
        elif name == "scout":
            sub.add_argument("question", nargs="?", help="the repo question for the Scout Agent")
            sub.add_argument("--profile", help="resolve the roster under this profile")
            sub.add_argument("--path", action="store_true", help="print the packaged pi scout extension path")
        elif name == "prototype":
            sub.add_argument("slug", help="route dir slug — the route lives at proto-<slug>/")
            sub.add_argument(
                "ask", help="the design ask (start), feedback (--continue) or final call (--lock)"
            )
            mode = sub.add_mutually_exclusive_group()
            mode.add_argument(
                "--continue", dest="continue_", action="store_true",
                help="iterate on the same Session",
            )
            mode.add_argument(
                "--lock", action="store_true", help="lock the winning variant, write NOTES.md",
            )
            sub.add_argument(
                "--ref", action="append", default=[], metavar="PATH",
                help="reference file/image for the designer to study (repeatable)",
            )
            sub.add_argument(
                "--profile", help="resolve the roster under this profile (start call only)",
            )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "init":
        return _cmd_init(no_input=args.no_input)
    if args.command == "batch":
        return batch.watch(
            Path.cwd().resolve(), args.batch_id, deploy=args.deploy, profile=args.profile
        )
    if args.command == "run":
        if args.allow_overlap and (args.resume or len(args.ticket) > 1 or args.deploy):
            print(
                "lime run: --allow-overlap is a single fresh-ticket escape hatch only —"
                " it cannot be combined with --resume or a multi-ticket batch or --deploy",
                file=sys.stderr,
            )
            return 2
        if args.deploy and args.resume:
            print(
                "lime run: --deploy launches fresh tickets only — it cannot be combined"
                " with --resume",
                file=sys.stderr,
            )
            return 2
        if args.replan and not args.resume:
            print("lime run: --replan requires --resume RUN_ID", file=sys.stderr)
            return 1
        if args.rereview and not args.resume:
            print("lime run: --rereview requires --resume RUN_ID", file=sys.stderr)
            return 1
        if args.rereview and args.replan:
            print(
                "lime run: --rereview cannot be combined with --replan — each already"
                " decides what re-runs",
                file=sys.stderr,
            )
            return 1
        if args.resume:
            return _cmd_run_resume(args)
        if not args.ticket:
            print("lime run: a ticket number is required unless --resume is given", file=sys.stderr)
            return 1
        return _cmd_run(args)
    if args.command == "runs":
        if args.runs_command == "rm":
            return _cmd_runs_rm(args)
        return _cmd_runs(args)
    if args.command == "guard":
        return _cmd_guard()
    if args.command == "trace":
        return _cmd_trace(args)
    if args.command == "cost":
        return _cmd_cost(args)
    if args.command == "dash":
        if args.dash_command == "init":
            return home.init_command()
        if args.dash_command == "visit":
            return home.visit_command()
        return _cmd_dash(args)
    if args.command == "focus":
        if args.focus_command == "summarize":
            return summaries.command(args, _registry_path())
        return focus.command(args, _registry_path())
    if args.command == "status":
        return status_report.command(args, _registry_path())
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "ticket":
        if args.ticket_command in ("close", "reopen"):
            return _cmd_ticket_status(args)
        return _cmd_ticket_next()
    if args.command == "hook":
        return _cmd_hook_budget(args)
    if args.command == "deploy":
        if args.check:
            return _cmd_deploy_check()
        if not args.ticket:
            print("lime deploy: a ticket number is required unless --check is given", file=sys.stderr)
            return 1
        try:
            return _cmd_deploy(args)
        except _DeployProcessError as error:
            print(f"lime deploy: {error}", file=sys.stderr)
            return 1
    if args.command == "profiles":
        if args.profiles_command == "get":
            return _cmd_profiles_get()
        elif args.profiles_command == "set":
            return _cmd_profiles_set()
        return _cmd_profiles_merge()
    if args.command == "models":
        return _cmd_models()
    if args.command == "research":
        return _cmd_research(args)
    if args.command == "scout":
        return _cmd_scout(args)
    if args.command == "prototype":
        return _cmd_prototype(args)
    parser.exit(2, f"lime {args.command}: not implemented yet\n")


# --- init: the idempotent owner-split stamp (spec §13) ---

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SKILL_SRC = Path(__file__).resolve().parent / "skill" / "lime"
USER_DIRS = ("maps", "tickets", "specs", "prompts")
GITIGNORE_LINES = (".lime/runs/", "__pycache__/")


def _stamp_once(dest: Path, content: str) -> str:
    """User-owned: written when absent, never touched again."""
    if dest.exists():
        return "kept"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    return "stamped"


def _merged_settings(path: Path) -> dict:
    """The budget-hook entry, JSON-merged: other keys and foreign hooks kept,
    any stale `lime hook budget` entry replaced by the current one."""
    settings = json.loads(path.read_text()) if path.exists() else {}
    hooks = settings.setdefault("hooks", {})
    hooks["UserPromptSubmit"] = [
        entry for entry in hooks.get("UserPromptSubmit", [])
        if not any("lime hook budget" in h.get("command", "") for h in entry.get("hooks", []))
    ] + [{"hooks": [{"type": "command", "command": "lime hook budget"}]}]
    return settings


def _append_missing_lines(path: Path, lines: tuple[str, ...]) -> str:
    text = path.read_text() if path.exists() else ""
    present = {line.strip() for line in text.splitlines()}
    missing = [line for line in lines if line not in present]
    if not missing:
        return "kept"
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "".join(f"{line}\n" for line in missing))
    return "stamped"


def _subdirectory_warning(root: Path) -> list[str]:
    """Two lines when `root` sits below its git repository's root (a monorepo package):
    a Run carries the ticket into, and runs every agent and command from, the repository
    root (`worktree.create`, `git_helper.repo_root`), never from this subdirectory."""
    done = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-prefix", "--show-toplevel"],
                          capture_output=True, text=True)
    prefix, _, top = done.stdout.rstrip("\n").partition("\n")
    if done.returncode != 0 or not prefix:
        return []
    return [f"warning: this directory is {prefix.rstrip('/')} inside the git repository at {top},"
            " and a Run always works from the repository root.",
            f"  Run `lime init` in {top} instead."]


def _cmd_init(no_input: bool = False) -> int:
    root = Path.cwd().resolve()

    # Parse (and merge) settings.json before writing anything: an unreadable
    # file aborts the whole stamp rather than leaving half of it behind.
    settings_path = root / ".claude" / "settings.json"
    try:
        settings = _merged_settings(settings_path)
    except (json.JSONDecodeError, AttributeError, TypeError) as err:
        print(f"lime init: cannot merge {settings_path}: {err}", file=sys.stderr)
        return 1

    # User-owned: templates and dirs, created once, never touched again.
    owned: list[str] = []  # every path init owns, for the wizard's commit line

    def note(state: str, rel: str, label: str | None = None) -> str:
        owned.append(rel)
        print(state, f" {label or rel}")
        return state

    config_state = note(_stamp_once(root / ".lime" / "config.yaml", (TEMPLATES_DIR / "config.yaml").read_text()),
                        ".lime/config.yaml")
    note(_stamp_once(root / ".lime" / "trust.yaml", (TEMPLATES_DIR / "trust.yaml").read_text()),
         ".lime/trust.yaml")
    for name in USER_DIRS:
        d = root / ".lime" / name
        state = "kept" if d.exists() else "stamped"
        if state == "stamped":
            d.mkdir(parents=True)
            (d / ".gitkeep").touch()
        note(state, f".lime/{name}/.gitkeep", f".lime/{name}/")
    (root / ".lime" / "runs").mkdir(parents=True, exist_ok=True)
    note(_append_missing_lines(root / ".gitignore", GITIGNORE_LINES), ".gitignore")

    # Factory-owned: always rewritten, whatever is there.
    skill_dest = root / ".claude" / "skills" / "lime"
    if skill_dest.exists():
        shutil.rmtree(skill_dest)
    shutil.copytree(SKILL_SRC, skill_dest, ignore=shutil.ignore_patterns("__pycache__"))
    print("rewrote", " .claude/skills/lime/")
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    print("rewrote", " .claude/settings.json (budget hook entry)")
    owned += [".claude/skills/lime", ".claude/settings.json"]

    # Home side: worktree root + the project registry the dashboard reads.
    (Path.home() / ".lime" / "worktrees").mkdir(parents=True, exist_ok=True)
    registry = _registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    print(_append_missing_lines(registry, (str(root),)), f" {registry} entry")
    for line in _subdirectory_warning(root):
        print(line, file=sys.stderr)

    if no_input or not all(stream is not None and stream.isatty() for stream in (sys.stdin, sys.stdout)):
        if config_state == "stamped":
            print("Edit `commands:` and `deploy:` in .lime/config.yaml: the stamped values are for another stack.")
        return 0
    # 130 = interrupted (Ctrl-C or EOF at a question), the shell's code for SIGINT
    # this run's paths and an earlier init's alike: a rerun must not drop what is still uncommitted
    owned = [rel for rel in owned if (root / rel).exists()]
    return 0 if wizard.run(root, config_state == "stamped", input, owned=owned) else 130


# --- ticket next: print the next free ticket number ---


def _cmd_ticket_next() -> int:
    root = Path.cwd().resolve()
    if not git_helper.is_repo():
        print(
            "lime ticket next: not a git repository — numbers are derived from git",
            file=sys.stderr,
        )
        return 1
    try:
        print(numbering.next_number(root))
        return 0
    except RuntimeError as error:
        print(f"lime ticket next: {error}", file=sys.stderr)
        return 1


# --- ticket close / reopen ---


def _cmd_ticket_status(args) -> int:
    """Owner-only: set a ticket's header `Status:` line (close → closed, reopen → open)."""
    verb = args.ticket_command
    value = "closed" if verb == "close" else "open"
    label = f"lime ticket {verb}"

    # The tripwire runs first: before any registry or file access.
    if home.owner_only(label):
        return 2

    registry = _registry_path()
    project = next(
        (p for name, p in status_report.registered(registry) if name == args.project), None
    )
    if project is None:
        print(f'{label}: project "{args.project}" is not registered in {registry}', file=sys.stderr)
        return 2

    matches = tracker.by_number(project, args.number)
    if not matches:
        print(f"{label}: no ticket {args.number} in {project / '.lime' / 'tickets'}", file=sys.stderr)
        return 2
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        print(f"{label}: ticket {args.number} is ambiguous in {args.project}: {names}", file=sys.stderr)
        return 2

    ticket = matches[0]
    try:
        text = ticket.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"{label}: cannot read {ticket}: {error}", file=sys.stderr)
        return 2

    new = tracker.set_header_status(text, value)
    if new is None:
        print(f"{label}: {ticket} has no Status: line above its first ## heading", file=sys.stderr)
        return 2

    if new != text:
        tmp = ticket.with_name(ticket.name + ".tmp")
        try:
            tmp.write_bytes(new.encode("utf-8"))
            os.replace(tmp, ticket)
        except OSError as error:
            tmp.unlink(missing_ok=True)
            print(f"{label}: cannot write {ticket}: {error}", file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps({"project": args.project, "ticket": ticket.stem, "status": value}))
    else:
        print(f"{args.project} · {ticket.stem} · {value}")
    return 0


# --- run: launch a Run per ticket (spec §3, §8) ---

ROLES_FEATURE = ("scout", "planner", "builder", "reviewer", "documenter")  # the scout briefs the planner (ticket 71)


def _run_env(
    run_id: str, port: int | None, db_name: str | None, runs_dir: Path | None = None
) -> dict[str, str]:
    """The vars every subprocess a Run spawns needs (spec §7): agent/setup calls
    pick them up straight off `os.environ`; teardown gets them passed in.

    `LIME_RUNS_DIR` (only set once a Run has a `runs_dir` to give) is how a
    `lime research` call from inside the planner's bash tool — cwd is the
    worktree, which has no `.lime/runs/` of its own (gitignored, never
    checked out) — finds the project's real trace db to book onto instead of
    starting a disconnected one (spec §14).
    """
    env = {
        "LIME_RUN_ID": run_id,
        "LIME_PORT": str(port) if port is not None else "",
        "LIME_DB_NAME": db_name or "",
    }
    if runs_dir is not None:
        env["LIME_RUNS_DIR"] = str(runs_dir)
    return env


def _print_where_failures(failures: list[str]) -> None:
    """Shared read-only formatting of `tracker.where_failures` diagnostics."""
    for failure in failures:
        print(f"lime run: {failure}", file=sys.stderr)


def _cmd_run(args: argparse.Namespace) -> int:
    tickets = args.ticket
    deploy = getattr(args, "deploy", False)
    retry_of = getattr(args, "retry_of", None)
    if len(tickets) == 1 and not deploy and not retry_of:
        return _run_ticket(
            tickets[0], args.profile, allow_overlap=args.allow_overlap,
            batch_id=getattr(args, "batch_id", None),
        )
    return _run_batch(tickets, args.profile, deploy=deploy, retry_of=retry_of)


def _run_ticket(
    ticket_arg: str,
    profile: str | None,
    allow_overlap: bool = False,
    batch_id: str | None = None,
) -> int:
    project_root = Path.cwd().resolve()
    if clash := tracker.ambiguous(project_root, ticket_arg):
        names = ", ".join(p.name for p in clash)
        print(f"lime run: ticket {ticket_arg} is ambiguous in .lime/tickets/: {names}"
              f" — name one in full, e.g. `lime run {clash[0].stem}`", file=sys.stderr)
        return 2
    ticket_path = tracker.find(project_root, ticket_arg)
    if ticket_path is None:
        print(f"lime run: no ticket matching {ticket_arg!r} in .lime/tickets/", file=sys.stderr)
        return 1
    ticket = tracker.load(ticket_path)
    # Shared Where lint runs first — before blockers, overlap (even under
    # --allow-overlap), config/spawn checks, or number reservation — so a fault
    # refuses with zero launch effects.
    where_failures = tracker.where_failures(ticket, project_root)
    if where_failures:
        _print_where_failures(where_failures)
        return 2
    blocked = tracker.blocking(ticket, project_root)
    if blocked:
        print(f"lime run: {ticket.slug} is blocked by: {', '.join(blocked)}", file=sys.stderr)
        return 1

    with _launch_lock(project_root):
        if git_helper.is_repo():
            base = worktree.default_branch(project_root)
            try:
                local_only = git_helper.unpushed_commits(base, str(ticket_path))
            except RuntimeError as error:
                print(f"lime run: {error}", file=sys.stderr); return 1
            if local_only:
                rel = ticket_path.relative_to(project_root)
                print(
                    f"lime run: {rel} is committed on {base} but not on origin/{base}"
                    f" ({', '.join(local_only)}) —"
                    f" a Run commits the ticket on its own branch, and {base} could never"
                    f" fast-forward past a local copy of it (ticket 59).\n"
                    f"Fix: drop the commit and keep the file — `git reset --soft origin/{base}`"
                    f" when {base} is only ahead by ticket commits", file=sys.stderr)
                return 1

        # Overlap guard: refuse before reserving/creating anything when a live origin
        # Run branch touches the same Where paths. `--allow-overlap` skips only this.
        if not allow_overlap:
            try:
                conflicts = overlap.live_overlap(project_root, ticket)
            except overlap.OverlapError as error:
                print(f"lime run: {error}", file=sys.stderr)
                return 1
            if conflicts:
                for holder, paths in conflicts.items():
                    print(
                        f"lime run: {ticket.slug} overlaps {holder} on {', '.join(paths)}",
                        file=sys.stderr,
                    )
                print(
                    "lime run: wait for the overlapping Run(s) to merge or to be removed"
                    " (`lime runs rm <run_id>` discards that Run's work), or re-launch just"
                    " this ticket with --allow-overlap",
                    file=sys.stderr,
                )
                _record_refusal(project_root, ticket.slug, "overlap_refused", holders=conflicts)
                return 2

    raw = config.load(project_root)
    failures = config.validate_before_spawn(
        raw, list(ROLES_FEATURE), profile, root=project_root
    )
    if failures:
        for failure in failures:
            print(f"FAIL  {failure}", file=sys.stderr)
        return 1
    for warning in config.preflight_warnings(raw, list(ROLES_FEATURE), profile):
        print(f"warn  {warning}", file=sys.stderr)

    workflow = ticket.workflow or "feature"
    branch = worktree.branch_name(workflow, ticket.slug)
    base_branch = worktree.default_branch(project_root)
    run_id = new_id(8)
    try:
        with _launch_lock(project_root):
            numbering.reserve(project_root, ticket.number, ticket.slug, run_id)  # spec §8 birth step 0
            worktree_path = worktree.create(project_root, run_id, branch, ticket=ticket_path)
    except (numbering.NumberingError, worktree.WorktreeError) as error:
        print(f"lime run: {error}", file=sys.stderr)
        return 1

    runs_dir = (project_root / ".lime" / "runs").resolve()
    never = permissions.never_block(permissions.load_trust(project_root))
    port_range = config.port_range(raw)
    db_name = f"lime_{run_id}"
    # The trace db stays under the project's own .lime/runs/ (runs_dir, absolute);
    # every agent and git call must run from the worktree instead, and repo_root()
    # resolves off the process cwd (lime/git_helper.py) — so the chain needs cwd
    # inside the worktree for its whole life, restored no matter how it ends.
    os.chdir(worktree_path)
    try:
        # The Run's immutable birth identity: full HEAD in the NEW worktree, resolved
        # immediately after create/cwd change and before session.ensure()/setup. It is
        # normally the ticket commit; for the pre-existing identical ticket it is the
        # unchanged birth HEAD. No trailer search, branch-tip recomputation, or extra commit.
        birth_sha = git_helper.rev("HEAD")
        run = session.ensure(
            raw,
            run_id,
            workflow=workflow,
            request=ticket.ask,
            profile=profile,
            never=never,
            runs_dir=runs_dir,
            ticket=str(ticket_path),
            branch=branch,
            worktree=str(worktree_path),
            ports=port_range,
            db_name=db_name,
            base_sha=birth_sha,
            batch_id=batch_id,
        )
    except tracer.PortsExhausted as error:
        os.chdir(project_root)
        for problem in worktree.teardown(project_root, run_id, branch):
            print(f"lime run: {problem}", file=sys.stderr)
        print(f"lime run: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        os.chdir(project_root)
        print(f"lime run: {error}", file=sys.stderr)
        return 1
    try:
        # Every agent/setup/teardown subprocess this Run spawns inherits these
        # from os.environ (agent_pi/agent_cc copy it verbatim) — the one-process-
        # per-Run design (spec §7) means no other Run's env can collide with it.
        os.environ.update(_run_env(run_id, run.port, db_name, runs_dir))
        run.announce_roster(ROLES_FEATURE)
        return feature.run(run, ticket, base_branch)
    except Exception as error:
        print(f"lime run: {error}", file=sys.stderr)
        return 1
    finally:
        os.chdir(project_root)


LAUNCH_LOCK_REL = Path(".lime") / "runs" / "launch.lock"


@contextlib.contextmanager
def _launch_lock(project_root: Path):
    """Ticket 199 — one project-wide kernel lock so a launch's fetches and a
    `--deploy` batch's deploy never run at once (`cannot lock ref`).

    Blocking, no timeout. The flock is released when the holder exits, even on a
    crash, so there is never a stale lock to clean. Never nest it: a second fd in
    the same process would self-deadlock. The lock file is created on first use
    and never deleted.
    """
    path = project_root / LAUNCH_LOCK_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _spawn_run(cmd: list[str], cwd: Path) -> subprocess.Popen:
    """Spawn one child `lime run` process (spec §7).

    A narrow seam: tests that need real Git during scheduling (so they cannot patch
    `subprocess.Popen` globally) intercept only child launches here.
    """
    return subprocess.Popen(cmd, cwd=cwd)


def _record_refusal(
    project_root: Path, ticket: str | None, reason: str, *,
    run_id: str | None = None, **detail,
) -> None:
    """Best-effort ledger row for one refusal (ticket 216) — `lime status` reads it.
    A failed write never changes what the command prints or returns."""
    try:
        tr = tracer.Tracer((project_root / ".lime" / "runs").resolve())
    except (sqlite3.Error, OSError):
        return
    try:
        tr.refusal(ticket, reason, run_id=run_id, **detail)
    except (sqlite3.Error, OSError):
        pass
    finally:
        tr.con.close()


def _pick_ready(
    project_root: Path,
    pending: list[str],
    active_tickets: list[tracker.Ticket] | None = None,
    *,
    recorded: set[tuple[str, str]] | None = None,
    on_skip: Callable[[int], None] | None = None,
    on_hold: Callable[[int, str], None] | None = None,
) -> int | None:
    """Index of the first `pending` ticket that resolves, isn't blocked and has no
    live overlap with an unmerged Run branch (or an in-flight sibling's snapshot).

    Mutates `pending`: an arg matching no ticket file is reported and dropped
    (it will never resolve later) rather than retried forever. A held candidate
    (blocked or overlapping) is never popped — later checks may clear it.

    `active_tickets` are the parsed Where snapshots of children this process has
    already spawned but not yet reaped; a candidate overlapping one stays `pending`
    until that exact child is gone. Optional/backward-compatible: omitted (or empty)
    it behaves exactly as before.

    `recorded` dedupes the held-ticket refusal rows (ticket 216): one batch passes
    one set, so a held ticket records once per distinct holder map, not once per poll.

    `on_skip`/`on_hold` (ticket 254) are the batch's mirror hooks, called with the
    current index `i` right when a candidate is dropped or held — a no-file skip and
    a blocked/overlapping hold respectively. Both are optional; omitting them keeps
    the scheduling behaviour and the return value exactly as before.

    An `OverlapError` from the overlap check propagates; the batch treats it as
    transient and retries the pass (ticket 278).
    """
    active_tickets = list(active_tickets or [])
    i = 0
    while i < len(pending):
        path = tracker.find(project_root, pending[i])
        if path is None:
            print(f"lime run: no ticket matching {pending[i]!r} — skipping", file=sys.stderr)
            pending.pop(i)
            if on_skip is not None:
                on_skip(i)
            continue
        ticket = tracker.load(path)
        deps = tracker.blocking(ticket, project_root)
        if deps:
            if on_hold is not None:
                on_hold(i, batch.blocked_reason(deps))
            i += 1
            continue
        conflicts = overlap.live_overlap(project_root, ticket)
        if conflicts:
            key = (ticket.slug, json.dumps(conflicts, sort_keys=True))
            if recorded is None or key not in recorded:
                _record_refusal(project_root, ticket.slug, "overlap_held", holders=conflicts)
                if recorded is not None:
                    recorded.add(key)
            if on_hold is not None:
                on_hold(i, batch.overlap_reason(conflicts))
            i += 1
            continue
        clash = next(
            (
                active
                for active in active_tickets
                if overlap.conflicts_with(ticket, active, active.path, active.slug)
            ),
            None,
        )
        if clash is not None:
            if on_hold is not None:
                on_hold(i, batch.overlap_reason([clash.slug]))
            i += 1
            continue
        return i
    return None


def _report_stalled(project_root: Path, pending: list[str]) -> dict[str, str]:
    """Print why each pending ticket is not ready, once no children are active.

    Dependency-blocked tickets keep the existing `still blocked` diagnostic;
    overlapping ones are reported as `<ticket>: held: overlap with <Run/branch>`,
    one line per conflicting holder (paths may follow). A local holder names its
    Run id (`Run <run_id> (<branch>)`); a remote-only one keeps its branch key.

    Returns the blocker per held ticket (ticket argument → why) for the end report.
    """
    blockers: dict[str, str] = {}
    blocked: list[str] = []
    for ticket_arg in pending:
        path = tracker.find(project_root, ticket_arg)
        if path is None:
            continue
        ticket = tracker.load(path)
        deps = tracker.blocking(ticket, project_root)
        if deps:
            blocked.append(ticket_arg)
            blockers[ticket_arg] = ", ".join(deps)
            continue
        try:
            conflicts = overlap.live_overlap(project_root, ticket)
        except overlap.OverlapError as error:
            print(f"lime run: overlap check failed: {error}", file=sys.stderr)
            blockers[ticket_arg] = f"overlap check failed: {error}"
            continue
        for branch, paths in conflicts.items():
            detail = f" ({', '.join(paths)})" if paths else ""
            print(
                f"lime run: {ticket_arg}: held: overlap with {branch}{detail}",
                file=sys.stderr,
            )
        if conflicts:
            blockers[ticket_arg] = "; ".join(
                f"overlap with {branch}" for branch in conflicts
            )
    if blocked:
        print(
            f"lime run: still blocked, giving up on: {', '.join(blocked)}",
            file=sys.stderr,
        )
    return blockers


def _deploy_link(project_root: Path, ticket_arg: str) -> str | None:
    """Ticket 199 — carry one green child through `lime deploy` before the batch
    launches what it unblocks.

    Returns None only when the chain may advance; otherwise the command that picks
    the link up. The gates are exactly `lime deploy`'s because this *is* `_cmd_deploy`,
    unchanged: it never calls merge/ff/gate/session/status writers itself. Any refusal
    prints why, returns its pick-up command and leaves the ticket file in place so
    `_pick_ready` keeps the dependents held.

    Ticket 276: a deploy that returned 1 having recorded exactly one new
    `docs_merge_ci_pending` refusal for this Run waits once more for that CI (same
    ceiling, outside the launch lock) and deploys again; a second `pending` returns the
    pick-up command without deploying, and every other refusal holds as it always did.
    """
    resume = f"lime deploy {ticket_arg}"
    try:
        raw = config.load(project_root) or {}
    except Exception as error:
        print(
            f"lime run: {ticket_arg}: not deployed — cannot load project config"
            f" ({error}); its dependents stay held",
            file=sys.stderr,
        )
        return resume
    if raw.get("deploy"):
        print(
            f"lime run: {ticket_arg}: not deployed — deploy: lists commands and the"
            " Deploy session never runs unattended; run `lime deploy"
            f" {ticket_arg}` with the engineer present. Its dependents stay held",
            file=sys.stderr,
        )
        return resume
    ci = _await_ci(project_root, ticket_arg)
    if ci == "pending":
        print(
            f"lime run: {ticket_arg}: not deployed — CI still pending after"
            f" {DEPLOY_CI_CEILING_S // 60} min; its dependents stay held",
            file=sys.stderr,
        )
        return resume
    for again in (False, True):
        if again:
            print(
                f"lime run: {ticket_arg}: CI restarted on the docs merge lime pushed"
                " — waiting once more"
            )
            # Outside the lock: another `lime run` in this project must not wait on us.
            ci = _await_ci(project_root, ticket_arg, checks_expected=True)
            if ci == "pending":
                print(
                    f"lime run: {ticket_arg}: not deployed — CI on the pushed docs merge"
                    f" still pending after {DEPLOY_CI_CEILING_S // 60} min; its dependents"
                    " stay held",
                    file=sys.stderr,
                )
                return resume
        mark = _refusal_mark(project_root)  # read before the deploy, outside the lock
        print(f"lime run: deploying {ticket_arg}")
        crashed = False
        try:
            with _launch_lock(project_root):
                code = _cmd_deploy(argparse.Namespace(ticket=ticket_arg, check=False))
        except Exception as error:
            print(f"lime run: {ticket_arg}: lime deploy crashed — {error}", file=sys.stderr)
            code, crashed = 1, True
        if (
            code == 0 or again or crashed
            or not _docs_merge_ci_pending(project_root, ticket_arg, mark)
        ):
            break
    if code != 0:
        print(
            f"lime run: {ticket_arg}: not deployed (lime deploy exit {code});"
            " its dependents stay held",
            file=sys.stderr,
        )
        return resume
    leftover = tracker.find(project_root, ticket_arg)
    if leftover is not None:
        print(
            f"lime run: {ticket_arg}: merged on origin, but its ticket file is still in"
            " .lime/tickets/ (the local fast-forward was refused) — its dependents stay"
            " held; `git pull --ff-only` and remove that copy before launching them",
            file=sys.stderr,
        )
        return f"git pull --ff-only, then delete {leftover.relative_to(project_root)}"
    print(f"lime run: {ticket_arg} deployed")
    return None


def _child_resume(project_root: Path, ticket_arg: str, launched_at: datetime) -> str:
    """Pick-up command for a child that ended non-zero: resume its own Run row.

    The launch-time filter exists because `_resolve_deploy_run` returns the newest
    `kind='run'` row matching the ticket stem — an older Run of the same ticket (a
    previous attempt, or a stale row left by an earlier launch) must never be named
    as *this* child's resume target. A missing db, no matching row, or an unreadable
    trace db all fall back to a plain `lime run <ticket>`.
    """
    plain = f"lime run {ticket_arg}"
    db = project_root / ".lime" / "runs" / "lime.db"
    if not db.exists():
        return plain
    try:
        con = tracer.connect(db, readonly=True)
    except sqlite3.Error:
        return plain
    try:
        row = _resolve_deploy_run(con, ticket_arg)
    except sqlite3.Error:
        return plain
    finally:
        con.close()
    if row is None:
        return plain
    try:
        started = datetime.fromisoformat(row["started_at"])
    except (TypeError, ValueError):
        return plain
    if started >= launched_at:
        return f"lime run --resume {row['run_id']}"
    return plain


def _retry_fate(
    project_root: Path, dead_batch: str, ticket_arg: str, now: datetime
) -> tuple[str, sqlite3.Row | None]:
    """Read a dead batch's started child's Run and its take-over fate (ticket 256).

    Read-only and never mutating: `("queued", None)` when there is no db or the
    batch never started a Run for this ticket (spawn it as usual); `("overlap",
    row)` when `_resolve_deploy_run` names a DIFFERENT Run — the ticket was started
    by hand meanwhile; `(PASSED, row)` when the child passed and an earlier deploy
    row proves ticket 253 already merged it, even with the ticket file gone; a
    vanished ticket file is `("no_file", row)`; otherwise the child's own
    `batch.child_fate`. An unreadable ledger reads `("check_failed", None)`.
    """
    db = project_root / ".lime" / "runs" / "lime.db"
    if not db.exists():
        return ("queued", None)
    try:
        con = tracer.connect(db, readonly=True)
    except sqlite3.Error:
        return ("check_failed", None)
    try:
        row = batch.child_run(con, dead_batch, ticket_arg)
        if row is None:
            return ("queued", None)
        hand = _resolve_deploy_run(con, ticket_arg)
        if hand is not None and hand["run_id"] != row["run_id"]:
            return ("overlap", hand)
        fate = batch.child_fate(con, row, now)
        if fate == batch.PASSED and _earlier_deploy_row(con, row["run_id"]):
            return (batch.PASSED, row)
        if tracker.find(project_root, ticket_arg) is None:
            return ("no_file", row)
        return (fate, row)
    except sqlite3.Error:
        return ("check_failed", None)
    finally:
        con.close()


def _print_deploy_report(
    target: str, tickets: list[str], outcomes: dict[str, tuple[str, str]]
) -> None:
    """Ticket 200 — one stdout report per `--deploy` batch, once every child is reaped.

    Pure printing: every batch argument lands in exactly one bucket (deployed →
    failed → held, batch-argument order within a bucket). A repeated argument is
    reported once (its first occurrence wins); one the scheduler dropped with no
    outcome at all is held with its own reason.
    """
    ordered = list(dict.fromkeys(tickets))
    resolved = {
        t: outcomes.get(t, ("held", "no ticket file in .lime/tickets/"))
        for t in ordered
    }
    counts = {kind: 0 for kind in ("deployed", "failed", "held")}
    for kind, _detail in resolved.values():
        counts[kind] += 1
    print(
        f"lime run: --deploy report (target: {target}):"
        f" {counts['deployed']} deployed · {counts['failed']} failed"
        f" · {counts['held']} held"
    )
    for kind in ("deployed", "failed", "held"):
        for t in ordered:
            got, detail = resolved[t]
            if got != kind:
                continue
            if kind == "deployed":
                print(f"  deployed: {t}")
            elif kind == "failed":
                print(f"  failed: {t} — resume: {detail}")
            else:
                print(f"  held: {t} — blocker: {detail}")


# Ticket 278: a batch keeps a failing overlap check (e.g. a `cannot lock ref` fetch
# race with a sibling's or a deploy's fetch) pending this long, measured from the
# streak's first failure, before it drops the pending queue.
OVERLAP_CHECK_CEILING_S = 300


def _batch_clock() -> float:
    """Monotonic seconds for the overlap-failure streak — a module function so tests
    can patch it (never a real sleep)."""
    return time.monotonic()


def _run_batch(
    tickets: list[str], profile: str | None, deploy: bool = False, retry_of: str | None = None
) -> int:
    """One process, port-cap concurrent: fan the batch out to child `lime run`
    processes (own cwd/env each, spec §7) — up to `runs.ports`' width running at
    once, the next unblocked, non-overlapping ticket started as soon as one exits.

    A ticket held back by a live Run branch (origin or this project's own
    unpushed Runs) is never popped; the loop keeps reevaluating while other
    children run. The launched ticket's Where is snapshotted in parent memory
    before its child can change the root file, so a later candidate that overlaps
    an already-spawned child is held even before that child's trace row exists.
    A failed overlap check holds the queue and runs again each pass; only once it has
    kept failing for `OVERLAP_CHECK_CEILING_S` from the streak's first failure does
    spawning stop, active children drain, and the pending queue get dropped as
    `overlap check failed`, failing the batch.

    When `deploy` is set, each green child is carried through `lime deploy` so a
    blocker merges and unblocks its dependents (Step 5 wires the deploy itself).
    `retry_of` marks the watchdog's retry batch (ticket 255); the batch starts one
    watchdog beside itself (ticket 255). A retry first takes over its dead batch's
    `started` children by their Run's fate (ticket 256), so no ticket gets a second
    Run; a still-live child is adopted — it holds a port slot and is re-polled each
    tick until it ends, then takes the same fate path as any other started child.
    Only the still-`queued` children are launched as usual.
    The batch then ends with one stdout report naming the target and every
    argument's fate (deployed · failed with its resume command · held with its
    blocker).
    """
    project_root = Path.cwd().resolve()
    if deploy:
        target = config.auto_deploy(config.load(project_root) or {})
        if target is None:
            print(
                "lime run: --deploy refused — auto_deploy must be off, staging or prod"
                " in .lime/config.yaml",
                file=sys.stderr,
            )
            return 2
        if target == "off":
            print(
                "lime run: --deploy refused — auto_deploy is off in .lime/config.yaml"
                " (a missing key means off); set auto_deploy: staging or prod to let a"
                " batch merge unattended",
                file=sys.stderr,
            )
            return 2
        print(f"lime run: --deploy on — target {target}: each green link merges unattended")
    # Atomic preflight of every explicit argument: resolve/load and lint all of
    # them before any config/cap loading or scheduling. A faulty existing file
    # refuses the whole batch — the valid prefix is not launched first. Missing
    # args are left to the existing scheduler's handling, unchanged.
    where_failures: list[str] = []
    for ticket_arg in tickets:
        path = tracker.find(project_root, ticket_arg)
        if path is None:
            continue
        where_failures.extend(tracker.where_failures(tracker.load(path), project_root))
    if where_failures:
        _print_where_failures(where_failures)
        return 2
    cap = max(len(config.port_range(config.load(project_root))), 1)
    pending = list(tickets)
    # The batch's own ledger mirror (ticket 254). `positions[i]` is `pending[i]`'s
    # row: every pop from `pending` pops the same index here. Nothing reads it back.
    mirror = batch.Batch.open(project_root, tickets, retry_of=retry_of)
    if retry_of:
        print(f"lime run: retry of dead batch {retry_of}")
    mirror.start_watchdog(deploy=deploy, profile=profile)
    positions = list(range(len(tickets)))

    def skip(i):
        mirror.drop(positions.pop(i), batch.NO_TICKET_FILE)

    def hold(i, reason):
        mirror.hold(positions[i], reason)

    active: dict[subprocess.Popen, str] = {}
    # The parsed Where snapshot of each in-flight child, keyed by that exact child
    # object: closes the spawn-to-trace-row race without a second DB writer or lock.
    active_tickets: dict[subprocess.Popen, tracker.Ticket] = {}
    exit_codes: dict[str, int] = {}
    launched_at: dict[str, datetime] = {}
    outcomes: dict[str, tuple[str, str]] = {}
    # A child the retry resumes once (ticket 256): the ticket's resume command, and
    # the Run ids already resumed — a Run is never resumed a second time.
    resume_line: dict[str, str] = {}
    resumed: set[str] = set()
    # A still-live Run the retry takes over (ticket 256): run_id → (ticket, position).
    # It holds a port slot and is re-polled each tick until it ends, then takes the
    # same fate path as a started child.
    adopted: dict[str, tuple[str, int]] = {}
    # The last overlap-check error once the streak passed its ceiling (ticket 278);
    # it still guards the launch loop.
    fatal_discovery: str | None = None
    # The `_batch_clock()` reading at the streak's first failure, or None when no
    # streak is open (ticket 278).
    overlap_failing_since: float | None = None
    held_recorded: set[tuple[str, str]] = set()

    if retry_of:
        def take_over(ticket_arg, position, fate, row):
            run_id = row["run_id"] if row is not None else None
            if fate == "overlap":
                print(
                    f"lime run: {ticket_arg}: started by hand meanwhile as"
                    f" Run {run_id} — dropped"
                )
                mirror.drop(position, batch.overlap_reason([f"Run {run_id}"]))
                outcomes[ticket_arg] = ("held", f"overlap Run {run_id}")
                exit_codes[ticket_arg] = 1
            elif fate == "no_file":
                print(f"lime run: {ticket_arg}: no ticket file — dropped")
                mirror.drop(position, batch.NO_TICKET_FILE)
            elif fate == "check_failed":
                print(
                    f"lime run: {ticket_arg}: take-over check failed — dropped",
                    file=sys.stderr,
                )
                mirror.drop(position, "take-over check failed")
                outcomes[ticket_arg] = ("held", "take-over check failed")
                exit_codes[ticket_arg] = 1
            elif fate == batch.DEPLOYED:
                mirror.start(position)
                outcomes[ticket_arg] = ("deployed", "")
                exit_codes[ticket_arg] = 0
            elif fate == batch.PASSED:
                mirror.start(position)
                print(f"lime run: {ticket_arg}: Run {run_id} passed unwatched")
                if deploy:
                    resume = _deploy_link(project_root, ticket_arg)
                    exit_codes[ticket_arg] = 0 if resume is None else 1
                    outcomes[ticket_arg] = (
                        ("deployed", "") if resume is None else ("failed", resume)
                    )
                else:
                    exit_codes[ticket_arg] = 0
            elif fate == batch.KILLED and run_id not in resumed:
                # A child killed with the batch: resume it once, here, so no second
                # Run of the ticket is born. Not cap-checked — the dead batch held
                # at most cap children, so the widest it can be is cap.
                mirror.start(position)
                print(
                    f"lime run: resuming {ticket_arg} (Run {run_id}, killed with"
                    f" batch {retry_of})"
                )
                snapshot_path = tracker.find(project_root, ticket_arg)
                snapshot = tracker.load(snapshot_path) if snapshot_path else None
                proc = _spawn_run(
                    [sys.executable, "-m", "lime", "run", "--resume", run_id],
                    project_root,
                )
                active[proc] = ticket_arg
                if snapshot is not None:
                    active_tickets[proc] = snapshot
                resume_line[ticket_arg] = f"lime run --resume {run_id}"
                resumed.add(run_id)
            elif fate == batch.LIVE:
                # A child still running: adopt it — hold its port slot, re-poll it
                # each tick, and take its fate once it ends. No second Run is born.
                mirror.start(position)
                adopted[run_id] = (ticket_arg, position)
                print(
                    f"lime run: adopting {ticket_arg} (Run {run_id} still running)"
                )
            else:  # FAILED
                line = (
                    f"lime run --resume {run_id}"
                    if row["status"] in RESUMABLE_STATUSES
                    else f"lime run {ticket_arg}"
                )
                print(
                    f"lime run: {ticket_arg}: Run {run_id} failed on its own — left as is;"
                    f" resume: {line}"
                )
                mirror.drop(position, f"failed: Run {run_id}")
                outcomes[ticket_arg] = ("failed", line)
                exit_codes[ticket_arg] = 1

        keep_pending: list[str] = []
        keep_positions: list[int] = []
        for ticket_arg, position in zip(pending, positions):
            fate, row = _retry_fate(project_root, retry_of, ticket_arg, datetime.now(UTC))
            if fate == "queued":
                keep_pending.append(ticket_arg)
                keep_positions.append(position)
            else:
                take_over(ticket_arg, position, fate, row)
        pending[:] = keep_pending
        positions[:] = keep_positions

    while pending or active or adopted:
        while pending and len(active) + len(adopted) < cap and fatal_discovery is None:
            try:
                idx = _pick_ready(
                    project_root, pending, list(active_tickets.values()),
                    recorded=held_recorded, on_skip=skip, on_hold=hold,
                )
            except overlap.OverlapError as error:
                # Ticket 278: a failed overlap check is transient — keep the queue and
                # retry it each pass until the streak reaches its ceiling.
                now = _batch_clock()
                if overlap_failing_since is None:
                    overlap_failing_since = now
                    print(
                        f"lime run: overlap check failed: {error} — holding the queue;"
                        f" retrying each pass for up to {OVERLAP_CHECK_CEILING_S} s",
                        file=sys.stderr,
                    )
                if now - overlap_failing_since >= OVERLAP_CHECK_CEILING_S:
                    fatal_discovery = str(error)
                break
            # A `_pick_ready` call that returned — with an index or None — ends the
            # streak (ticket 278).
            overlap_failing_since = None
            if idx is None:
                break
            ticket_arg = pending.pop(idx)
            position = positions.pop(idx)
            # Snapshot *before* the child starts: it may edit/delete the root ticket,
            # and re-reading later would let overlapping work slip past the guard.
            snapshot_path = tracker.find(project_root, ticket_arg)
            snapshot = tracker.load(snapshot_path) if snapshot_path else None
            cmd = [sys.executable, "-m", "lime", "run", "--batch-id", mirror.batch_id, ticket_arg]
            if profile:
                cmd += ["--profile", profile]
            print(f"lime run: launching {ticket_arg}")
            launched_at[ticket_arg] = datetime.now(UTC)
            proc = _spawn_run(cmd, project_root)
            mirror.start(position)
            active[proc] = ticket_arg
            if snapshot is not None:
                active_tickets[proc] = snapshot
        if pending and len(active) + len(adopted) >= cap:
            mirror.wait_for_slot(positions)
        if not active and not adopted:
            if fatal_discovery is not None:
                if pending:
                    print(
                        f"lime run: overlap check failed for {OVERLAP_CHECK_CEILING_S} s,"
                        f" giving up on: {', '.join(pending)}"
                        f" — last error: {fatal_discovery}",
                        file=sys.stderr,
                    )
                    for t in pending:
                        outcomes[t] = (
                            "held",
                            f"overlap check failed: {fatal_discovery}",
                        )
                    exit_codes.update({t: 1 for t in pending})
                    for position in positions:
                        mirror.drop(position, batch.OVERLAP_CHECK_FAILED)
                    pending.clear()
                    positions.clear()
                break
            if overlap_failing_since is None:
                held = _report_stalled(project_root, pending)
                for t in pending:
                    outcomes[t] = (
                        "held",
                        held.get(t, "not ready when nothing else could run"),
                    )
                exit_codes.update({t: 1 for t in pending})
                for position in positions:
                    mirror.give_up(position)
                pending.clear()
                positions.clear()
                break
            # a failing overlap check still under its ceiling (ticket 278): keep the
            # queue, check again next pass
        mirror.flush()
        time.sleep(1)
        for proc in [p for p in active if p.poll() is not None]:
            ticket_arg = active.pop(proc)
            active_tickets.pop(proc, None)
            exit_codes[ticket_arg] = proc.returncode
            print(f"lime run: {ticket_arg} finished (exit {proc.returncode})")
            resumed_line = resume_line.get(ticket_arg)
            if proc.returncode != 0:
                if resumed_line is not None:
                    # A resume that failed again is reported with its own line and
                    # never retried (deploy or not).
                    print(
                        f"lime run: {ticket_arg}: resumed once and failed —"
                        f" resume: {resumed_line}"
                    )
                    outcomes[ticket_arg] = ("failed", resumed_line)
                elif deploy:
                    outcomes[ticket_arg] = (
                        "failed",
                        _child_resume(project_root, ticket_arg, launched_at[ticket_arg]),
                    )
            elif deploy:
                resume = _deploy_link(project_root, ticket_arg)
                exit_codes[ticket_arg] = 0 if resume is None else 1
                outcomes[ticket_arg] = (
                    ("deployed", "") if resume is None else ("failed", resume)
                )

        # Each tick after reaping, re-poll every adopted Run (ticket 256): a live or
        # unreadable one keeps waiting (retried next tick); any other fate ends the
        # adoption and takes the same path a started child would.
        for run_id, (adopted_ticket, position) in list(adopted.items()):
            fate, row = _retry_fate(
                project_root, retry_of, adopted_ticket, datetime.now(UTC)
            )
            if fate in (batch.LIVE, "check_failed"):
                continue
            print(
                f"lime run: {adopted_ticket}: adopted Run {run_id} ended ({fate})"
            )
            del adopted[run_id]
            take_over(adopted_ticket, position, fate, row)

    if deploy:
        _print_deploy_report(target, tickets, outcomes)
    mirror.close()
    return 1 if any(code != 0 for code in exit_codes.values()) else 0


# --- run --resume: restart the failed Phase in the same worktree/branch/profile (spec §9) ---

RESUMABLE_STATUSES = ("fail", "budget_exhausted")


def _resume_stale_plan(
    tr: "tracer.Tracer", run_id: str, ticket_path: str, expected_prompt: str
) -> str | None:
    """Why a replay-eligible plan cannot be reused, or None when it is unchanged.

    Every staleness check lives in `phases.stale_plan_reason`; this only gathers the
    cli-side inputs (the replay entry, the ticket bytes and the plan path). A run with
    no replay-eligible plan has nothing to be stale.
    """
    entry = tr.replay_map(run_id).get("plan")
    if entry is None:
        return None
    try:
        ticket_sha = hashlib.sha256(Path(ticket_path).read_bytes()).hexdigest()
    except OSError as error:
        return f"the ticket is unreadable: {error}"
    return phases.stale_plan_reason(
        tr,
        run_id,
        entry["phase_id"],
        ticket_sha256=ticket_sha,
        prompt=expected_prompt,
        plan_path=tr.run_dir(run_id) / "context_handoff" / "plan.md",
    )


def _expected_plan_prompt(raw: dict, profile: str | None, ticket) -> str:
    """Reconstruct the exact augmented planning input for a resume comparison.

    Read-only and offline: `tracker.prompt(ticket)` plus the bug-fix addendum when
    that workflow applies, then the shared builder-limits renderer resolved from the
    run's stored profile (never the default profile when the Run has a named one).
    Never creates a session and needs no credential preflight.
    """
    from lime import limits

    builder = next(a for a in config.roster(raw, profile) if a.name == "builder")
    prompt = tracker.prompt(ticket)
    if ticket.workflow == "bug-fix":
        from lime.workflows import bug_fix

        prompt = prompt + bug_fix.PLAN_ADDENDUM
    return limits.append_planner(prompt, builder, raw)


def _close_dead_run(tr, run_id: str) -> None:
    """Close a Run whose process died without closing it (ticket 256).

    Every still-`running` phase becomes `fail` with a kill note, then the Run
    itself becomes `fail` — which also closes its open process rows. Called only
    from `_cmd_run_resume`; the writers pin in tests/test_cli.py records both."""
    for phase in tr.con.execute(
        "SELECT phase_id FROM phases WHERE run_id=? AND status='running'", (run_id,)
    ).fetchall():
        tr.set_phase_status(
            phase["phase_id"],
            "fail",
            error="killed — its process died without closing the Run",
        )
    tr.set_run_status(run_id, "fail")


def _cmd_run_resume(args: argparse.Namespace) -> int:
    """Restart the failed Phase in the SAME worktree/branch/profile (spec §9): the
    chain re-enters from the top, but every Phase that already succeeded is replayed
    from the trace (`Run.replay`) instead of re-run, so execution truly resumes at
    the failure. Agent Sessions are fresh (the old ones are stale once the
    ticket/plan changed underneath them); re-scoping is on the human.
    A resume that would replay everything and do nothing is refused out loud
    rather than reported as a zero-token failure.

    `--rereview` reopens a Run whose phases all succeeded but whose review kept
    blocking: it invalidates from the last verify before the first review."""
    project_root = Path.cwd().resolve()
    run_id = args.resume
    runs_dir = (project_root / ".lime" / "runs").resolve()
    db = runs_dir / "lime.db"
    if not db.exists():
        print(f"lime run: no trace database at {db}", file=sys.stderr)
        return 1
    con = tracer.connect(db, readonly=True)
    row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        con.close()
        print(f"lime run: unknown run {run_id!r}", file=sys.stderr)
        return 1

    status = row["status"]
    # A `running` Run whose process is gone (ticket 256) cannot be resumed by the
    # ordinary path — its phase is stuck `running`, so `resume_blocked` refuses.
    # Detect it here, before the read-only connection closes.
    stale = status == "running" and batch.run_dead(con, run_id, datetime.now(UTC))
    tr = tracer.Tracer(runs_dir)
    con.close()

    if status not in RESUMABLE_STATUSES and not stale:
        print(
            f"lime run: {run_id} is {status!r} — only {' or '.join(RESUMABLE_STATUSES)}"
            " Runs can be resumed, or a dead running Run",
            file=sys.stderr,
        )
        return 1
    if not row["ticket"] or not Path(row["ticket"]).exists():
        print(f"lime run: {run_id}'s ticket is gone — nothing to resume", file=sys.stderr)
        return 1
    if not row["worktree"] or not Path(row["worktree"]).exists():
        print(f"lime run: {run_id}'s worktree is gone — discard it and start a new Run", file=sys.stderr)
        return 1
    if stale:
        # Past the ticket-gone and worktree-gone refusals, so those mutate nothing.
        # Close the dead Run as a failed one, then continue as an ordinary fail —
        # the agent map is kept, exactly as for a plain fail.
        _close_dead_run(tr, run_id)
        status = "fail"
        print(
            f"lime run: {run_id} was still running with no live process —"
            " closed it as failed"
        )
    if args.rereview:
        if "commit_code" in tr.replay_map(run_id):
            print(
                f"lime run: {run_id} already committed its code after an approved review"
                " — nothing to re-review; a plain --resume continues from the failed phase",
                file=sys.stderr,
            )
            return 1
        if tr.rereview_target(run_id) is None:
            print(
                f"lime run: {run_id} never reached review — --rereview needs a live review"
                " after a passing verify; use a plain --resume",
                file=sys.stderr,
            )
            return 1

    # Pinned birth identity, after the existence checks and before any
    # replan/status/session mutation. Re-read after Tracer construction so a
    # genuinely pre-column row is handled by its migrated keys; never read the card as
    # a competing authority and never infer a base from a NULL.
    stored_base_sha = tr.con.execute(
        "SELECT base_sha FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()["base_sha"]
    worktree_path = Path(row["worktree"])
    if stored_base_sha:
        try:
            git_helper.validate_run_base(stored_base_sha, repo_root=worktree_path)
        except git_helper.InvalidRunBase as error:
            print(f"lime run: {error}", file=sys.stderr)
            return 1

    ticket = tracker.load(row["ticket"])

    # Explicit stale-plan decision (ticket 132, 129): a replay-eligible plan whose
    # ticket, augmented planning prompt (resolved builder limits) or plan.md changed —
    # or whose provenance is missing/legacy — refuses an ordinary resume before any
    # status/phase/session/worktree mutation. `--replan` proceeds. Loaded and resolved
    # read-only, offline; the run's stored profile is used, never the default.
    raw = config.load(project_root)
    profile = row["profile"]
    expected_prompt = _expected_plan_prompt(raw, profile, ticket)
    stale = _resume_stale_plan(tr, run_id, row["ticket"], expected_prompt)
    if stale and not args.replan:
        print(
            f"lime run: {run_id} has a cached plan that cannot be reused — {stale}.",
            file=sys.stderr,
        )
        print(
            "  The ticket, planning inputs or resolved limits changed (or the old"
            f" evidence is unavailable). Re-plan explicitly with: lime run --resume {run_id} --replan",
            file=sys.stderr,
        )
        return 1

    failures = config.validate_before_spawn(raw, list(ROLES_FEATURE), profile, root=project_root)
    if failures:
        for failure in failures:
            print(f"FAIL  {failure}", file=sys.stderr)
        return 1
    for warning in config.preflight_warnings(raw, list(ROLES_FEATURE), profile):
        print(f"warn  {warning}", file=sys.stderr)

    base_branch = worktree.default_branch(project_root)
    never = permissions.never_block(permissions.load_trust(project_root))
    if row["port"] is not None:
        # The port went free the moment this Run failed — another live Run may
        # have since claimed it, so resuming into it blindly would collide.
        holder = tr.port_holder(row["port"], exclude_run_id=run_id)
        if holder is not None:
            print(
                f"lime run: port {row['port']} is now held by {holder} — free it"
                f" (lime runs rm {holder}) before resuming {run_id}",
                file=sys.stderr,
            )
            return 1
    if args.replan:
        # Explicit ordinary-resume decision: record it, then invalidate scout and
        # everything after it (scout input is ticket-specific), clear stale runtime
        # session mappings, and re-plan against the current ticket. Old plan/handoff/
        # worktree bytes are preserved until the planner deliberately replaces them.
        # Done before resume_blocked so the fresh invalidation clears the all-succeeded
        # dead end; it changes no run status by itself.
        tr.event(
            run_id, "log", name="replan_decision",
            payload={"reason": "engineer requested --replan", "engineer": engineer_name()},
        )
        dropped = tr.invalidate_from(run_id, "scout", reason="engineer requested --replan")
        if not dropped:
            dropped = tr.invalidate_from(run_id, "plan", reason="engineer requested --replan")
        (tr.run_dir(run_id) / "agent_map.json").unlink(missing_ok=True)
        if dropped:
            print(f"lime run: re-planning {run_id} — invalidated {', '.join(dropped)}")
    if args.rereview:
        # Explicit decision: record it and invalidate from the last verify before the
        # first review, clear stale session mappings; scout/plan/builds replay.
        dropped = tr.rereview(run_id, engineer=engineer_name())
        (tr.run_dir(run_id) / "agent_map.json").unlink(missing_ok=True)
        print(f"lime run: re-reviewing {run_id} — invalidated {', '.join(dropped)}")
    blocked = tr.resume_blocked(run_id)
    if blocked is not None:
        print(
            f"lime run: {run_id} has no phase left to re-run — {blocked}."
            " Nothing to resume; start a fresh run (lime run <ticket>).",
            file=sys.stderr,
        )
        return 1

    tr.set_run_status(run_id, "running")
    if status == "budget_exhausted":
        # A budget_exhausted Run resumed into its exhausted session would be re-killed at
        # once — stale Sessions, fresh start. A plain `fail` keeps the map: ticket 76's
        # early write means a phase that died mid-stream still left its runtime session id
        # behind, and re-entering that Phase should resume it rather than start over.
        (tr.run_dir(run_id) / "agent_map.json").unlink(missing_ok=True)

    worktree_path = Path(row["worktree"])
    os.chdir(worktree_path)
    try:
        run = session.ensure(
            raw,
            run_id,
            workflow=row["workflow"],
            request=ticket.ask,
            profile=profile,
            never=never,
            runs_dir=runs_dir,
            ticket=row["ticket"],
            branch=row["branch"],
            worktree=str(worktree_path),
            base_sha=stored_base_sha,
        )
        run.replay = run.tracer.replay_map(run_id)
        run.announce_roster(ROLES_FEATURE)
        # The port/db_name were already reserved by the original Run — reuse them,
        # same as the worktree/branch, rather than allocating fresh ones.
        os.environ.update(_run_env(run_id, row["port"], row["db_name"], runs_dir))
        return feature.run(run, ticket, base_branch)
    except Exception as error:
        print(f"lime run: {error}", file=sys.stderr)
        return 1
    finally:
        os.chdir(project_root)


# --- research: on-demand researcher Session (spec §14) ---

ROLES_RESEARCH = ("researcher",)
RESEARCH_GATE_RETRIES = 1  # bounded same-session corrections, same rule as the chain phases


def _research_phase(run, question: str) -> ResearchOutput:
    with run.phase(
        PhaseParams(
            name="research",
            kind="agent",
            stage="Prepare",
            owner="researcher",
            description="Answer the question from outside sources; change nothing.",
            retries=RESEARCH_GATE_RETRIES,
        )
    ) as ph:
        return ph.call(
            AgentCall(
                output_type=ResearchOutput,
                prompt=question,
                gates=[gates.artifacts_exist, gates.sources_cited],
            )
        )


def _on_demand(kind: str, role: str, question: str, profile: str | None, phase,
               runs_dir: Path | None = None):
    """One on-demand Role Session, never a chain step (spec §14). A caller inside
    a Run — the planner's bash tool inherits `LIME_RUN_ID` and `LIME_RUNS_DIR`
    (spec §7) — books the phase onto that parent Run, so a call from a worktree
    finds the project's real trace db instead of starting a disconnected row.
    Called bare — from the Orchestrator, cwd already the project — it gets its
    own `kind=<kind>` row. Returns `(envelope, exit_code)`; the envelope is
    None when the phase failed.

    When `runs_dir` is given explicitly (bare scout's throwaway directory), it
    overrides the default. The `LIME_RUN_ID`/`LIME_RUNS_DIR` branch remains
    authoritative for joined calls."""
    project_root = Path.cwd().resolve()
    raw = config.load(project_root)
    failures = config.validate_before_spawn(raw, [role], profile, chain_tests=False, root=project_root)
    if failures:
        for failure in failures:
            print(f"FAIL  {failure}", file=sys.stderr)
        return None, 1

    joined = bool(os.environ.get("LIME_RUN_ID"))
    if runs_dir is not None:
        pass  # explicit override — use it below
    elif joined and os.environ.get("LIME_RUNS_DIR"):
        runs_dir = Path(os.environ["LIME_RUNS_DIR"])
    else:
        runs_dir = (project_root / ".lime" / "runs").resolve()
    never = permissions.never_block(permissions.load_trust(project_root))
    run_id = os.environ["LIME_RUN_ID"] if joined else new_id(8)
    run = session.ensure(
        raw, run_id, workflow="", request=question, kind=kind, profile=profile, never=never, runs_dir=runs_dir
    )
    if joined:
        # The parent chain owns this row's whole lifecycle (status, its own
        # "kind=run" process registration) — this is a second, short-lived
        # process only booking a phase onto it, never settling or holding it
        # (spec §14). `session.ensure` always registers the calling process
        # under the run, so undo that registration immediately: left standing,
        # this process's pid becomes the LATEST one on the row, and once it
        # exits, `tracer.live_pid`/`_held_ports` would see a dead pid and free
        # the parent's port while the parent is still very much running.
        run.tracer.end_process(run_id, os.getpid())
        run._run_closed = True  # neutralize run.phase()'s close_run() on a failed phase below

    try:
        envelope = phase(run, question)
    except Exception as error:
        print(f"lime {kind}: {error}", file=sys.stderr)
        return None, 1
    return envelope, (0 if joined else run.finish(accepted=True))


def _cmd_research(args: argparse.Namespace) -> int:
    envelope, code = _on_demand("research", "researcher", args.question, args.profile, _research_phase)
    if envelope is None:
        return code
    brief = envelope.artifacts[0] if envelope.artifacts else "(no brief written)"
    print(f"lime research: {brief}")
    return code


# --- scout: on-demand scout Session for the Orchestrator's repo questions ---

ORCHESTRATOR_BRIEF_DESCRIPTION = "Answer the Orchestrator's repo question: paths and one-line notes, never file contents."
SCOUT_PRINT_LINES = 40  # a scout brief is ≤20 lines; the cap only guards against a runaway one


def _scout_phase(run, question: str):
    return phases.scout(run, question, description=ORCHESTRATOR_BRIEF_DESCRIPTION)


def _cmd_scout(args: argparse.Namespace) -> int:
    """`lime scout "<q>"` — the roster's scout Role answers a repo question so the
    Orchestrator never reads target source itself and never reaches for a
    harness-native subagent (ADR 0014). Prints the findings inline: they *are*
    the ≤20-line answer, and the Orchestrator has no business `cat`-ing a Run's
    handoff dir.

    `lime scout --path` prints the packaged pi extension path and returns.
    """
    if args.path:
        print(SCOUT_EXTENSION)
        return 0

    question = args.question
    if not question:
        print("lime scout: a question is required unless --path is given", file=sys.stderr)
        return 1

    joined = bool(os.environ.get("LIME_RUN_ID"))

    if joined:
        # Inside a Run — book the phase onto the parent row; no temp dir.
        envelope, code = _on_demand("scout", "scout", question, args.profile, _scout_phase)
        if envelope is None:
            return code
        findings = envelope.artifacts[0] if envelope.artifacts else "(no findings written)"
        print(f"lime scout: {findings}")
        try:
            lines = Path(findings).read_text().splitlines()
        except OSError:
            lines = [f"{f.file} — {f.note}" for f in envelope.findings]
        for line in lines[:SCOUT_PRINT_LINES]:
            print(line)
        if len(lines) > SCOUT_PRINT_LINES:
            print(f"… {len(lines) - SCOUT_PRINT_LINES} more lines in {findings}")
        return code

    # Bare scout: throwaway trace directory that never touches the project's .lime/runs.
    tmp_runs = Path(tempfile.mkdtemp(prefix="lime-scout-"))
    try:
        envelope, code = _on_demand("scout", "scout", question, args.profile, _scout_phase,
                                    runs_dir=tmp_runs)
        if envelope is None:
            return code
        findings = envelope.artifacts[0] if envelope.artifacts else "(no findings written)"
        print(f"lime scout: {findings}")
        try:
            lines = Path(findings).read_text().splitlines()
        except OSError:
            lines = [f"{f.file} — {f.note}" for f in envelope.findings]
        for line in lines[:SCOUT_PRINT_LINES]:
            print(line)
        if len(lines) > SCOUT_PRINT_LINES:
            print(f"… {len(lines) - SCOUT_PRINT_LINES} more lines in {findings}")
        return code
    finally:
        shutil.rmtree(tmp_runs, ignore_errors=True)


# --- prototype: the designer loop, Upstream, HITL, main working tree (spec §14) ---

ROLES_PROTOTYPE_START = ("scout", "designer")
ROLES_PROTOTYPE_RESUME = ("designer",)
PROTOTYPE_GATE_RETRIES = 1  # bounded same-session corrections, same rule as every other agent phase
# the loop is open exactly while the row sits in one of these (17-designer-role.md: "status
# running while the loop is open" plus the same fail/budget_exhausted resumability `lime run
# --resume` gives every other Run kind) — anything else is a closed loop: success (locked),
# deployed, discarded
PROTOTYPE_OPEN_STATUSES = ("running", "fail", "budget_exhausted")

SCOUT_PROTOTYPE_PROMPT = (
    "Brief a new prototype route before the designer builds it: where routes live in this"
    " project's router, the design tokens and fonts already in use, shared components worth"
    " reusing, and one example page that shows the project's conventions."
)


def _prototype_start_phase(run, slug: str, prompt: str, attachments: list[str]) -> DesignOutput:
    with run.phase(
        PhaseParams(
            name="design", kind="agent", stage="Build", owner="designer",
            description="Build 2-3 variants for the ask.", retries=PROTOTYPE_GATE_RETRIES,
        )
    ) as ph:
        return ph.call(
            AgentCall(
                output_type=DesignOutput, prompt=prompt, attachments=attachments,
                gates=[gates.route_dir_in_fence(slug), gates.variant_count],
            )
        )


def _prototype_continue_phase(run, slug: str, prompt: str, attachments: list[str]) -> DesignOutput:
    # no variant_count here — 17-designer-role.md's 2-3 count is an INITIAL-call gate; a
    # continue is expected to "change only the variant(s) named", not re-declare every one
    with run.phase(
        PhaseParams(
            name="design_continue", kind="agent", stage="Build", owner="designer",
            description="Iterate on the variants from feedback.", retries=PROTOTYPE_GATE_RETRIES,
        )
    ) as ph:
        return ph.call(
            AgentCall(
                output_type=DesignOutput, prompt=prompt, attachments=attachments,
                gates=[gates.route_dir_in_fence(slug)],
            )
        )


def _prototype_lock_phase(run, slug: str, prompt: str, attachments: list[str]) -> DesignOutput:
    with run.phase(
        PhaseParams(
            name="design_lock", kind="agent", stage="Review", owner="designer",
            description="Lock the winning variant and write NOTES.md.", retries=PROTOTYPE_GATE_RETRIES,
        )
    ) as ph:
        return ph.call(
            AgentCall(
                output_type=DesignOutput, prompt=prompt, attachments=attachments,
                gates=[gates.route_dir_in_fence(slug), gates.notes_locked],
            )
        )


def _prototype_start_prompt(slug: str, ask: str) -> str:
    return (
        f"{ask}\n\nBuild 2-3 substantively different variants at the route `proto-{slug}/`"
        f" (this project's router convention), reachable at `/proto-{slug}?variant=a|b|c`."
    )


def _prototype_continue_prompt(slug: str, feedback: str) -> str:
    return (
        f"Feedback on `proto-{slug}`: {feedback}\n\n"
        "Change only the variant(s) this names; leave the others exactly as they are."
    )


def _prototype_lock_prompt(slug: str, final_call: str) -> str:
    return (
        f"The engineer's final call on `proto-{slug}`: {final_call}\n\n"
        "Write NOTES.md in the route dir with three sections — fixed (what the final call"
        " decided), free (left to the builder), open (the planner must still settle) — then"
        " emit your Report JSON with notes_path set to that file."
    )


def _find_prototype_run(tr: tracer.Tracer, slug: str):
    return tr.con.execute(
        "SELECT * FROM runs WHERE kind='prototype' AND ticket=? ORDER BY started_at DESC LIMIT 1",
        (slug,),
    ).fetchone()


def _fence_designer_to_slug(run, slug: str) -> None:
    """Substitute `<slug>` in the designer's `writes:` glob — computable from the CLI slug
    before the route dir exists (17-designer-role.md); permissions.py enforces it unchanged.

    `writes: None` means unrestricted (permissions.py: "None = unrestricted, [] = no repo
    writes") — left alone. Only a concrete glob list ever needs the slug substituted in.
    """
    designer = run.agent("designer")
    if designer.writes is None:
        return
    run.agents["designer"] = designer.model_copy(
        update={"writes": [w.replace("<slug>", slug) for w in designer.writes]}
    )


def _cmd_prototype(args: argparse.Namespace) -> int:
    project_root = Path.cwd().resolve()
    slug = args.slug
    ask = resolve_prompt(args.ask)
    refs = [str(Path(r).resolve()) for r in args.ref]
    mode = "lock" if args.lock else "continue" if args.continue_ else "start"

    raw = config.load(project_root)
    never = permissions.never_block(permissions.load_trust(project_root))
    runs_dir = (project_root / ".lime" / "runs").resolve()
    tr = tracer.Tracer(runs_dir)
    existing = _find_prototype_run(tr, slug)
    is_open = existing is not None and existing["status"] in PROTOTYPE_OPEN_STATUSES

    if mode == "start":
        if is_open:
            print(
                f"lime prototype: {slug!r} already has an open prototype ({existing['run_id']},"
                f" {existing['status']}) — use --continue/--lock, or `lime runs rm` it first",
                file=sys.stderr,
            )
            return 1
        profile = args.profile
        roles = list(ROLES_PROTOTYPE_START)
    else:
        if not is_open:
            state = f" ({existing['status']!r})" if existing is not None else ""
            print(
                f"lime prototype: no open prototype {slug!r}{state} — start one with"
                f" `lime prototype {slug} \"<ask>\"`",
                file=sys.stderr,
            )
            return 1
        profile = existing["profile"]
        roles = list(ROLES_PROTOTYPE_RESUME)

    failures = config.validate_before_spawn(raw, roles, profile, chain_tests=False, root=project_root)
    if failures:
        for failure in failures:
            print(f"FAIL  {failure}", file=sys.stderr)
        return 1

    if mode == "start":
        run_id = new_id(8)
    else:
        run_id = existing["run_id"]
        if existing["status"] != "running":
            # same resumption rule `lime run --resume` gives a fail/budget_exhausted Run —
            # both are valid `running` transitions (tracer.RUN_TRANSITIONS)
            tr.set_run_status(run_id, "running")

    run = session.ensure(
        raw, run_id, workflow="", request=ask, kind="prototype", profile=profile,
        never=never, runs_dir=runs_dir, ticket=slug,
    )
    _fence_designer_to_slug(run, slug)

    try:
        if mode == "start":
            phases.scout(run, SCOUT_PROTOTYPE_PROMPT, description="Brief the designer on routes, tokens, components and conventions.")
            envelope = _prototype_start_phase(run, slug, _prototype_start_prompt(slug, ask), refs)
        elif mode == "continue":
            envelope = _prototype_continue_phase(
                run, slug, _prototype_continue_prompt(slug, ask), refs
            )
        else:
            envelope = _prototype_lock_phase(run, slug, _prototype_lock_prompt(slug, ask), refs)
    except Exception as error:
        print(f"lime prototype: {error}", file=sys.stderr)
        return 1

    if mode == "lock":
        return run.finish(accepted=True)

    # The loop stays open across separate `lime prototype` invocations (spec §14) — only
    # this process's registration ends; the runs row itself is left "running".
    run.tracer.end_process(run.run_id, os.getpid())
    variants = ", ".join(v.id for v in envelope.variants)
    print(f"lime prototype: {run.run_id}  {envelope.url_path}  variants: {variants}")
    return 0


# --- runs: list Runs / discard one (spec §3, §8) ---

# `needs_rescope` is listed only so Runs preserved before ticket 193 still show in
# `lime runs`; no code path sets this status any more.
RUNS_LIVE_STATUSES = ("running", "fail", "success", "budget_exhausted", "needs_rescope")


def _age(iso: str | None, now: datetime) -> str:
    """`34m` / `2h` — how long ago an ISO timestamp was; empty when there is none."""
    if not iso:
        return ""
    seconds = max(0, int((now - datetime.fromisoformat(iso)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _size_cell(con: sqlite3.Connection, run_id: str) -> str:
    """`120/1500` — the Run's last measured added lines over its budget; `-` when unmeasured."""
    size = phases.last_size(con, run_id)
    return "-" if size is None else f"{size[0]}/{size[1]}"


def _cmd_runs(args: argparse.Namespace) -> int:
    con = _open_db("runs")
    if con is None:
        return 1
    query = (
        "SELECT run_id, kind, workflow, ticket, status, port, branch, started_at,"
        " (SELECT max(started_at) FROM events WHERE events.run_id = runs.run_id) AS last_event_at"
        " FROM runs"
    )
    params: tuple = ()
    if not args.all:
        query += " WHERE status IN ({})".format(",".join("?" * len(RUNS_LIVE_STATUSES)))
        params = RUNS_LIVE_STATUSES
    query += " ORDER BY started_at DESC"
    rows = con.execute(query, params).fetchall()
    if not rows:
        print("no Runs" if args.all else "no live Runs (pass --all for deployed/discarded too)")
        return 0
    now = datetime.now(UTC)
    _table(
        ["run_id", "kind", "workflow", "ticket", "status", "port", "branch", "started", "size", "last"],
        [
            (r["run_id"], r["kind"], r["workflow"] or "",
             Path(r["ticket"]).name if r["ticket"] else "", r["status"], r["port"],
             r["branch"] or "", r["started_at"][:19], _size_cell(con, r["run_id"]),
             _age(r["last_event_at"], now))
            for r in rows
        ],
    )
    return 0


def _cmd_runs_rm(args: argparse.Namespace) -> int:
    project_root = Path.cwd().resolve()
    runs_dir = (project_root / ".lime" / "runs").resolve()
    db = runs_dir / "lime.db"
    if not db.exists():
        print(f"lime runs rm: no trace database at {db}", file=sys.stderr)
        return 1
    tr = tracer.Tracer(runs_dir)
    row = tr.con.execute("SELECT * FROM runs WHERE run_id=?", (args.run_id,)).fetchone()
    if row is None:
        print(f"lime runs rm: unknown run {args.run_id!r}", file=sys.stderr)
        return 1
    if row["status"] in ("deployed", "discarded"):
        print(f"lime runs rm: {args.run_id} is already {row['status']!r}", file=sys.stderr)
        return 1
    pid = tr.live_pid(args.run_id)
    if pid is not None:
        print(
            f"lime runs rm: {args.run_id} is still running (pid {pid}) — stop it first",
            file=sys.stderr,
        )
        return 1

    if row["kind"] == "prototype":
        # no worktree, branch or CC session dir ever existed for it (spec §14) — this
        # is the abandon door the asset promises, nothing more to tear down
        tr.set_run_status(args.run_id, "discarded")
        print(f"lime runs rm: {args.run_id} discarded — the route dir on disk is untouched")
        return 0

    raw = config.load(project_root)
    commands = raw.get("commands") or {}
    problems = worktree.teardown(
        project_root, args.run_id, row["branch"] or "",
        command=commands.get("teardown"),
        env=_run_env(args.run_id, row["port"], row["db_name"]),
    )
    for problem in problems:
        print(f"lime runs rm: {problem}", file=sys.stderr)
    tr.set_run_status(args.run_id, "discarded")
    if problems:
        print(f"lime runs rm: {args.run_id} discarded with problems above — check before trusting it", file=sys.stderr)
        return 1
    print(f"lime runs rm: {args.run_id} discarded — worktree, branch, CC session dir removed; Trace kept")
    return 0


# --- deploy: merge -> clean -> Deploy session (spec §10, §12) ---

# The Orchestrator launch line, reused verbatim for the Deploy session (spec §12) —
# `lime/skill/lime/SKILL.md` "Launch line" is the source of truth (ticket 35 owns it).
DEPLOY_MODEL = "claude-fable-5[1m]"
DEPLOY_DISALLOWED_TOOLS = (
    "Artifact,Workflow,ScheduleWakeup,SendFeedback,ReportFindings,CronCreate,CronDelete,"
    "CronList,DesignSync,EnterWorktree,ExitWorktree,Monitor,PushNotification,RemoteTrigger"
)

_CI_RED = git_helper.CI_RED
_CI_PENDING = git_helper.CI_PENDING

# Ticket 199's fixed CI ceiling for the `--deploy` batch: a bounded poll count, no clock read.
DEPLOY_CI_CEILING_S = 30 * 60
DEPLOY_CI_POLL_S = 30

# Ticket 275's bounded wait for a docs-only deploy merge to become the PR head: a fixed
# poll count, no clock read, exactly like `_await_ci`.
DOCS_MERGE_HEAD_WAIT_S = 120
DOCS_MERGE_POLL_S = 5

_ci_status = git_helper.ci_status


def _await_ci(
    project_root: Path, ticket_arg: str, *, checks_expected: bool = False
) -> str:
    """Bounded, read-only wait for a successful child's PR CI (ticket 199).

    Returns `_ci_status` as soon as it settles (green/red), `"pending"` when the fixed
    ceiling is reached, and `"unknown"` when there is no successful Run to watch or
    `gh pr view` fails. It never decides a deploy — `_cmd_deploy` re-checks PR state and
    CI itself — and it writes nothing.

    Ticket 276: with `checks_expected`, an empty `statusCheckRollup` reads as `"pending"`
    instead of `_ci_status([]) == "green"` — GitHub may not have registered checks on the
    just-pushed head yet, and an empty rollup must never merge it.
    """
    db = project_root / ".lime" / "runs" / "lime.db"
    if not db.exists():
        return "unknown"
    con = tracer.connect(db, readonly=True)
    try:
        row = _resolve_deploy_run(con, ticket_arg)
    finally:
        con.close()
    if row is None or row["status"] != "success":
        return "unknown"
    branch = row["branch"]
    print(
        f"lime run: {ticket_arg}: waiting for CI on {branch}"
        f" (up to {DEPLOY_CI_CEILING_S // 60} min)"
    )
    polls = max(DEPLOY_CI_CEILING_S // DEPLOY_CI_POLL_S, 1)
    for attempt in range(polls + 1):
        try:
            pr = git_helper.pr_view(branch)
        except RuntimeError:
            return "unknown"
        checks = pr.get("statusCheckRollup") or []
        status = "pending" if not checks and checks_expected else _ci_status(checks)
        if status != "pending" or attempt == polls:
            return status
        time.sleep(DEPLOY_CI_POLL_S)
    return "unknown"  # unreachable: the loop always returns


def _refusal_mark(project_root: Path) -> int | None:
    """The newest `events.event_id` before a deploy, so a later read sees only its rows
    (ticket 276). Read-only: a missing db or an unreadable ledger is `None`."""
    db = project_root / ".lime" / "runs" / "lime.db"
    if not db.exists():
        return None
    try:
        con = tracer.connect(db, readonly=True)
    except sqlite3.Error:
        return None
    try:
        row = con.execute("SELECT COALESCE(MAX(event_id), 0) FROM events").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return int(row[0]) if row is not None else None


def _docs_merge_ci_pending(
    project_root: Path, ticket_arg: str, mark: int | None
) -> bool:
    """Whether the deploy that just ran recorded exactly one new refusal for this Run and
    it was `docs_merge_ci_pending` (ticket 276).

    Filtering by the Run's `run_id` ignores concurrent children's rows, and
    `event_id > mark` ignores stale rows from earlier deploys. It never writes.
    """
    if mark is None:
        return False
    db = project_root / ".lime" / "runs" / "lime.db"
    if not db.exists():
        return False
    try:
        con = tracer.connect(db, readonly=True)
    except sqlite3.Error:
        return False
    try:
        row = _resolve_deploy_run(con, ticket_arg)
        if row is None:
            return False
        names = [
            refusal_row["name"]
            for refusal_row in con.execute(
                "SELECT name FROM events WHERE type='refusal' AND run_id=?"
                " AND event_id>? ORDER BY event_id",
                (row["run_id"], mark),
            ).fetchall()
        ]
    except sqlite3.Error:
        return False
    finally:
        con.close()
    return names == ["docs_merge_ci_pending"]


def _deploy_ticket_stem(ticket_path: str | None) -> str | None:
    return Path(ticket_path).stem if ticket_path else None


TICKETS_REL = Path(".lime") / "tickets"


def _pr_ticket_stem(head_ref: str) -> str | None:
    """`lime/<workflow>/<stem>` -> stem; anything else (e.g. `lime/install`) -> None."""
    parts = head_ref.split("/", 2)
    return parts[2] if len(parts) == 3 and parts[0] == "lime" else None


def _stale_tickets(project_root: Path, merged: list[dict]) -> list[tuple[Path, int]]:
    """Ticket files still on disk whose stem is the head branch of a merged PR —
    delete-on-close should have taken them with the merge (ticket 61).
    Returns (path relative to project_root, PR number), sorted by path."""
    by_stem = {}
    for pr in merged:
        stem = _pr_ticket_stem(pr.get("headRefName") or "")
        if stem and stem not in by_stem:
            by_stem[stem] = int(pr["number"])
    found = []
    for path in sorted((project_root / TICKETS_REL).glob("*.md")):
        if path.stem in by_stem:
            found.append((path.relative_to(project_root), by_stem[path.stem]))
    return found


def _stale_line(path: Path, number: int) -> str:
    return f"stale ticket: {path} (PR #{number} merged)"


class _DeployCheckRun:
    """Just enough of a Run for `quality.run_check`.

    Its repo_root is the throwaway merged worktree, its commands are the loaded config
    commands, it has no phases (so `_run` logs under a `00_<name>` dir), and it reuses
    the Run's own tracer and a Console bound to it. No Run status/phase state is touched.

    `command_env`, when set, is the full environment its subprocesses get: the deploy
    check's own resource identity, distinct from the deploying Run's. `None` (every
    ordinary Run) keeps `quality._run`'s original `operator_env()` behavior.
    """

    def __init__(
        self, *, repo_root, commands, context_handoff_dir, run_id, tracer, console,
        command_env=None,
    ):
        self.repo_root = repo_root
        self.commands = commands
        self.phases: list = []
        self.context_handoff_dir = context_handoff_dir
        self.run_id = run_id
        self.tracer = tracer
        self.console = console
        self.command_env = command_env


def _probe_free_port() -> int:
    """An available ephemeral port on loopback; the probe closes before returning."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _allocate_check_port(*, excluded, candidate=None) -> int | None:
    """A loopback port for the merged-tree check, never one already in use.

    `excluded` is the deploying Run's recorded port, every currently held Run port and
    the whole configured `runs.ports` pool, so the check cannot steal a slot from a Run
    launched while it runs. Bounded retry; `None` on probe error or exhaustion — the
    caller refuses rather than falling back to a fixed, empty or parent port. The probe
    closes immediately; it is a bind handoff, not an OS-wide lease, so the project may
    bind the port for setup/test.
    """
    for _ in range(100):
        try:
            port = candidate() if candidate is not None else _probe_free_port()
        except OSError:
            return None
        if port not in excluded:
            return port
    return None


def _deploy_git_read(project_root: Path, *args: str) -> subprocess.CompletedProcess | None:
    """One captured read-only `git -C project_root ...`, or None on an OS execution error.

    The argument vector is never shell-interpolated and never touches the checkout. A
    nonzero status is returned for the caller to judge, not raised; `KeyboardInterrupt`
    and `SystemExit` propagate untouched.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(project_root), *args], capture_output=True, text=True
        )
    except OSError:
        return None


def _deploy_launch_message(body: str, expected_subject: str, run_id: str) -> bool:
    """Exact launch format only: `ticket: <stem>` plus the two orchestrator trailers."""
    lines = body.splitlines()
    if not lines or lines[0].strip() != expected_subject:
        return False
    trailers = [line.strip() for line in lines[1:] if line.strip()]
    return trailers == [f"Lime-Run: {run_id}", "Lime-Role: orchestrator"]


def _deploy_launch_only_range(
    project_root: Path, base_sha: str, default_sha: str, stem: str, run_id: str,
) -> bool:
    """Prove every commit in `default_sha..base_sha` is this Run's own launch commit.

    The range must be nonempty and linear (each commit exactly one parent). Each commit
    is inspected against its own parent — subject, trailers and a changed-path set
    restricted to `.lime/tickets/<stem>.md` — so unrelated edits that a later commit
    reverted cannot slip through. A merge, ordinary work or another Run's ticket commit
    fails the proof. Paths come back NUL-separated with renames disabled.
    """
    listing = _deploy_git_read(
        project_root, "rev-list", "--parents", f"{default_sha}..{base_sha}"
    )
    if listing is None or listing.returncode != 0:
        return False
    entries = [line.split() for line in listing.stdout.splitlines() if line.strip()]
    if not entries:
        return False
    expected_subject = f"ticket: {stem}"
    expected_path = f".lime/tickets/{stem}.md"
    for entry in entries:
        if len(entry) != 2:  # one commit and exactly one parent — no merge, no root
            return False
        commit, parent = entry
        message = _deploy_git_read(project_root, "log", "-1", "--format=%B", commit)
        if message is None or message.returncode != 0:
            return False
        if not _deploy_launch_message(message.stdout, expected_subject, run_id):
            return False
        changed = _deploy_git_read(
            project_root, "diff", "--name-only", "--no-renames", "-z", parent, commit
        )
        if changed is None or changed.returncode != 0:
            return False
        paths = [path for path in changed.stdout.split("\0") if path]
        if paths != [expected_path]:
            return False
    return True


def _deploy_base_unmoved(
    project_root: Path, run_id: str, base_sha: str | None, default_sha: str, branch: str,
) -> bool:
    """Conservative, read-only proof that the Run was born at the default tip.

    Exact equality is the existing safe fast path, including the birth case where the
    identical ticket was already tracked so `worktree.create` made no launch commit. A
    NULL pin is never unmoved. Otherwise the fetched default tip must be an ancestor of
    the pin and every commit in `default_sha..base_sha` must be one of this Run's own
    launch commits (`ticket: <stem>`, `Lime-Run: <run_id>`, `Lime-Role: orchestrator`,
    touching only `.lime/tickets/<stem>.md`).

    Anything else — a non-ancestor, a merge, ordinary work, another Run's ticket commit,
    a missing/unreadable object or malformed metadata — returns False so the caller runs
    the full merged-tree gate: never skip on doubt. Reads only; no ref, checkout, row or
    cache is ever written, and interrupts propagate.
    """
    if not base_sha:
        return False
    if base_sha == default_sha:
        return True
    stem = _pr_ticket_stem(branch)
    if not stem:
        return False
    ancestor = _deploy_git_read(
        project_root, "merge-base", "--is-ancestor", default_sha, base_sha
    )
    if ancestor is None or ancestor.returncode != 0:
        return False
    return _deploy_launch_only_range(project_root, base_sha, default_sha, stem, run_id)


def _documenter_writes(raw: dict, profile: str | None) -> tuple[str, ...]:
    """The `documenter` Role's `writes:` list, resolved through `profile` (ticket 275).

    An unknown profile (`ValueError`), no documenter, or a `writes:` that is not a list
    all own nothing — the merged-tree gate then refuses a conflict exactly as before.
    """
    try:
        agents = config.merged_agents(raw, profile)
    except ValueError:
        return ()
    for agent in agents:
        if agent.get("name") == "documenter":
            writes = agent.get("writes")
            if isinstance(writes, list):
                return tuple(w for w in writes if isinstance(w, str))
            return ()
    return ()


def _check_deploy_merged_tree(
    project_root: Path, run_id: str, base_sha: str | None, branch: str, commands: dict,
    *, excluded_ports: tuple[int, ...] = (), ticket: str | None = None,
    docs_writes: tuple[str, ...] = (), had_checks: bool = True,
    merged_head: list[str] | None = None,
) -> int:
    """Pre-merge gate: run setup+test against the actual merged tree in a throwaway worktree.

    Invoked after the OPEN/CI-green refusals and before any merge, fast-forward, teardown
    or interactive session. Fetches origin and resolves the default tip; when the stored
    pin is proven unmoved by `_deploy_base_unmoved` (exact equality with the tip, or the
    tip is an ancestor of the pin and only this Run's launch commits intervene) it prints
    `lime deploy: base unmoved` and returns without creating a worktree or running
    commands. Every other case (including a NULL legacy pin, an unrecognized history or
    any inspection failure) tests the real merged tree — never infer/backfill a birth base
    or skip on doubt.

    The check runs under its own subprocess resource identity (a fresh uuid's port and
    `lime_<id>` database), never the deploying Run's, so it can neither collide with a
    live Run nor leave a lasting Run row. After setup/test it runs the configured teardown
    once, through the same frozen environment, before the worktree is removed.

    The one exception to a conflict refusal (ticket 275): a conflict only in paths the
    documenter's `writes:` lists (`docs_writes`), where `docs_merge.resolve` turns both
    sides' additions into a merged text, is resolved and tested here. On green the tested
    tree becomes a real merge commit (an unreferenced object; the local Run branch and
    worktree stay untouched) and `_publish_docs_merge` lease-pushes it onto the PR — the
    gate's only remote write — then appends it to `merged_head` for `_cmd_deploy` to merge
    at that exact head. A refusal from any of that (or a resolver that cannot merge, or a
    non-docs conflict) records `merge_conflict` and pushes nothing.

    Returns 0 on green (context already cleaned up) and 1 with a printed refusal on any
    config, git, port, setup or test failure. Both original checkouts and every ref are
    left untouched except the lease push named above. `ticket` names the refusal events
    (`merged_tree_failed`, `merge_conflict`) this refusal records for `lime status`
    (ticket 216).
    """
    try:
        git_helper.fetch_origin(project_root)
        default = worktree.default_branch(project_root)
        default_sha = git_helper.resolve_remote_tip(
            project_root, f"origin/{default}", label="default branch"
        )
    except RuntimeError as error:
        print(f"lime deploy: cannot resolve the base tip — {error}", file=sys.stderr)
        return 1

    if _deploy_base_unmoved(project_root, run_id, base_sha, default_sha, branch):
        print("lime deploy: base unmoved")
        return 0

    if not (commands or {}).get("test"):
        print(
            "lime deploy: commands.test is unset — cannot verify the merged tree; refusing",
            file=sys.stderr,
        )
        return 1
    if (bad := quality.test_timeout_failure(commands)):
        print(f"lime deploy: {bad} — refusing", file=sys.stderr)
        return 1
    try:
        pr_sha = git_helper.resolve_remote_tip(project_root, f"origin/{branch}", label="PR branch")
    except RuntimeError as error:
        print(f"lime deploy: cannot resolve the PR head — {error}", file=sys.stderr)
        return 1

    # Logs live under the Run's own context_handoff, keyed by a fresh uuid so they never
    # overwrite original verify evidence or another deployment attempt, and survive the
    # worktree's deletion.
    log_dir = (
        project_root / ".lime" / "runs" / run_id / "context_handoff"
        / "deploy-check" / str(uuid.uuid4())
    )
    tr = tracer.Tracer(project_root / ".lime" / "runs")
    console = Console(tr, run_id)
    parent = tr.con.execute(
        "SELECT port FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    parent_port = parent["port"] if parent is not None else None
    excluded = set(excluded_ports) | set(tr._held_ports())
    if parent_port is not None:
        excluded.add(parent_port)

    # The docs-only exception (ticket 275): a resolver that only ever merges a conflict
    # `docs_merge` can, and only for paths the documenter's `writes:` owns. `resolved`
    # records which paths it actually merged, so the commit message and the printed
    # refusal name them.
    resolved: list[str] = []
    staged: str | None = None
    merge_sha: str | None = None

    def resolve(path: str, text: str) -> str | None:
        if not permissions.owned(path, docs_writes):
            return None
        result = docs_merge.resolve(text)
        if result is not None:
            resolved.append(path)
        return result

    try:
        with git_helper.merged_worktree(
            project_root, default_sha, pr_sha, resolve=resolve if docs_writes else None
        ) as tree:
            # Right after entering: if the merge resolved a docs-only conflict, freeze the
            # exact tree setup/test will run on so a green run can commit it verbatim.
            if resolved:
                staged = git_helper.staged_tree(tree)
                print(
                    "lime deploy: resolved a docs-only conflict in"
                    f" {', '.join(resolved)} — testing the merged tree"
                )
            # Allocate only after preflight and after entering the merged tree: a git or
            # config refusal must not have probed or held a port it never used.
            check_id = uuid.uuid4().hex
            check_port = _allocate_check_port(excluded=excluded)
            if check_port is None:
                print(
                    "lime deploy: could not allocate an independent port for the"
                    " merged-tree check — refusing",
                    file=sys.stderr,
                )
                return 1
            check_db_name = f"lime_{check_id}"
            command_env = {
                **operator_env(), **_run_env(check_id, check_port, check_db_name)
            }
            adapter = _DeployCheckRun(
                repo_root=str(tree), commands=commands, context_handoff_dir=log_dir,
                run_id=run_id, tracer=tr, console=console, command_env=command_env,
            )
            try:
                for name in ("setup", "test"):
                    if name == "setup" and not (commands or {}).get("setup"):
                        continue  # setup is optional when unset, exactly as elsewhere
                    result = quality.run_check(adapter, name)
                    if result is not None and not result.passed:
                        print(
                            f"lime deploy: merged-tree {name} failed — `{result.command}`"
                            f" exited {result.returncode}\n{result.output_tail}\n"
                            f"  output: {result.output_artifact}",
                            file=sys.stderr,
                        )
                        _record_refusal(
                            project_root, ticket, "merged_tree_failed",
                            run_id=run_id, check=name, returncode=result.returncode,
                        )
                        return 1
            finally:
                # Teardown runs before removal, exactly once, in the same frozen env and
                # cwd even when setup/test failed, timed out or raised. No configured
                # teardown means no subprocess, warning or quality artifact.
                if (commands or {}).get("teardown"):
                    try:
                        result = quality.run_check(adapter, "teardown")
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as error:
                        print(
                            f"lime deploy: merged-tree teardown raised — LIME_DB_NAME="
                            f"{check_db_name}: {error}",
                            file=sys.stderr,
                        )
                    else:
                        if result is not None and not result.passed:
                            print(
                                "lime deploy: merged-tree teardown failed —"
                                f" LIME_DB_NAME={check_db_name} `{result.command}` exited"
                                f" {result.returncode}\n{result.output_tail}\n"
                                f"  output: {result.output_artifact}",
                                file=sys.stderr,
                            )
            # Only after setup/test+teardown came back green do we turn the tested tree
            # into a real (unreferenced) merge commit — its failure is a plain git refusal
            # that the outer except below handles, with nothing pushed.
            if staged:
                merge_sha = git_helper.commit_merge(
                    project_root, staged, pr_sha, default_sha,
                    f"Merge origin/{default} into {branch}\n\n"
                    f"lime deploy resolved a docs-only conflict in: {', '.join(resolved)}\n\n"
                    f"Lime-Run: {run_id}",
                )
    except RuntimeError as error:
        print(f"lime deploy: merged-tree gate refused — {error}", file=sys.stderr)
        if isinstance(error, git_helper.MergeConflict):
            _record_refusal(
                project_root, ticket, "merge_conflict", run_id=run_id, paths=error.paths
            )
        return 1

    # The worktree is cleaned up now (its `with` exited above), so the only remote write
    # left — the lease push onto the PR (ticket 275) — is safe to make here.
    if merge_sha:
        if not _publish_docs_merge(
            project_root, run_id, branch, pr_sha, merge_sha,
            had_checks=had_checks, ticket=ticket,
        ):
            return 1
        if merged_head is not None:
            merged_head.append(merge_sha)
    return 0


def _publish_docs_merge(
    project_root: Path, run_id: str, branch: str, pr_sha: str, merge_sha: str,
    *, had_checks: bool, ticket: str | None,
) -> bool:
    """Lease-push a tested docs-only merge onto the PR, wait for the head, gate its CI
    (ticket 275).

    `merge_sha` is the merge commit the merged-tree gate already built and tested as an
    unreferenced object. `--force-with-lease` on `pr_sha` means the push is refused
    whenever the PR branch moved under us (a refusal records `docs_merge_push_rejected`
    and nothing else runs). From then on this only reads: it waits — bounded by a single
    wall-clock deadline of `DOCS_MERGE_HEAD_WAIT_S`, so time spent inside a `gh` call
    counts, each `pr_view` timed to whatever remains, with a `gh` error counting as "not
    yet shown" — for `headRefOid` to become `merge_sha`, then reads CI once on that same
    head. A head that never shows records `docs_merge_head_missing`; a non-green CI
    records `docs_merge_ci_<ci>`. It never touches the local Run branch, worktree, merge,
    teardown or a Run row. Returns True only when the pushed docs merge is the head and
    its CI is green.
    """
    try:
        git_helper.push_lease(project_root, merge_sha, branch, pr_sha)
    except RuntimeError as error:
        print(f"lime deploy: docs merge push refused — {error}", file=sys.stderr)
        _record_refusal(
            project_root, ticket, "docs_merge_push_rejected", run_id=run_id, sha=merge_sha
        )
        return False

    deadline = time.monotonic() + DOCS_MERGE_HEAD_WAIT_S
    pr: dict | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            # A hung `gh` call is bounded by whatever remains of the two-minute window,
            # so slow or stuck calls can never push the wait past its deadline.
            pr = git_helper.pr_view(branch, timeout=remaining)
        except RuntimeError:
            pr = None
        if pr is not None and pr.get("headRefOid") == merge_sha:
            break
        time.sleep(max(0.0, min(DOCS_MERGE_POLL_S, deadline - time.monotonic())))
    if pr is None or pr.get("headRefOid") != merge_sha:
        print(
            f"lime deploy: docs merge {merge_sha[:12]} is not the head of {branch}"
            " — refusing",
            file=sys.stderr,
        )
        _record_refusal(
            project_root, ticket, "docs_merge_head_missing", run_id=run_id, sha=merge_sha
        )
        return False

    checks = pr.get("statusCheckRollup") or []
    ci = _ci_status(checks) if checks else ("pending" if had_checks else "green")
    if ci != "green":
        print(
            f"lime deploy: CI is {ci} on the pushed docs merge {merge_sha[:12]} — refusing;"
            " re-run lime deploy once it is green",
            file=sys.stderr,
        )
        _record_refusal(
            project_root, ticket, f"docs_merge_ci_{ci}", run_id=run_id, sha=merge_sha
        )
        return False

    print(f"lime deploy: pushed docs merge {merge_sha[:12]} onto {branch}")
    return True


def _resolve_deploy_run(con: sqlite3.Connection, ticket_arg: str) -> sqlite3.Row | None:
    """Match `lime run`'s ticket argument (a number, or a full stem) against a Run's stored
    `ticket` path — by the time a Run has succeeded the ticket file itself is long gone
    (delete-on-close, spec §10), so the trace db is the only place left to look."""
    rows = con.execute(
        "SELECT * FROM runs WHERE kind='run' ORDER BY started_at DESC"
    ).fetchall()
    for row in rows:
        stem = _deploy_ticket_stem(row["ticket"])
        if stem and tracker.stem_matches(stem, ticket_arg):
            return row
    return None


def _ambiguous_deploy_tickets(con: sqlite3.Connection, ticket_arg: str) -> list[str]:
    """The ticket file names of every Run a bare number matches, when they name more than one
    ticket (`1-b.md`, `01-a.md`); otherwise `[]`. Retries of one ticket are not ambiguous, and
    a discarded Run (`lime runs rm`, say before its ticket was renamed) does not count."""
    if not tracker.is_number(ticket_arg):
        return []
    names = sorted({
        Path(row["ticket"]).name
        for row in con.execute("SELECT ticket FROM runs WHERE kind='run' AND status != 'discarded'")
        if (stem := _deploy_ticket_stem(row["ticket"])) and tracker.stem_matches(stem, ticket_arg)
    })
    return names if len(names) > 1 else []


def _earlier_deploy_row(con: sqlite3.Connection, run_id: str) -> bool:
    """Whether a `kind='deploy'` process row for this Run already exists (ticket 253).

    Ticket 252's deploy row is the only proof that lime merged this Run's PR. This is
    read before this deploy opens its own row, so "earlier" never includes the current
    run. A ledger with no `processes` table holds no proof, so it reads False.
    """
    try:
        row = con.execute(
            "SELECT 1 FROM processes WHERE run_id=? AND kind='deploy' LIMIT 1", (run_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _deploy_prompt(commands: list[str]) -> str:
    numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(commands, start=1))
    return (
        "The PR for this Run just merged. First `git pull` so this checkout has that merge"
        " commit — the commands below must run against it, not whatever was here before."
        " Then run these deploy commands from the project root, in order. Report each"
        " command's result as you go; reason about a failure only if one occurs.\n\n" + numbered
    )


def _deploy_session_command(session_id: str, prompt: str) -> list[str]:
    return [
        "claude", "--model", DEPLOY_MODEL, "--strict-mcp-config",
        "--disallowedTools", DEPLOY_DISALLOWED_TOOLS,
        "--session-id", session_id,
        prompt,
    ]


def _occupancy(usage: dict) -> int:
    """Context occupancy of one turn (spec §4 formula, same as `agent_cc._Stream._occupancy`):
    input + cache_read + cache_write. `output_tokens` is not occupancy."""
    return (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )


def _sum_transcript_tokens(transcript: Path) -> int:
    """Session total: every turn's occupancy summed over the whole transcript (spec §4 formula;
    contrast `_context_tokens`, the Orchestrator hook's *last-turn-only* reading)."""
    if not transcript.exists():
        return 0
    total = 0
    seen_ids: set[str] = set()
    for line in transcript.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        usage = message.get("usage")
        if not usage:
            continue
        msg_id = message.get("id")
        if msg_id:  # repeated per content block, same id+usage (agent_cc._assistant_message)
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
        total += _occupancy(usage)
    return total


def _run_deploy_session(
    tr: tracer.Tracer, run_id: str, repo_root: Path, commands: list[str],
    run_budget: dict[str, int] | None = None,
) -> None:
    """Phase (d): the interactive Deploy session — human present, on the Orchestrator launch
    line, running `deploy:` in order (spec §10, §12).

    `kind="deploy"`, not `"agent"` — CONTEXT.md is explicit that the Deploy session is
    "Not a Role: Roles are headless; the engineer is present here," and it returns no
    Envelope, so it does not fit the Phase library's engineer/agent/code trio either.
    """
    seq = tr.max_phase_seq(run_id) + 1
    phase_id = tr.add_phase(
        run_id, seq=seq, name="deploy", kind="deploy", stage="Ship", owner="orchestrator",
        description="Run commands.deploy in order, interactively, human present.",
    )
    tr.set_phase_status(phase_id, "running")
    session_id = str(uuid.uuid4())
    result = subprocess.run(
        _deploy_session_command(session_id, _deploy_prompt(commands)), cwd=repo_root
    )
    transcript = worktree.session_dir(repo_root) / f"{session_id}.jsonl"
    tokens = _sum_transcript_tokens(transcript)
    # Resolved thresholds, run budget over the defaults (claude_code's window is
    # unknown, so it contributes none) — persisted on the Session row like every
    # other Session (ticket 251).
    warn, stop, _kill_at = budget.thresholds(None, run_budget, 0)
    db_session_id = tr.start_session(
        run_id, phase_id, seq=seq, role="orchestrator", runtime="claude_code", model=DEPLOY_MODEL,
        budget_warn=warn, budget_stop=stop,
    )
    tr.touch_session(db_session_id, context_tokens=tokens)
    tr.add_totals(run_id, tokens=tokens, cost=0.0)  # claude_code spend is the subscription, notional
    tr.set_phase_status(phase_id, "success" if result.returncode == 0 else "fail")
    if result.returncode != 0:
        # The PR is already merged and cleaned up — that can't be undone here, and the
        # human who was just present is the one who saw whatever went wrong. Warn, don't
        # block: (e) still records what ran.
        print(
            f"lime deploy: the Deploy session exited {result.returncode} — check what ran"
            f" before trusting the deploy:\n  lime trace {run_id}",
            file=sys.stderr,
        )


class _DeployProcessError(RuntimeError):
    """The deploy could not open its own `kind="deploy"` process row (ticket 252).

    A deploy without its row can't prove liveness, so it must not merge; this is
    raised before the `with` body runs and caught at the deploy entry point.
    """


@contextlib.contextmanager
def _deploy_process(runs_dir: Path, run_id: str):
    """The deploy's own process row on the Run it deploys (ticket 252).

    Opens a beating `kind="deploy"` row before the merge and closes it on every
    exit — the normal return, a refusal after the row opened, or an exception.
    If the ledger refuses the row, the deploy aborts with `_DeployProcessError`
    rather than merge without it.
    """
    pid = os.getpid()
    tr = None
    try:
        tr = tracer.Tracer(runs_dir)
        process_id = tr.start_process(
            run_id, kind="deploy", name=Path(sys.argv[0]).stem, pid=pid,
            command=" ".join([Path(sys.argv[0]).name, *sys.argv[1:]]),
        )
        tracer.start_heartbeat(runs_dir / "lime.db", process_id)
    except (sqlite3.Error, OSError) as error:
        if tr is not None:
            tr.con.close()
        raise _DeployProcessError(
            f"no process row for {run_id} ({error}) — refusing to deploy without it"
        ) from error
    try:
        yield
    finally:
        try:
            tr.end_process(run_id, pid)
        except (sqlite3.Error, OSError):
            pass
        tr.con.close()


def _cmd_deploy(args: argparse.Namespace) -> int:
    project_root = Path.cwd().resolve()
    runs_dir = (project_root / ".lime" / "runs").resolve()
    db = runs_dir / "lime.db"
    if not db.exists():
        print(f"lime deploy: no trace database at {db}", file=sys.stderr)
        return 1
    con = tracer.connect(db, readonly=True)
    if clash := _ambiguous_deploy_tickets(con, args.ticket):
        con.close()
        print(f"lime deploy: ticket {args.ticket} is ambiguous in {db}: {', '.join(clash)}"
              f" — name one in full, e.g. `lime deploy {Path(clash[0]).stem}`", file=sys.stderr)
        return 2
    row = _resolve_deploy_run(con, args.ticket)
    earlier_deploy = row is not None and _earlier_deploy_row(con, row["run_id"])
    con.close()
    if row is None:
        print(f"lime deploy: no Run matching ticket {args.ticket!r} in {db}", file=sys.stderr)
        return 1
    if row["status"] != "success":
        print(
            f"lime deploy: {row['run_id']} is {row['status']!r}, not 'success' — nothing to deploy",
            file=sys.stderr,
        )
        return 1
    run_id, branch = row["run_id"], row["branch"]
    stem = _deploy_ticket_stem(row["ticket"])

    # (a) PR open + CI green — the one refusal gate (spec §10, ticket 33's Done means)
    try:
        pr = git_helper.pr_view(branch)
    except RuntimeError as error:
        print(f"lime deploy: {error}", file=sys.stderr)
        return 1
    state = pr.get("state")
    # MERGED with an earlier deploy row is our own half-done deploy: lime already merged
    # this PR, so there is no open PR to gate on and no CI to wait for — finish it.
    finishing = state == "MERGED" and earlier_deploy
    if state != "OPEN" and not finishing:
        print(f"lime deploy: PR for {branch} is {state}, not OPEN — refusing", file=sys.stderr)
        _record_refusal(
            project_root, stem,
            "hand_merge" if state == "MERGED" else "pr_not_open",
            run_id=run_id, state=state,
        )
        return 1
    if not finishing:
        ci = _ci_status(pr.get("statusCheckRollup") or [])
        if ci != "green":
            print(f"lime deploy: CI is {ci} for {branch} — refusing until it's green", file=sys.stderr)
            _record_refusal(project_root, stem, f"ci_{ci}", run_id=run_id)
            return 1

    with _deploy_process(runs_dir, run_id):
        # (a2) pre-merge gate: run setup+test against the actual merged tree in a throwaway
        # detached worktree, before any merge/ff/teardown/session. Load the config once and
        # reuse it for the post-merge behavior below. On green the context is already cleaned
        # up; on refusal the Run stays success and nothing merges.
        try:
            raw = config.load(project_root) or {}
        except Exception as error:  # unreadable/invalid config is a deploy refusal, never a crash
            print(f"lime deploy: cannot load project config — {error}", file=sys.stderr)
            return 1
        commands = raw.get("commands") or {}
        if finishing:
            print(
                f"lime deploy: {branch} was merged by an earlier deploy of {run_id}"
                " — finishing it"
            )
        else:
            merged_head: list[str] = []
            if _check_deploy_merged_tree(
                project_root, run_id, row["base_sha"], branch, commands,
                excluded_ports=tuple(config.port_range(raw)), ticket=stem,
                docs_writes=_documenter_writes(raw, row["profile"]),
                had_checks=bool(pr.get("statusCheckRollup")),
                merged_head=merged_head,
            ) != 0:
                return 1

            # (b) merge + delete remote branch
            try:
                if merged_head:
                    # a docs-only conflict was resolved, tested and lease-pushed as
                    # `merged_head[0]`; merge the PR at that exact head
                    git_helper.merge_pr(branch, head_sha=merged_head[0])
                else:
                    git_helper.merge_pr(branch)
            except RuntimeError as error:
                print(f"lime deploy: {error}", file=sys.stderr)
                return 1
            print(f"lime deploy: merged {branch}")

        # Every step below passes when it is already done, so a finishing run that dies
        # again leaves the Run `success` with its own deploy row, and the next re-run
        # finishes it.

        # (b2) the merge happened on the remote — bring the local default branch (and the
        # working tree the engineer is looking at) up to it. Fast-forward only: if it is
        # refused, warn and carry on; (c)-(e) still owe the caller a finished deploy.
        # Also remove the untracked copy of the closed ticket — the Run carried it into
        # its branch and its docs commit deleted it there; the engineer's untracked copy
        # is the last trace of an already-closed ticket.
        ff_ok = False
        try:
            base = worktree.default_branch(project_root)
            print(f"lime deploy: fast-forwarded {base} to {git_helper.fast_forward(base)}")
            stem = _deploy_ticket_stem(row["ticket"])
            copy = project_root / ".lime" / "tickets" / f"{stem}.md"
            if stem and copy.exists() and git_helper.uncommitted_state(str(copy)) == "untracked":
                copy.unlink()
                print(f"lime deploy: removed {copy.relative_to(project_root)} — closed by the merge")
            ff_ok = True
        except RuntimeError as error:
            print(
                f"lime deploy: could not fast-forward the local default branch — {error}\n"
                "  the merge is safe on origin; run `git pull --ff-only` once your tree allows it",
                file=sys.stderr,
            )

        # (b3) reconcile the ticket dir against what has merged (ticket 61). Only after a
        # real fast-forward — a refused one leaves main pre-merge, where the copy is expected.
        failed = False
        if ff_ok:
            stem = _deploy_ticket_stem(row["ticket"])
            if stem and (project_root / TICKETS_REL / f"{stem}.md").exists():
                print(
                    f"lime deploy: {TICKETS_REL / f'{stem}.md'} survived the merge of {branch} —"
                    " the Run's delete-on-close never reached the default branch; remove it by"
                    " hand (git rm) and land that removal on a branch",
                    file=sys.stderr,
                )
                failed = True
            try:
                for path, number in _stale_tickets(project_root, git_helper.merged_prs()):
                    if path.stem != stem:   # the deployed ticket was reported above
                        print(f"lime deploy: {_stale_line(path, number)}", file=sys.stderr)
            except RuntimeError as error:
                print(f"lime deploy: stale-ticket check skipped: gh unavailable ({error})", file=sys.stderr)

        # (c) teardown + worktree + local branch removed
        problems = worktree.teardown(
            project_root, run_id, branch,
            command=commands.get("teardown"),
            env=_run_env(run_id, row["port"], row["db_name"]),
        )
        for problem in problems:
            print(f"lime deploy: {problem}", file=sys.stderr)

        # (d)-(e) Deploy session (empty commands.deploy = skip, per the template) + deployed row
        tr = tracer.Tracer(runs_dir)
        deploy_commands = raw.get("deploy") or []
        if deploy_commands:
            _run_deploy_session(tr, run_id, project_root, deploy_commands, raw.get("budget"))
        tr.set_run_status(run_id, "deployed")
        print(f"lime deploy: {run_id} deployed")
        return 1 if failed else 0


def _cmd_deploy_check() -> int:
    """`lime deploy --check`: fetch origin and report — never merge, ff, checkout or delete (ticket 61)."""
    project_root = Path.cwd().resolve()
    base = worktree.default_branch(project_root)
    try:
        ahead, behind = git_helper.sync_state(base)
    except RuntimeError as error:
        print(f"lime deploy --check: {error}", file=sys.stderr)
        return 1
    if ahead == 0 and behind == 0:
        state = "up to date"
    elif ahead == 0:
        state = f"fast-forwardable ({behind} behind)"
    else:
        state = f"diverged ({ahead} ahead / {behind} behind)"
    print(f"lime deploy --check: {base} is {state} with origin/{base}")

    # Advisory base-movement report over open Runs (ticket 141). Uses the refs
    # sync_state already fetched; never writes statuses, never creates the db.
    default_sha = None
    try:
        default_sha = git_helper.resolve_remote_tip(
            project_root, f"origin/{base}", label="default branch"
        )
    except RuntimeError:
        default_sha = None
    if default_sha is not None:
        db = project_root / ".lime" / "runs" / "lime.db"
        if db.exists():
            con = tracer.connect(db, readonly=True)
            try:
                # A legacy ledger never recorded a base; its open Runs still get listed
                # (they cannot be inspected, but the operator must see them).
                legacy = tracer.runs_lacks_added_columns(con)
                open_runs = con.execute(
                    "SELECT run_id, ticket, branch, base_sha FROM runs"
                    " WHERE kind='run' AND status NOT IN ('deployed','discarded')"
                    " ORDER BY started_at"
                ).fetchall()
                for run_row in open_runs:
                    movement = git_helper.base_movement(
                        project_root, run_row["base_sha"], default_sha
                    )
                    ident = _deploy_ticket_stem(run_row["ticket"]) or run_row["branch"]
                    if movement.status == "behind":
                        print(
                            f"lime deploy --check: {run_row['run_id']} ({ident})"
                            f" base moved: {movement.merges} merges since base"
                        )
                    elif movement.status == "invalid" and run_row["base_sha"]:
                        print(
                            f"lime deploy --check: {run_row['run_id']} cannot inspect base"
                            f" {run_row['base_sha']}: {movement.detail}"
                        )
                    elif movement.status == "invalid" and legacy:
                        print(
                            f"lime deploy --check: {run_row['run_id']} ({ident})"
                            f" has no recorded base"
                        )
            finally:
                con.close()

    ok = ahead == 0
    try:
        stale = _stale_tickets(project_root, git_helper.merged_prs())
    except RuntimeError as error:
        print(f"lime deploy --check: stale-ticket check skipped: gh unavailable ({error})")
        stale = []
    else:
        for path, number in stale:
            print(f"lime deploy --check: {_stale_line(path, number)}")
        if not stale:
            print("lime deploy --check: no stale tickets")
    return 0 if ok and not stale else 1


def _open_db(command: str) -> sqlite3.Connection | None:
    db = RUNS_DIR / "lime.db"
    if not db.exists():
        print(f"lime {command}: no trace database at {db}", file=sys.stderr)
        return None
    return tracer.connect(db, readonly=True)


def _resolve_run(con: sqlite3.Connection, command: str, run_id: str | None) -> str | None:
    if run_id is None:
        row = con.execute("SELECT run_id FROM runs ORDER BY started_at DESC").fetchone()
        if row is None:
            print(f"lime {command}: no runs yet", file=sys.stderr)
            return None
        return row["run_id"]
    if con.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
        print(f"lime {command}: unknown run {run_id!r}", file=sys.stderr)
        return None
    return run_id


def _table(headers: list[str], rows: list[tuple]) -> None:
    rows = [tuple("" if v is None else str(v) for v in row) for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(headers)]
    for line in [tuple(headers)] + rows:
        print("  ".join(v.ljust(w) for v, w in zip(line, widths)).rstrip())


def _run_cost(con: sqlite3.Connection, run_id: str) -> float:
    row = con.execute(
        "SELECT sum(json_extract(payload_json, '$.cost')) AS c FROM events"
        " WHERE run_id=? AND type='turn'",
        (run_id,),
    ).fetchone()
    return row["c"] or 0.0


def _event_line(e: sqlite3.Row) -> str:
    clock = e["started_at"][11:19]  # HH:MM:SS of the ISO timestamp
    return "  ".join(filter(None, (clock, e["type"], e["name"] or "")))


def _cmd_trace(args: argparse.Namespace) -> int:
    con = _open_db("trace")
    if con is None:
        return 1
    run_id = _resolve_run(con, "trace", args.run_id)
    if run_id is None:
        return 1
    if args.summary:
        _print_summary(con, run_id)
    elif args.follow:
        _follow(con, run_id)
    else:
        _print_trace(con, run_id)
    return 0


def _phase_rows(con: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT phase_id, seq, name, kind, owner, status, attempt FROM phases"
        " WHERE run_id=? ORDER BY seq",
        (run_id,),
    ).fetchall()


def _phase_spend(con: sqlite3.Connection, run_id: str) -> dict[str, tuple[int, float]]:
    """(tokens, cost) per phase — every `turn` event rolled up by the phase it fired in."""
    rows = con.execute(
        "SELECT phase_id, sum(tokens) AS tokens,"
        " sum(json_extract(payload_json, '$.cost')) AS cost"
        " FROM events WHERE run_id=? AND type='turn' GROUP BY phase_id",
        (run_id,),
    ).fetchall()
    return {r["phase_id"]: (r["tokens"] or 0, r["cost"] or 0.0) for r in rows}


def _fmt_spend(spend: tuple[int, float] | None) -> tuple[int, str]:
    tokens, cost = spend or (0, 0.0)
    return tokens, f"${cost:.4f}"


def _print_trace(con: sqlite3.Connection, run_id: str) -> None:
    run = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    print(f"{run_id}  {run['workflow']}  ticket {run['ticket']}  {run['status']}")
    print()
    spend = _phase_spend(con, run_id)
    _table(
        ["seq", "name", "kind", "owner", "status", "attempt", "tokens", "cost"],
        [
            (p["seq"], p["name"], p["kind"], p["owner"], p["status"], p["attempt"],
             *_fmt_spend(spend.get(p["phase_id"])))
            for p in _phase_rows(con, run_id)
        ],
    )
    gates = con.execute(
        "SELECT phase_id, attempt, gate, passed FROM gate_results"
        " WHERE run_id=? ORDER BY id",
        (run_id,),
    ).fetchall()
    if gates:
        print()
        _table(
            ["phase", "attempt", "gate", "result"],
            [(g["phase_id"], g["attempt"], g["gate"], "pass" if g["passed"] else "FAIL")
             for g in gates],
        )
    events = con.execute(
        "SELECT * FROM events WHERE run_id=? ORDER BY event_id DESC LIMIT 25",
        (run_id,),
    ).fetchall()
    if events:
        print()
        for e in reversed(events):
            print(_event_line(e))


def _print_summary(con: sqlite3.Connection, run_id: str) -> None:
    """One screen: header, phases, gate tally, last event — ≤20 lines."""
    run = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    cost = _run_cost(con, run_id)
    print(
        f"{run_id}  {run['workflow']}  ticket {run['ticket']}"
        f"  {run['status']}  ${cost:.2f}  started {run['started_at'][:19]}"
    )
    phases = _phase_rows(con, run_id)
    allotment = 16
    if len(phases) > allotment:
        print(f"  … {len(phases) - allotment} earlier phases")
        phases = phases[-allotment:]
    for p in phases:
        line = f"  {p['seq']:02d} {p['name']}  {p['kind']}"
        if p["owner"]:
            line += f" {p['owner']}"
        line += f"  {p['status']}"
        if p["attempt"] > 1:
            line += f"  attempt {p['attempt']}"
        print(line)
    tally = con.execute(
        "SELECT sum(passed) AS ok, sum(1 - passed) AS bad FROM gate_results"
        " WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if tally["ok"] is not None:
        print(f"gates: {tally['ok']} passed · {tally['bad']} failed")
    last = con.execute(
        "SELECT * FROM events WHERE run_id=? ORDER BY event_id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if last is not None:
        print(f"last: {_event_line(last)}")


def _new_events(con: sqlite3.Connection, run_id: str, cursor: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM events WHERE run_id=? AND event_id>? ORDER BY event_id",
        (run_id, cursor),
    ).fetchall()


def _follow(con: sqlite3.Connection, run_id: str) -> None:
    """Print the trace, then tail new events by rowid cursor; Ctrl-C stops."""
    _print_trace(con, run_id)
    row = con.execute(
        "SELECT max(event_id) AS c FROM events WHERE run_id=?", (run_id,)
    ).fetchone()
    cursor = row["c"] or 0
    try:
        while True:
            for e in _new_events(con, run_id, cursor):
                print(_event_line(e), flush=True)
                cursor = e["event_id"]
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

DASH_DEFAULT_PORT = 4600  # dashboard/server/port.ts OWNER_PORT — the owner's dashboard port
DASH_UI_PORT = 4601  # vite.config.ts server.port and dashboard/server/local-only.ts UI_PORT


def _dash_port(flag: int | None) -> int:
    """The API port `lime dash` hands both children as PORT — dashboard/server/port.ts's
    rule with --port in front: --port, else PORT, else LIME_PORT (set inside a Run),
    else 4600. Empty counts as unset; anything else that is not 1-65535 is a ValueError."""
    sources = [("--port", str(flag))] if flag is not None else []
    sources += [(name, os.environ.get(name, "").strip()) for name in ("PORT", "LIME_PORT")]
    for name, raw in sources:
        if raw:
            if raw.isdigit() and 0 < int(raw) < 65536:
                return int(raw)
            raise ValueError(f"{name} {raw!r} is not a port (1-65535)")
    return DASH_DEFAULT_PORT


def _port_busy(port: int) -> bool:
    """Would the API server's bind on 127.0.0.1:port fail? SO_REUSEADDR, so a TIME_WAIT
    left by the last `lime dash` is not busy; a live listener still is."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _wait_listening(server, port: int, timeout: float = 15.0) -> str | None:
    """None once 127.0.0.1:port accepts a connection; else why not (the server exited, or
    stayed silent past `timeout`). `_port_busy` ran first, so the listener is ours."""
    deadline = time.monotonic() + timeout
    while True:
        code = server.poll()
        if code is not None:
            return (f"the API server exited (code {code}) before listening on 127.0.0.1:{port}"
                    f" — is port {port} busy? See its output above.")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return None
        except OSError:
            pass
        if time.monotonic() > deadline:
            return f"the API server did not listen on 127.0.0.1:{port} within {timeout:g}s"
        time.sleep(0.1)


def _registry_path() -> Path:
    return Path(os.environ.get("LIME_PROJECTS") or Path.home() / ".lime" / "projects")


def _cmd_dash(args) -> int:
    """Boot the dashboard: Bun API server over every registered lime.db + Vite UI.

    The one-command observability boot — the server is read-only and the
    registry (`~/.lime/projects`, one project path per line) says what it reads.
    Both children get one PORT: `--port`, else PORT, else LIME_PORT, else 4600;
    a busy port fails before anything starts. Once the API server listens it prints
    the home URL (`lime dash: home → http://localhost:4601/#/`), where the Vite UI
    serves; the UI port is fixed at 4601 and `--port` moves only the API port.
    """
    bun = shutil.which("bun")
    if bun is None:
        print(
            "lime dash: bun not found — the dashboard runs on Bun.\n"
            "Install it (https://bun.sh) and re-run; `lime check` reports its presence.",
            file=sys.stderr,
        )
        return 1
    if not (DASHBOARD_DIR / "package.json").exists():
        print(f"lime dash: dashboard app not found at {DASHBOARD_DIR}", file=sys.stderr)
        return 1
    registry = _registry_path()
    registered = registry.exists() and any(
        line.strip() and not line.strip().startswith("#")
        for line in registry.read_text().splitlines()
    )
    if not registered:
        state = "empty project registry" if registry.exists() else "no project registry"
        print(
            f"lime dash: {state} at {registry}\n"
            "`lime init` registers a repo there; LIME_PROJECTS=<file> overrides the location.",
            file=sys.stderr,
        )
        return 1
    try:
        port = _dash_port(args.port)
    except ValueError as exc:
        print(f"lime dash: {exc}", file=sys.stderr)
        return 1
    if _port_busy(port):
        print(f"lime dash: port {port} is busy on 127.0.0.1 — stop what holds it, or pick another: lime dash --port <N>", file=sys.stderr)
        return 1
    env = {**os.environ, "LIME_PROJECTS": str(registry), "PORT": str(port)}
    if subprocess.run([bun, "install"], cwd=DASHBOARD_DIR).returncode != 0:
        print("lime dash: `bun install` failed in dashboard/ — see above.", file=sys.stderr)
        return 1
    server = subprocess.Popen([bun, "run", "server/index.ts"], cwd=DASHBOARD_DIR, env=env)
    try:
        problem = _wait_listening(server, port)
        if problem:
            print(f"lime dash: {problem}", file=sys.stderr)
            return 1
        print(f"lime dash: home → http://localhost:{DASH_UI_PORT}/#/", flush=True)
        # Vite owns the foreground (and the Ctrl-C); the API server rides along.
        ui = subprocess.run([bun, "x", "vite"], cwd=DASHBOARD_DIR, env=env)
    finally:
        server.terminate()
        server.wait()
    return ui.returncode


def _writes_cell(writes: list[str] | None) -> str:
    if writes is None:
        return "all − trust"
    return " ".join(writes) or "[]"


def _probe_failures() -> list[str]:
    """The live end of `lime check`: gh auth, one haiku call, Bun presence (spec §5)."""
    failures = []
    if shutil.which("gh") is None:
        failures.append("gh not found — the push step and `lime deploy` need it")
    elif subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        failures.append("gh auth status is red — run `gh auth login`")
    if shutil.which("bun") is None:
        failures.append("bun not found — `lime dash` runs on it")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        probe = subprocess.run(
            ["claude", "-p", "Reply with exactly: ok", "--model", "haiku"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        if probe.returncode != 0:
            failures.append(f"claude haiku probe failed: {probe.stderr.strip()[-200:] or 'no stderr'}")
    except OSError:
        failures.append("claude not found — the claude_code runtime needs it")
    except subprocess.TimeoutExpired:
        failures.append("claude haiku probe timed out after 120s")
    return failures


def _cmd_check(args: argparse.Namespace) -> int:
    raw = config.load()
    line = f"lime check — {config.config_path()}"
    if args.profile:
        line += f" · profile {args.profile}"
    print(line)
    failures = config.validate(raw, profile=args.profile)
    failures += config.context_md_failures(Path("."))
    warnings: list[str] = []
    try:
        agents = config.roster(raw, args.profile)
    except (ValueError, TypeError):
        agents = []
    if agents:
        print()
        _table(
            ["role", "runtime · model · thinking", "tools", "writes"],
            [(a.name, f"{a.runtime} · {a.model} · {a.thinking}",
              " ".join(a.tools), _writes_cell(a.writes)) for a in agents],
        )
        print()
        for a in agents:
            try:
                *_, tool_warnings = config.resolve_tools(a.runtime, a.tools)
            except ValueError:
                continue  # already a validation failure
            warnings += [f"{a.name}: {w}" for w in tool_warnings]
    cap_failures, cap_warnings = config.capability_failures(agents)
    failures += cap_failures
    warnings += cap_warnings
    tickets = sorted(Path(".lime/tickets").glob("*.md"))
    # Every ticket is loaded and visited — never short-circuit behind `any(...)` —
    # so each status/kind contributes its Where diagnostics.
    for ticket_path in tickets:
        ticket = tracker.load(ticket_path)
        failures += tracker.where_failures(ticket, Path.cwd())
    cred_failures, cred_warnings = config.credential_failures(agents)
    failures += cred_failures
    warnings += cred_warnings
    failures += config.compaction_failures()
    failures += config.ticket_failures()
    failures += _probe_failures()
    for warning in warnings:
        print(f"warn  {warning}")
    for failure in failures:
        print(f"FAIL  {failure}")
    if failures:
        print(
            f"{len(failures)} failure(s) — config, model, credential and compaction failures"
            " also stop `lime run`; a ticket's Where failure stops any launch that includes that ticket;"
            " ticket-header, CONTEXT.md, gh, bun and claude-probe failures only fail this check"
        )
        return 1
    print("green — roster, catalog, credentials, tickets and probes all pass")
    return 0


def _cmd_cost(args: argparse.Namespace) -> int:
    con = _open_db("cost")
    if con is None:
        return 1
    if args.run_id is not None:
        run_id = _resolve_run(con, "cost", args.run_id)
        if run_id is None:
            return 1
        where, params = "e.run_id=?", (run_id,)
        print(f"cost — run {run_id}")
    else:
        since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        where, params = "e.started_at>=?", (since,)
        print("cost — last 7 days")
    rows = con.execute(
        "SELECT s.runtime, s.model, s.role,"
        " sum(json_extract(e.payload_json, '$.cost')) AS cost"
        f" FROM events e JOIN sessions s ON s.session_id = e.name"
        f" WHERE e.type='turn' AND {where}"
        " GROUP BY s.runtime, s.model, s.role",
        params,
    ).fetchall()
    by_provider: dict[str, float] = {}
    by_role: dict[str, float] = {}
    for r in rows:
        # No price table: provider = model prefix; claude_code spend is the
        # subscription, so its figure is notional, not billed.
        provider = "claude_code" if r["runtime"] == "claude_code" else r["model"].split("/")[0]
        by_provider[provider] = by_provider.get(provider, 0.0) + (r["cost"] or 0.0)
        by_role[r["role"]] = by_role.get(r["role"], 0.0) + (r["cost"] or 0.0)
    print("by provider")
    for provider, cost in sorted(by_provider.items(), key=lambda kv: -kv[1]):
        note = "  subscription, notional" if provider == "claude_code" else ""
        print(f"  {provider:<16} ${cost:.2f}{note}")
    print("by role")
    for role, cost in sorted(by_role.items(), key=lambda kv: -kv[1]):
        print(f"  {role:<16} ${cost:.2f}")
    return 0


# --- profiles: the dashboard's config read/write path (ticket 87) ---


def _cmd_profiles_get() -> int:
    """Full raw agents:/profiles: blocks plus the merged roster per profile, as
    JSON on stdout (the dashboard's GET …/profiles). Also carries the
    per-model thinking support table (ticket 91)."""
    try:
        raw = config.load(Path.cwd())
    except Exception as error:
        print(f"lime profiles get: {error}", file=sys.stderr)
        return 1
    profiles = raw.get("profiles") or {}
    merged = {"default": config.merged_agents(raw)}
    for pname in profiles:
        try:
            merged[pname] = config.merged_agents(raw, pname)
        except (ValueError, TypeError) as error:
            print(f"lime profiles get: {error}", file=sys.stderr)
            return 1
    print(json.dumps({
        "agents": raw.get("agents") or [],
        "profiles": profiles,
        "defaults": raw.get("defaults") or {},
        "merged": merged,
        "profiles_list": ["default", *profiles],
        "thinking_support": config.thinking_support(),
    }))
    return 0


def _cmd_models() -> int:
    """pi's own catalog as {source, models} on stdout — the models the dashboard's
    tray may offer, which is exactly the set `lime check` accepts (ticket 92).
    An empty list means pi has no catalog here; the dashboard falls back to the ledger."""
    print(json.dumps({"source": "pi", "models": config.catalog_models()}))
    return 0


def _cmd_profiles_set() -> int:
    """Replace the agents: and profiles: blocks from {agents, profiles} JSON on
    stdin. Validation failures print {ok:false, errors} and leave the file
    untouched; success prints {ok:true, wrote} — wrote:false is a canonical
    no-op (the file was not touched at all)."""
    try:
        payload = json.load(sys.stdin)
    except ValueError as error:
        print(json.dumps({"ok": False, "errors": [f"invalid JSON on stdin: {error}"]}))
        return 1
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("agents"), list)
        or not isinstance(payload.get("profiles"), dict)
    ):
        print(json.dumps({
            "ok": False,
            "errors": ['payload must be {"agents": [...], "profiles": {...}}'],
        }))
        return 1
    path = Path.cwd() / ".lime" / "config.yaml"
    before = path.read_bytes() if path.exists() else None
    ok, errors = config.write_profiles(Path.cwd(), payload["agents"], payload["profiles"])
    if not ok:
        print(json.dumps({"ok": False, "errors": errors}))
        return 1
    wrote = before is None or path.read_bytes() != before
    print(json.dumps({"ok": True, "wrote": wrote}))
    return 0


def _cmd_profiles_merge() -> int:
    """Apply profile-model deltas from ``{changes}`` JSON on stdin.  Each change
    ``{profile, role, patch}`` where ``patch`` carries ``model``, ``runtime``,
    and optional ``thinking``.  Returns ``{ok:true, wrote}`` on success or
    ``{ok:false, errors}`` on failure — same convention as ``set``.  Invalid
    input is rejected before any write; an identical-merge no-op returns
    ``wrote:false`` without touching the file."""
    try:
        payload = json.load(sys.stdin)
    except ValueError as error:
        print(json.dumps({"ok": False, "errors": [f"invalid JSON on stdin: {error}"]}))
        return 1
    changes = payload.get("changes")
    if not isinstance(changes, list) or not changes:
        print(json.dumps({"ok": False, "errors": ["changes must be a non-empty list"]}))
        return 1
    ok, wrote, errors = config.merge_profiles(Path.cwd(), changes)
    if not ok:
        print(json.dumps({"ok": False, "errors": errors}))
        return 1
    print(json.dumps({"ok": True, "wrote": wrote}))
    return 0


# --- hook budget: the Orchestrator's advisory UserPromptSubmit hook (spec §9) ---

BUDGET_WARN = 150_000  # smart-zone warn; same number as a Run's, advisory only here
BUDGET_EXTENSION = str(Path(__file__).resolve().parent.parent / "guard" / "budget.ts")  # pi delivery of this nudge
SCOUT_EXTENSION = str(Path(__file__).resolve().parent / "harness_engineering" / "scout.ts")  # pi native scout bridge


def _context_tokens(transcript: Path) -> int | None:
    """Last assistant turn's context occupancy (spec §4 formula; contrast
    `_sum_transcript_tokens`, the Deploy session's *whole-transcript* reading)."""
    if not transcript.exists():
        return None
    tokens = None
    for line in transcript.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        usage = (event.get("message") or {}).get("usage")
        if not usage:
            continue
        tokens = _occupancy(usage)
    return tokens


def _cmd_hook_budget(args) -> int:
    """`lime hook budget` — the Orchestrator's advisory nudge, one text, two
    harnesses. Claude Code: UserPromptSubmit hook, transcript on stdin. pi:
    `guard/budget.ts` (loaded with `-e $(lime hook budget --path)`) reads the
    session's context usage on turn_end and passes it as `--tokens`. Silent
    under warn, a printed line at or above it. Never denies — the Orchestrator
    has no Guard, only this nudge (spec §9)."""
    if args.path:
        print(BUDGET_EXTENSION)
        return 0
    if args.tokens is not None:
        tokens, fresh = args.tokens, "/new"
    else:
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            return 0
        if not isinstance(payload, dict):
            return 0
        transcript = payload.get("transcript_path")
        tokens = _context_tokens(Path(transcript)) if transcript else None
        fresh = "/clear"
    if tokens is None or tokens < BUDGET_WARN:
        return 0
    handover_cmd = 'claude -p "/lime map <map> <NN>"'
    venue = (
        f"a Herdr pane running: {handover_cmd}"
        if os.environ.get("HERDR_ENV") == "1"
        else f"the printed line: {handover_cmd}"
    )
    print(
        f"lime budget: ~{tokens:,} tokens ≥ warn {BUDGET_WARN:,} (smart-zone) — advisory only.\n"
        f"Ordinary work: finish this step, then {fresh}.\n"
        "Driving a claimed map ticket: write assets/<NN>-handoff.md (≤40 lines),"
        f" then hand over via {venue}."
    )
    return 0


# --- guard: the per-tool-call budget messenger (spec §8) ---

_HANDOFF_WRITE_TOOLS = {"write", "edit"}  # runtime-native names, lowercased (pi and cc both match)


def _native_write_target(call: dict) -> str | None:
    """The file a native write/edit tool call names, or None.

    Only the lowercased native names `write`/`edit` count. A missing/non-object
    input or a non-string/missing target yields None — the runtime validates its
    own input, and the Guard must never crash on a malformed call."""
    if str(call.get("tool_name", "")).lower() not in _HANDOFF_WRITE_TOOLS:
        return None
    tool_input = call.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(path, str) or not path:
        return None
    return path


def _in_handoff(path: str, handoff_dir: str | None) -> bool:
    """True when the path resolves inside the resolved handoff dir (never a string prefix)."""
    if not handoff_dir:
        return False
    try:
        return Path(path).resolve().is_relative_to(Path(handoff_dir).resolve())
    except (OSError, ValueError):
        return False


def _exempt_at_stop(call: dict, handoff_dir: str | None) -> bool:
    """The two exemptions at `stop`: the `submit` envelope call — `status: partial`
    must still be deliverable (ticket 74) — and a write inside the handoff dir."""
    if str(call.get("tool_name", "")).lower() == config.SUBMIT_TOOL:
        return True
    path = _native_write_target(call)
    return path is not None and _in_handoff(path, handoff_dir)


def _parse_writes(raw: str | None) -> list[str] | None:
    """The Role's call-time write boundary, or None when it is unset or malformed.

    Unset (no env) means no new restriction. A valid value is a JSON list of
    strings. Invalid JSON, null, objects, scalars, and lists holding a non-string
    are malformed: they skip only this fence (the phase-end diff check still runs)."""
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, list) or not all(isinstance(w, str) for w in parsed):
        return None
    return parsed


def _write_fence_reason(handoff_dir: str | None) -> str:
    destination = handoff_dir or "LIME_HANDOFF_DIR"
    return (
        "this Role's writes boundary does not allow that path. Put your findings in "
        f"the configured handoff directory ({destination}) and call submit to deliver "
        "your report."
    )


def _write_fence(call: dict, handoff_dir: str | None, raw_writes: str | None) -> str | None:
    """Deny a native write/edit outside the Role's writes boundary, else None.

    Handoff-directory targets are always allowed (the phase-end check never sees
    them). Everything else must be inside the resolved cwd and pass the SAME
    allowlist matcher the phase-end check uses — writes is always a concrete list
    here, so `protected_files=[]` decides identically to loading trust: only an
    explicit grant passes. A target outside cwd (even for a broad `**` grant) is
    denied. Malformed policy or an unusable target supplies no denial."""
    writes = _parse_writes(raw_writes)
    if writes is None:
        return None
    path = _native_write_target(call)
    if path is None:
        return None
    if _in_handoff(path, handoff_dir):
        return None
    try:
        target = Path(path).resolve()
        cwd = Path.cwd().resolve()
        if not target.is_relative_to(cwd):
            return _write_fence_reason(handoff_dir)
        relative = target.relative_to(cwd).as_posix()
        agent = AgentConfig(name="guard", model="guard", writes=writes)
        if not permissions.permitted(relative, agent, []):
            return _write_fence_reason(handoff_dir)
    except (OSError, ValueError):
        return None  # a path/permission error must not become a hard denial
    return None


def _cmd_guard() -> int:
    """`lime guard` — reads budget.json, answers allow / allow+warn / deny-unless-handoff-write.

    Delivered per the probe (assets/07-guard-probe.md): claude_code runs this
    directly as the PreToolUse hook command; pi's `guard/guard.ts` shells out to
    it from the `tool_call` extension hook. Both consume the same
    `hookSpecificOutput`-shaped JSON on stdout — cc verbatim, guard.ts translated.
    """
    try:
        call = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        call = {}
    budget_file = os.environ.get("LIME_BUDGET_FILE")
    handoff_dir = os.environ.get("LIME_HANDOFF_DIR")
    mailbox = budget.read(Path(budget_file)) if budget_file else {"state": "ok"}
    state = mailbox.get("state", "ok")
    stop_reason = mailbox.get("reason")

    partial_verb = "report" if os.environ.get("LIME_AGENT_RUNTIME") == "claude_code" else "submit"
    truthful = (
        "Report truthfully. "
        "Use status: success only when every applicable requirement "
        "(including required verification and reporting/artifacts) is already met; "
        f"otherwise write a useful handoff and {partial_verb} status: partial, identifying "
        "done and remaining/unverified work. Never claim checks ran or passed when "
        "they did not."
    )
    decision, reason, context = "allow", "", ""
    if state == "stop" and not _exempt_at_stop(call, handoff_dir):
        decision = "deny"
        if stop_reason == "time_limit":
            reason = (
                "the Session's time ceiling has been reached and substantive tools are "
                "closed — only handoff-directory writes/edits and final reporting remain. "
                + truthful
            )
        elif stop_reason == "repeat":
            reason = (
                "this Session called the same tool with identical arguments three times "
                "in a row, so substantive tools are closed — only handoff-directory "
                "writes/edits and final reporting remain. " + truthful
            )
        else:
            reason = "context budget exhausted — no more substantive work. " + truthful
    else:
        try:
            fence = _write_fence(call, handoff_dir, os.environ.get("LIME_WRITES"))
        except Exception:  # an internal fence/permission error fails open, never a hard denial
            fence = None
        if fence:
            decision = "deny"
            reason = fence
        elif state == "warn" and budget_file:
            warned = Path(budget_file).with_name("guard_warned")
            if not warned.exists():
                warned.touch()
                handoff_path = os.environ.get("LIME_HANDOFF_PATH", handoff_dir or "")
                context = (
                    "context budget warning — reduce exploration, avoid new scope, and "
                    "prioritize completing the current step and required verification. "
                    "Keep a concise handoff note up to date at "
                    f"{handoff_path} (<=80 lines: Done/Left/Decisions/Don't redo/Open, "
                    "no code or diffs) as work proceeds. This warning alone does not require "
                    "stopping or submitting partial."
                )

    output: dict = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision}}
    if reason:
        output["hookSpecificOutput"]["permissionDecisionReason"] = reason
    if context:
        output["hookSpecificOutput"]["additionalContext"] = context
    print(json.dumps(output))
    return 0
