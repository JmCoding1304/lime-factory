"""The `lime init` setup wizard: stack commands, accounts, pi compaction, a first ticket.

Every question goes through the injected `ask(prompt) -> str` (the CLI passes `input`),
so tests drive it with scripted answers and no TTY. Each file is written whole and
atomically once its questions are answered, so Ctrl-C or EOF at any point leaves valid
files behind.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Callable, Sequence

import yaml

from . import config, git_helper, numbering

Ask = Callable[[str], str]

COMMAND_KEYS = ("setup", "test", "lint", "typecheck", "build", "teardown")
PLACEHOLDER_TEST = config.PLACEHOLDER_TEST  # `config.validate` refuses it, so no Run starts on it
# Per key, the Makefile targets in priority order (first present wins). Never `install`:
# a Run's setup runs in every worktree, and `make install` writes outside it.
MAKE_TARGETS = {"setup": ("setup",), "test": ("test",), "lint": ("lint",),
                "typecheck": ("typecheck",), "build": ("build",)}
NO_UV_SETUP = ("  no setup proposed: no uv.lock found, and a Run never installs into your own"
               " Python environment")
CREATE_ORIGIN = "gh repo create --private --source=. --remote=origin"
COMPACTION_OFF = '{"compaction": {"enabled": false}}'

RESUME = """Setup stopped. Every file written so far is complete.{again}
Or finish by hand:
  1. edit `commands:`, `deploy:` and the roster (`agents:`) in .lime/config.yaml
  2. lime check
  3. commit the files lime init stamped and push them to origin (a Run starts from what origin has)
  4. lime ticket next, then write .lime/tickets/<N>-<slug>.md (see README, First Run)"""
AGAIN = "\nTo get the questions again: delete .lime/config.yaml, then rerun `lime init`."

TICKET = """# {number} — {title}
Workflow: feature
Status: open

## Ask
{title}.
TODO: say in one or two sentences what should change and why.

## Where
- CHANGE-ME/path/to/file - TODO: one backticked path per line, then a few words on why

## Done means
- TODO: one observable result per bullet, six at most
- TODO: tests cover it

## Out of scope
- TODO: what this ticket is not, each entry with a ticket number or the word never
"""


# --- (a) stack commands ---


def _read(root: Path, name: str) -> str | None:
    path = root / name
    return path.read_text(errors="replace") if path.is_file() else None


def _node_commands(root: Path, package: str) -> dict[str, str]:
    try:
        scripts = (json.loads(package).get("scripts") or {})
    except (ValueError, AttributeError):
        scripts = {}
    if not isinstance(scripts, dict):
        scripts = {}
    if (root / "pnpm-lock.yaml").exists():
        setup, run = "pnpm install --frozen-lockfile", "pnpm {}"
    elif (root / "yarn.lock").exists():
        setup, run = "yarn install --frozen-lockfile", "yarn {}"
    elif (root / "bun.lock").exists() or (root / "bun.lockb").exists():
        setup, run = "bun install --frozen-lockfile", "bun run {}"  # bare `bun test` is bun's own runner
    else:
        setup = "npm ci" if (root / "package-lock.json").exists() else "npm install"
        run = "npm run {}"
    found = {"setup": setup}
    for key, names in {"test": ("test",), "lint": ("lint",),
                       "typecheck": ("typecheck", "type-check"), "build": ("build",)}.items():
        script = next((n for n in names if n in scripts), None)
        if script is None or "no test specified" in str(scripts[script]):  # npm init's stub
            continue
        found[key] = "npm test" if run == "npm run {}" and script == "test" else run.format(script)
    return found


def _python_commands(root: Path, pyproject: str) -> dict[str, str]:
    """With uv.lock, `uv sync` + `uv run`. Without it no setup at all: anything else
    (pip, Poetry) would install into the user's own environment from every worktree."""
    uv = (root / "uv.lock").exists()
    prefix = "uv run " if uv else "python3 -m "
    found = {"setup": "uv sync"} if uv else {}
    found["test"] = f"{prefix}pytest"
    if "ruff" in pyproject:
        found["lint"] = f"{prefix}ruff check ."
    if "mypy" in pyproject:
        found["typecheck"] = f"{prefix}mypy ."
    elif "pyright" in pyproject:
        found["typecheck"] = f"{prefix}pyright"
    return found


def _python_without_uv(root: Path) -> bool:
    """The stack `detect_commands` reads as Python (no package.json) with no uv.lock."""
    return (not (root / "package.json").is_file() and (root / "pyproject.toml").is_file()
            and not (root / "uv.lock").exists())


def _make_commands(makefile: str) -> dict[str, str]:
    targets = set(re.findall(r"^([A-Za-z0-9_-]+)\s*:(?!=)", makefile, re.M))
    found = {}
    for key, names in MAKE_TARGETS.items():
        if (name := next((n for n in names if n in targets), None)) is not None:
            found[key] = f"make {name}"
    return found


def detect_commands(root: Path) -> dict[str, str]:
    """A proposed value per `commands:` key from the project's files; keys it cannot infer are absent."""
    found: dict[str, str] = {}
    if (package := _read(root, "package.json")) is not None:
        found = _node_commands(root, package)
    elif (pyproject := _read(root, "pyproject.toml")) is not None:
        found = _python_commands(root, pyproject)
    elif (root / "Cargo.toml").is_file():
        found = {"test": "cargo test", "lint": "cargo clippy -- -D warnings", "build": "cargo build"}
    elif (root / "go.mod").is_file():
        found = {"test": "go test ./...", "lint": "go vet ./...", "build": "go build ./..."}
    if (makefile := _read(root, "Makefile")) is not None:
        found.update(_make_commands(makefile))  # the project's own targets beat our guess
    return found


def _block_span(lines: list[str], start_key: str, end_key: str) -> tuple[int, int]:
    start = next(i for i, line in enumerate(lines) if line.startswith(start_key))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith(end_key))
    return start, end


def write_commands(text: str, commands: dict[str, str]) -> str:
    """The stamped config text with `commands:` replaced by `commands` and `deploy:` emptied;
    every other line, comments included, untouched."""
    lines = text.splitlines(keepends=True)
    start, end = _block_span(lines, "commands:", "deploy:")
    keep = [line for line in lines[start + 1:end] if line.lstrip().startswith("# test_timeout")]
    body = [f"  {yaml.safe_dump({k: v}, width=10**6).rstrip()}\n" for k, v in commands.items() if v]
    lines[start + 1:end] = body + keep + ["\n"]
    start, end = _block_span(lines, "deploy:", "auto_deploy:")
    lines[start:end] = ["deploy: []                           # run in order by the Deploy session; empty = skip\n\n"]
    return "".join(lines)


def _ask_commands(ask: Ask, say: Callable[[str], None], proposed: dict[str, str],
                  note: str | None = None) -> dict[str, str]:
    say("Commands lime runs in each Run's worktree. Enter keeps the proposal, '-' clears an optional one.")
    if note:
        say(note)
    answers = {}
    for key in COMMAND_KEYS:
        required = key == "test"
        default = proposed.get(key) or (PLACEHOLDER_TEST if required else "")
        shown = default or ("optional, Enter to skip" if not required else "")
        reply = ask(f"  {key} [{shown}]: ").strip()
        if reply == "-":
            answers[key] = default if required else ""
        else:
            answers[key] = reply or default
    if answers["test"] == PLACEHOLDER_TEST:
        say("  no test command found: wrote a placeholder; `lime check` and `lime run` refuse it"
            " until you set commands.test in .lime/config.yaml")
    return answers


# --- (b) accounts ---


def _sub(text: str, pattern: str, repl: str) -> str:
    new, count = re.subn(pattern, repl, text, count=1, flags=re.M)
    if count != 1:
        raise ValueError(f"config template has no line matching {pattern!r}")
    return new


def claude_only(text: str) -> str:
    """The stamped roster rewritten to what the all-claude profile resolves to, so a plain
    `lime run` needs only a Claude subscription. The config schema has no default-profile
    key, and `--profile` is applied per launch; the base roster is the only place left."""
    text = _sub(text, r"^  runtime: pi\b.*$",
                "  runtime: claude_code               # Claude subscription only (the all-claude profile, applied by lime init)")
    text = _sub(text, r"^(  model: )openrouter/deepseek/\S+", r"\1sonnet")
    text = _sub(text, r"^(    model: )openai-codex/\S+.*$", r"\1fable   # Claude-only roster")
    text = _sub(text, r"^(    model: )openrouter/moonshotai/\S+", r"\1opus")
    # the quality profile's builder would otherwise be claude_code on an OpenRouter model id
    return _sub(text, r"^(      - \{name: builder,\s+model: )openrouter/anthropic/[^}\s]+", r"\1sonnet")


# --- (c) pi compaction ---


def compaction_fix_command(path: Path) -> str:
    return (
        "python3 -c 'import json,pathlib as P;p=P.Path(\"" + str(path) + "\");"
        "p.parent.mkdir(parents=True,exist_ok=True);s=json.loads(p.read_text()) if p.exists() else {};"
        "s.setdefault(\"compaction\",{})[\"enabled\"]=False;p.write_text(json.dumps(s,indent=2))'"
    )


class NotAnObject(ValueError):
    """pi's settings file is JSON whose top level is not an object: nothing to merge into."""


def disable_compaction(path: Path) -> None:
    """Merge `compaction.enabled: false` into pi's settings JSON, keeping every other key;
    a `compaction` that is not an object is replaced. The new text goes to a temp file
    beside the symlink-resolved file and then replaces it, so an interrupt never leaves
    the file half-written and a dotfiles symlink stays a symlink."""
    target = path.resolve()
    settings = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    if not isinstance(settings, dict):
        raise NotAnObject("its top level is not a JSON object")
    compaction = settings.get("compaction")
    settings["compaction"] = {**(compaction if isinstance(compaction, dict) else {}), "enabled": False}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.lime-tmp")
    try:
        tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if target.exists():
            os.chmod(tmp, stat.S_IMODE(target.stat().st_mode))
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _compaction_step(ask: Ask, say: Callable[[str], None]) -> None:
    path = config.PI_SETTINGS
    if not config.compaction_failures():
        return
    say("\nlime refuses to run while pi auto-compaction is on: it would rewrite an agent's context mid-Run.")
    if ask(f"Set compaction.enabled=false in {path}? [Y/n]: ").strip().lower() in ("", "y", "yes"):
        try:
            disable_compaction(path)
        except (OSError, ValueError) as err:
            fix = (f"make the file {COMPACTION_OFF}" if isinstance(err, NotAnObject)  # as `lime check` says
                   else f"add {COMPACTION_OFF} to it")
            say(f"  could not update {path}: {err}\n  fix by hand: {fix}")
        else:
            say(f"  updated {path}")
    else:
        say(f"  not changed. Fix it yourself before `lime run`:\n  {compaction_fix_command(path)}")


# --- (d) first ticket ---


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "first-ticket"


def write_ticket(root: Path, number: int, title: str) -> Path:
    path = root / ".lime" / "tickets" / f"{number}-{slugify(title)}.md"
    path.write_text(TICKET.format(number=number, title=title))
    return path


def _ticket_step(root: Path, ask: Ask, say: Callable[[str], None]) -> Path | None:
    if not git_helper.is_repo():  # ticket numbers come from git: say so before asking anything
        say("\nNo starter ticket: this is not a git repository yet. After the steps below,"
            " `lime ticket next` numbers one.")
        return None
    if ask("\nCreate a starter ticket? [Y/n]: ").strip().lower() not in ("", "y", "yes"):
        return None
    title = ask("One-line title (what should the first Run change?): ").strip()
    if not title:
        say("  no title, no ticket. Later: `lime ticket next`, then write .lime/tickets/<N>-<slug>.md")
        return None
    try:
        number = numbering.next_number(root)
    except RuntimeError as err:
        say(f"  cannot number a ticket yet ({err}); later: `lime ticket next`")
        return None
    path = write_ticket(root, number, title)
    say(f"  wrote {path.relative_to(root)}\n"
        "  Before launching, fill in: Ask (what and why), Where (real paths, one backticked path per line,\n"
        "  the launch is refused without them), Done means, Out of scope. Replace every TODO.")
    return path


# --- (e) next steps ---


def _git_out(root: Path, *args: str) -> str | None:
    """A git command's stripped stdout in `root`; None when it fails."""
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def _to_commit(root: Path, owned: Sequence[str], repo: bool) -> list[str]:
    """The paths of `owned` git has something to commit for (untracked or changed; a path the
    project gitignores drops out by itself). Outside a repo, all of them."""
    if not repo:
        return list(owned)
    return [path for path in owned
            if _git_out(root, "status", "--porcelain", "--untracked-files=all", "--", path) != ""]


def _publish_step(root: Path, ticket: Path | None, owned: Sequence[str]) -> tuple[str, list[str]]:
    """Commit only the init-owned paths git has something to commit for (never the ticket,
    never what the user staged), give the repo an origin when it has none, and push the
    checked-out branch (unborn included). Runs start from origin's default branch."""
    repo = _git_out(root, "rev-parse", "--git-dir") is not None
    origin = repo and _git_out(root, "remote", "get-url", "origin") is not None
    branch = _git_out(root, "symbolic-ref", "--short", "-q", "HEAD") if repo else "main"
    start = _git_out(root, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD") if origin else None
    start = start.removeprefix("origin/") if start else None  # what `worktree.default_branch` reads
    paths = _to_commit(root, owned, repo)
    parts = [] if repo else ["make this directory a git repository"]
    commands = [] if repo else ["git init -b main"]
    if paths:
        parts.append("commit the files lime init stamped")
        listed = " ".join(paths)
        commands.append(f"git add {listed} && git commit -m 'chore: lime init' -- {listed}")
    if not origin:
        parts.append("create a private GitHub repository as origin")
        commands.append(CREATE_ORIGIN)
    if branch:
        parts.append("push")
        commands.append(f"git push -u origin {branch}")
    why = [] if paths or not repo else ["the files lime init stamped are already committed"]
    why.append("a Run starts from what origin has" + ("" if repo else ", so commit your own project files too"))
    if ticket:
        why.append("the ticket stays uncommitted, its Run commits it")
    if parts:
        what = f"{', '.join(parts[:-1])} and {parts[-1]}" if len(parts) > 1 else parts[0]
        what += f" ({'; '.join(why)})"
    else:  # a detached HEAD with an origin and nothing to commit
        what = f"{why[0]} ({'; '.join(why[1:])})"
    if not branch:
        what += "\n     No branch is checked out (detached HEAD): "
        if start and commands:
            what += (f"Runs start from origin/{start}, so switch to {start}, run the lines below there,"
                     f" then push {start} to origin.")
        elif start:
            what += (f"Runs start from origin/{start}, so the stamped files have to reach {start} on origin"
                     " before the first launch.")
        else:
            what += ("switch to the branch Runs should start from, "
                     + ("run the lines below there, then " if commands else "then ")
                     + "push that branch to origin.")
    elif start and start != branch:
        what += (f"\n     Runs start from origin/{start}, not from {branch}, so the stamped files have to reach"
                 f" {start} (merge {branch}, or open a pull request) before the first launch.")
    return what, commands


def next_steps(root: Path, ticket: Path | None, owned: Sequence[str]) -> str:
    """What to do after the wizard. Every command line pastes as-is into an interactive
    zsh (no comments, redirects or `&`); notes live on the description line above it."""
    number = ticket.name.split("-", 1)[0] if ticket else "N"
    if ticket:
        write = (f"edit {ticket.relative_to(root)} in your editor: fill in Ask, Where (one backticked path"
                 " per line; until then `lime check` is red and `lime run` refuses it), Done means and"
                 " Out of scope", [])
    else:
        write = ("write a ticket: `lime ticket next` prints its number N, then write"
                 " .lime/tickets/<N>-<slug>.md (README, First Run)", ["lime ticket next"])
    steps = [
        _publish_step(root, ticket, owned),
        write,
        ("validate", ["lime check"]),
        (f"launch the Run{'' if ticket else ' (N = the ticket number)'}: it runs for a long time, so keep"
         " this terminal open until it ends (closing it stops the Run); watch it from a second terminal"
         " with `lime trace --follow`", [f"lime run {number}"]),
        ("review the PR on GitHub", ["gh pr list"]),
        ("merge it", [f"lime deploy {number}"]),
    ]
    out = ["\nNext steps"]
    for i, (what, commands) in enumerate(steps, start=1):
        out.append(f"  {i}. {what}")
        out += [f"       {c}" for c in commands]
    return "\n".join(out)


# --- the wizard ---


def run(root: Path, config_created: bool, ask: Ask, say: Callable[[str], None] = print,
        owned: Sequence[str] = ()) -> bool:
    """Ask the setup questions in order. False when interrupted (Ctrl-C or EOF), after printing
    how to finish by hand. `owned`: every path lime init owns that exists, this run's or an
    earlier one's; the printed commit line keeps those git has something to commit for.
    The commands and roster answers land in one atomic config write, so an interrupt before
    it leaves the stamped template untouched."""
    try:
        cfg = root / ".lime" / "config.yaml"
        if config_created:
            proposed = detect_commands(root)
            note = NO_UV_SETUP if "setup" not in proposed and _python_without_uv(root) else None
            text = write_commands(cfg.read_text(), _ask_commands(ask, say, proposed, note))
            say("\nWhich agent logins do you have?\n"
                "  1) Claude subscription only\n"
                "  2) Claude + an OpenRouter key + a Codex login through pi (the stamped default roster)")
            while (choice := ask("Choice [1]: ").strip()) not in ("", "1", "2"):
                say("  answer 1 or 2")
            only_claude = choice != "2"
            config._atomic_write(cfg, claude_only(text) if only_claude else text)
            say("  wrote commands:, deploy: [] and the roster into .lime/config.yaml")
            say("  roster set to Claude only: a plain `lime run` needs no other login" if only_claude
                else "  roster kept; log in to both providers in pi, `lime check` verifies them")
        else:
            say("\n.lime/config.yaml already existed and is never modified: skipping the commands and"
                " accounts questions. Review its `commands:`, `deploy:` and roster by hand.")
        _compaction_step(ask, say)
        ticket = _ticket_step(root, ask, say) if config_created else None  # a restamp asks no ticket
        say(next_steps(root, ticket, owned))
        return True
    except (EOFError, KeyboardInterrupt):
        say("\n" + RESUME.format(again=AGAIN if config_created else ""))
        return False
