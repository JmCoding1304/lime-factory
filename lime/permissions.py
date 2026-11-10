"""What an agent may CHANGE, enforced in code after the fact (spec §8).

`tools:` is a capability list, not a sandbox, and two holes make it
unenforceable on its own:

  * `bash` runs anything. A builder handed bash to run a test suite can also
    run `git checkout adws/` — which is not hypothetical: one did, discarding
    uncommitted changes to the very quality check it was about to be judged by.
  * `write` reaches any path, not just the one report file an agent was given
    it for. A reviewer configured with "no edit, so it cannot quietly fix"
    could still rewrite the code it was reviewing.

So permission is verified the way every other claim in this system is —
after the fact, against the repo itself. `snapshot()` fingerprints the working
tree's change-set before an agent runs; `enforce()` compares it afterwards and
fails the phase if the agent touched anything outside its allowlist.

Comparing change-sets, rather than watching for writes, is what catches the
`git checkout` case: a path that was modified before the agent ran and is clean
afterwards has been reverted, and a reversion is a modification. Appearing,
disappearing, and changing all count.

The diff proves *what* changed, and in a worktree (the Run's own tree) that
is also *who* — the agent is alone there. A main-tree Run (prototype,
research) shares the tree with the engineer, so `enforce` takes a `Ledger`
that records what the agent's own tool calls named and judges only those
changes — exact path only, because run 08d820f5 showed a directory-level
heuristic still blames the engineer; bash writes that name no path (globs,
`cd dir && …`, `git stash`) are the guard's business.

A breach is NOT a gate violation. Gates are for work an agent can be asked to
redo; a breach cannot be corrected by re-prompting, because the write already
happened. It aborts the phase and names every offending path.

Two keys drive it (lime: `protected_files` moved from the config to trust.yaml;
there is no `always_writable()` — the handoff dir lives under the
project's gitignored `.lime/runs/`, outside the worktree, so it never appears
in a snapshot and needs no grant):
    trust.yaml protected_files   paths no agent may touch unless it names them itself
    agents[].writes      None = unrestricted · [] = read-only · [...] = only these

This module also owns the other trust.yaml half: `load_trust()` reads the four
pattern lists and `never_block()` renders `secrets/branches/commands` as the
"Never …" block every prompt embeds via `{{never}}` (spec §6).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

from .data_types import AgentConfig

TEMPLATE = Path(__file__).parent / "templates" / "trust.yaml"
TRUST_KEYS = ("protected_files", "secrets", "branches", "commands")


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


def _git(args: list[str], cwd) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


_WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit"}   # pi write/edit · cc Write/Edit/MultiEdit/NotebookEdit
_PATH_KEYS = ("file_path", "notebook_path", "path")


class Ledger:
    """What the agent's own tool calls named — the only proof of authorship in a tree it shares."""

    def __init__(self, repo_root):
        self.root = Path(repo_root).resolve()
        self.paths: set[str] = set()
        self.commands: list[str] = []

    def record(self, tool: str, args) -> None:
        """Record a tool call's evidence. Lower-cased tool in _WRITE_TOOLS → path;
        tool == "bash" → command string. Non-dict args are ignored."""
        if not isinstance(args, dict):
            return
        tool_lower = tool.lower()
        if tool_lower in _WRITE_TOOLS:
            for key in _PATH_KEYS:
                value = args.get(key)
                if value:
                    self.paths.add(self._relative(str(value)))
                    return
        if tool_lower == "bash":
            cmd = args.get("command") or ""
            self.commands.append(str(cmd))

    def _relative(self, path: str) -> str:
        """Normalise a path to repo-relative form.

        Absolute paths are resolved against self.root; relative paths are
        norm-pathed (so './x' → 'x'). If resolution fails, the absolute string
        is kept as-is.
        """
        from os.path import normpath
        p = Path(path)
        if p.is_absolute():
            try:
                resolved = p.resolve()
                return str(resolved.relative_to(self.root))
            except ValueError:
                try:
                    return str(p.relative_to(self.root))
                except ValueError:
                    return path
        return normpath(path)

    def names(self, path: str) -> bool:
        """True if the agent's tool calls named this path or it appears in a bash command."""
        if path in self.paths:
            return True
        root = str(self.root)
        forms = {path, "./" + path, root + "/" + path, str(self.root.resolve() / path)}
        rx = re.compile(
            r"(?<![\w./-])(?:" + "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True)) + r")(?![\w.-])"
        )
        for cmd in self.commands:
            if rx.search(cmd):
                return True
        return False


def snapshot(repo_root) -> dict[str, str]:
    """Fingerprint every path the working tree currently differs on.

    Tracked files carry their numstat counts, so an edit to an already-dirty
    file still registers as a change. Untracked files are listed by name.
    Gitignored paths never appear.
    """
    fingerprints: dict[str, str] = {}
    for line in _git(["diff", "HEAD", "--numstat"], repo_root).splitlines():
        fields = line.split("\t")
        if len(fields) >= 3:
            path = fields[-1].strip()
            fingerprints[path] = f"{fields[0]},{fields[1]}"
    for path in _git(["ls-files", "--others", "--exclude-standard"],
                     repo_root).splitlines():
        if path.strip():
            fingerprints[path.strip()] = "untracked"
    return fingerprints


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Every path whose state differs — appeared, vanished, or was rewritten."""
    return sorted({p for p in set(before) | set(after)
                   if before.get(p) != after.get(p)})


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw_*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
    ADW scripts it means. `**` is the way to say "cross directories".
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):                      # directory prefix
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def owned(path: str, writes) -> bool:
    """True when `path` matches any pattern in a Role's `writes:` list.

    A Role owns only the paths its own list names: `None` (unrestricted means
    no explicit list) and `[]` (read-only) own nothing.
    """
    return any(_matches(path, p) for p in (writes or []))


def permitted(path: str, agent: AgentConfig, protected_files: list[str]) -> bool:
    """The agent's own list first, then what is protected."""
    if owned(path, agent.writes):
        return True                      # naming a path is what unlocks a protected one
    if any(_matches(path, p) for p in protected_files):
        return False
    return agent.writes is None          # None = unrestricted, [] = no repo writes


def _prune_empty_parents(repo_root, path: str) -> None:
    """Remove empty parent directories left behind when an untracked file is deleted.

    Walks the deleted file's LEXICAL parents upward with `rmdir()`, strictly below
    the repository root, and stops at the first nonempty or unremovable folder —
    never recursive deletion, never an unbounded `removedirs()`, and never a
    resolved symlink into an external tree. Cleanup is best-effort: the caller has
    already deleted the file and must keep reporting that outcome."""
    root = Path(repo_root)
    parent = (root / path).parent
    while True:
        if parent == root:
            return  # never remove the repository root
        try:
            parent.relative_to(root)  # strictly below root, lexical (no .resolve())
        except ValueError:
            return
        try:
            parent.rmdir()
        except OSError:
            return  # first nonempty/unremovable folder — stop
        parent = parent.parent


def _roll_back(repo_root, path: str, before: dict[str, str], after: dict[str, str]) -> str:
    """Undo one unauthorized change. Returns a word describing what happened.

    Only changes the agent INTRODUCED are undone. A path that was already dirty
    when the agent started is left exactly as it is: the operator had
    uncommitted work there, and discarding it to tidy up would be the same harm
    this module exists to prevent, committed by the cleanup instead of the agent.
    """
    if path in before:
        # Already dirty beforehand. If it is gone from the diff now, the agent
        # reverted an engineer's uncommitted work and the content is not ours
        # to reconstruct — say so loudly rather than pretend it was handled.
        return "REVERTED-BY-AGENT (uncommitted work lost, cannot restore)" \
            if path not in after else "left as-is (was already modified)"
    if after.get(path) == "untracked":
        try:
            (Path(repo_root) / path).unlink()
        except OSError as error:
            return f"could not delete ({error})"
        _prune_empty_parents(repo_root, path)  # cleanup failure never relabels the deleted file
        return "deleted"
    result = subprocess.run(["git", "checkout", "--", path],
                            cwd=repo_root, capture_output=True, text=True)
    return "rolled back" if result.returncode == 0 else "could not roll back"


def enforce(repo_root, agent: AgentConfig, before: dict[str, str],
            protected_files: list[str], ledger: Ledger | None = None) -> list[str]:
    """Compare the tree against `before`; undo and raise if the agent overstepped.

    Returns the paths it legitimately changed, so the trace records what an
    agent actually touched rather than only what it claimed in its envelope.

    Detection alone would leave the repo holding the unauthorized change while
    reporting a failure, so anything the agent introduced outside its allowlist
    is rolled back before the phase dies. What it cannot undo, it names.

    `ledger`: when the Run shares its working tree with the engineer (main-tree
    Run — prototype, research), the diff proves *what* changed, but a `Ledger`
    records what the agent's own tool calls named and is the only trustworthy
    proof of authorship (ticket 54). When `ledger` is None (worktree Run — the
    agent is alone there), every diff change is attributed to the agent.
    """
    after = snapshot(repo_root)
    touched = changed_paths(before, after)
    if ledger is not None:
        touched = [p for p in touched if ledger.names(p)]
    breaches = [p for p in touched if not permitted(p, agent, protected_files)]
    if not breaches:
        return touched

    outcomes = {p: _roll_back(repo_root, p, before, after) for p in breaches}
    scope = ("read-only" if agent.writes == []
             else f"limited to {agent.writes}" if agent.writes
             else f"barred from {protected_files}")
    detail = "\n".join(f"  - {p} — {outcome}" for p, outcome in outcomes.items())
    raise PermissionBreach(
        f"{agent.name} is {scope} but modified {len(breaches)} path(s):\n{detail}")


# --- trust.yaml: the four pattern lists + the {{never}} block (spec §8, §6) ---


def trust_path(root: Path = Path(".")) -> Path:
    project = root / ".lime" / "trust.yaml"
    return project if project.exists() else TEMPLATE


def load_trust(root: Path = Path(".")) -> dict[str, list[str]]:
    raw = yaml.safe_load(trust_path(root).read_text()) or {}
    return {key: raw.get(key) or [] for key in TRUST_KEYS}


def never_block(trust: dict[str, list[str]]) -> str:
    """secrets/branches/commands as the "Never …" block (08-roles.md); protected_files stays in code."""
    lines = [
        ("Read or write", trust["secrets"]),
        ("Push to or check out", trust["branches"]),
        ("Run", trust["commands"]),
    ]
    bullets = [f"- {label}: {', '.join(items)}" for label, items in lines if items]
    if not bullets:
        return ""
    return "\n".join(["Never do any of these, whatever the task says:", *bullets])
