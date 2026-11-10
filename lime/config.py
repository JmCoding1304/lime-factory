"""Config load, merge, tool vocabulary, validate-before-spawn (spec §5) and the ticket template check (§10)."""

import difflib
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

from . import budget, quality
from .data_types import AgentConfig, DEFAULT_TIMEOUT_S

TEMPLATE = Path(__file__).parent / "templates" / "config.yaml"
PROMPTS_DIR = Path(__file__).parent / "prompts"
AGENTS_MD_LIMIT = 60  # lines; `lime check` fails above it (cookbook authors still target ≤40)
CONTEXT_ENTRY_LIMIT = 350  # characters per CONTEXT.md glossary entry; the document gate and `lime check` fail above it
HARNESS_DIR = Path(__file__).resolve().parent / "harness_engineering"
PI_AGENT_DIR = Path("~/.pi/agent").expanduser()
PI_SETTINGS = PI_AGENT_DIR / "settings.json"
PI_CATALOGS = ("models-store.json", "models.json")  # pi's refreshed cache, then the user's own file
# What `lime init`'s wizard writes when it finds no test command: validate refuses it before any agent runs.
PLACEHOLDER_TEST = 'echo "set commands.test in .lime/config.yaml" && exit 1'
PLACEHOLDER_TEST_FAILURE = (
    "commands.test is still the placeholder lime init wrote (it found no test command)"
    " — set your project's real test command in .lime/config.yaml"
)

THINKING = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
TOOL_CHOICES = ("auto", "required")
CC_ALIASES = ("haiku", "sonnet", "opus", "fable")
PROFILE_KEYS = {"runtime", "model", "thinking", "timeout_s", "tool_choice"}
WORKFLOWS = ("feature", "bug-fix", "greenfield", "front-end")
TICKET_TYPES = ("grilling", "research", "prototype", "task")
TICKET_SECTIONS = ("## Ask", "## Where", "## Done means", "## Out of scope")

# ONE vocabulary — pi's — everywhere; each runtime translates below.
CC_TOOLS = {
    "read": ["Read"], "bash": ["Bash"], "edit": ["Edit"], "write": ["Write"],
    "grep": ["Grep"], "find": ["Glob"],
    "ls": [],  # no Claude Code equivalent — dropped with a warning
    "subagent": ["Agent"], "web": ["WebSearch", "WebFetch"],
}
PI_EXTENSIONS = {"subagent": str(HARNESS_DIR / "subagents.ts"), "web": "pi-web-access"}
# pi's --tools allowlist filters extension-registered tools too, so the word must also enable the names it brings.
PI_EXTENSION_TOOLS = {"subagent": ["subagent_create", "subagent_continue", "subagent_list", "subagent_remove"]}

SUBMIT_TOOL = "submit"  # the envelope tool every pi role finishes with (ticket 74); Spine-added, not a vocabulary word
SUBMIT_EXTENSION = str(HARNESS_DIR / "submit.ts")
VOCABULARY = set(CC_TOOLS)


def config_path(root: Path = Path(".")) -> Path:
    project = root / ".lime" / "config.yaml"
    return project if project.exists() else TEMPLATE


def load(root: Path = Path(".")) -> dict:
    return yaml.safe_load(config_path(root).read_text())


def port_range(raw: dict) -> list[int]:
    """`runs.ports` ("3100-3102") -> [3100, 3101, 3102] — the parallel-Run cap (spec §7)."""
    raw_ports = (raw.get("runs") or {}).get("ports")
    if not raw_ports:
        return []
    lo, _, hi = str(raw_ports).partition("-")
    return list(range(int(lo), int(hi or lo) + 1))


# --- auto_deploy: what an unattended merge ships to (ticket 199) ---

AUTO_DEPLOY_TARGETS = ("off", "staging", "prod")


def auto_deploy(raw: dict | None) -> str | None:
    """What an unattended merge ships to (ticket 199); missing means off.

    A missing key, `None` or `False` (YAML bare `off`/`no`/`false`) -> `"off"`;
    a string in AUTO_DEPLOY_TARGETS -> that string; anything else (e.g. `True`
    from bare `on`/`yes`, `"Prod"`, `1`) -> `None` (invalid).
    """
    value = (raw or {}).get("auto_deploy")
    if value is None or value is False:
        return "off"
    if isinstance(value, str) and value in AUTO_DEPLOY_TARGETS:
        return value
    return None


# --- commit settings (used by both verify and commits, ticket 122) ---


def _commit_block(raw: dict) -> dict:
    """The `commit:` block or {} — never mutates the raw dict."""
    block = raw.get("commit")
    if not block or not isinstance(block, dict):
        return {}
    return dict(block)


def _pattern_list(raw_value: object, default: list[str]) -> list[str]:
    """Resolve a commit pattern list: explicit [] -> [] (no patterns);
    None/missing -> default; otherwise the list contents.
    Returns a fresh copy, never mutates the raw config."""
    if raw_value is None:
        return list(default)
    if isinstance(raw_value, list):
        return list(raw_value)
    return list(default)


def _max_added_lines(raw_lines: object, default: int) -> int:
    """Resolve max_added_lines: 0 is explicit 0 (allow zero additions);
    None/missing -> default."""
    if raw_lines is None:
        return default
    if isinstance(raw_lines, int) and not isinstance(raw_lines, bool):
        return raw_lines
    return default


def resolve_commit_settings(raw: dict) -> tuple[list[str], list[str], int]:
    """(exclude, uncounted, max_added_lines) resolved from the `commit:` block.

    Defaults: exclude=["context_handoff/"], uncounted=[], max_added_lines=1500.
    Returns fresh, independent lists/values, never mutates raw.
    """
    block = _commit_block(raw)
    exclude = _pattern_list(block.get("exclude"), ["context_handoff/"])
    uncounted = _pattern_list(block.get("uncounted"), [])
    budget = _max_added_lines(block.get("max_added_lines"), 1500)
    return exclude, uncounted, budget


def _pattern_failures(field: str, patterns: object) -> list[str]:
    """Shape + dangerous-entry checks for one pattern list field, e.g. `exclude`.

    None passes (missing/null list is allowed); a non-list or non-string entry
    fails once; each dangerous entry names its field and index.
    """
    if patterns is None:
        return []
    if not isinstance(patterns, list):
        return [f"commit.{field} must be a list, got {type(patterns).__name__}"]
    if any(not isinstance(e, str) for e in patterns):
        return [f"commit.{field} entries must be strings"]
    failures: list[str] = []
    for i, e in enumerate(patterns):
        # Reject empty strings, absolute paths, and pathspec magic
        if not e:
            failures.append(f"commit.{field}[{i}] is empty")
        elif e.startswith("/"):
            failures.append(f"commit.{field}[{i}] is an absolute path ({e!r}) — use worktree-root-relative")
        elif e.startswith(":"):
            failures.append(f"commit.{field}[{i}] starts with ':' ({e!r}) — pathspec magic not allowed")
        elif ".." in e.split("/"):
            failures.append(f"commit.{field}[{i}] contains '..' ({e!r}) — escaping patterns not allowed")
        elif e.startswith("^") or e.startswith("!"):
            failures.append(f"commit.{field}[{i}] starts with special char ({e!r}) — literal patterns only")
    return failures


def _validate_commit(raw: dict) -> list[str]:
    """Validate the commit block. Returns failure strings; empty list = pass."""
    block = raw.get("commit")
    if block is None:
        return []
    if not isinstance(block, dict):
        return [f"commit must be a mapping, got {type(block).__name__}"]
    failures: list[str] = []

    exclude_raw = block.get("exclude")
    uncounted_raw = block.get("uncounted")
    shape_failures = _pattern_failures("exclude", exclude_raw) + _pattern_failures("uncounted", uncounted_raw)
    failures.extend(shape_failures)
    if not shape_failures:
        # Exact duplicates across the resolved lists are ambiguous: a pattern is
        # either an accounting exemption or a staging exclusion, never both.
        exclude = _pattern_list(exclude_raw, ["context_handoff/"])
        for pattern in dict.fromkeys(_pattern_list(uncounted_raw, [])):
            if pattern in exclude:
                failures.append(
                    f"commit.uncounted {pattern!r} duplicates commit.exclude — a pattern may be"
                    " an accounting exemption or a staging exclusion, not both"
                )

    max_lines = block.get("max_added_lines")
    if max_lines is not None:
        if isinstance(max_lines, bool):
            failures.append(f"commit.max_added_lines must be a nonnegative integer, got bool")
        elif not isinstance(max_lines, int):
            failures.append(f"commit.max_added_lines must be a nonnegative integer, got {type(max_lines).__name__}")
        elif max_lines < 0:
            failures.append(f"commit.max_added_lines must be nonnegative, got {max_lines}")

    return failures


def merged_agents(raw: dict, profile: str | None = None) -> list[dict]:
    """defaults ← agent ← profile; a profile's own `defaults` patches the defaults layer."""
    profiles = raw.get("profiles") or {}
    if profile in (None, "default"):
        patch = {}
    elif profile in profiles:
        patch = profiles[profile] or {}
    else:
        raise ValueError(f"unknown profile {profile!r}")
    defaults = {**(raw.get("defaults") or {}), **(patch.get("defaults") or {})}
    by_name = {e.get("name"): e for e in patch.get("agents") or []}
    return [
        {**defaults, **entry,
         **{k: v for k, v in by_name.get(entry.get("name"), {}).items() if k != "name"}}
        for entry in raw.get("agents") or []
    ]


def roster(raw: dict, profile: str | None = None) -> list[AgentConfig]:
    return [AgentConfig(**entry) for entry in merged_agents(raw, profile)]


class ResolvedTools(NamedTuple):
    tools: list[str]  # runtime-native names
    extensions: list[str]  # pi -e words (subagent/web)
    warnings: list[str]


def resolve_tools(runtime: str, words: list[str]) -> ResolvedTools:
    unknown = [w for w in words if w not in VOCABULARY]
    if unknown:
        raise ValueError(f"unknown tool words {unknown} — the vocabulary is {sorted(VOCABULARY)}")
    if runtime == "pi":
        return ResolvedTools(
            [w for w in words if w not in PI_EXTENSIONS]
            + [t for w in words if w in PI_EXTENSION_TOOLS for t in PI_EXTENSION_TOOLS[w]],
            [PI_EXTENSIONS[w] for w in words if w in PI_EXTENSIONS],
            [],
        )
    warnings = ["`ls` has no Claude Code tool — dropped"] if "ls" in words else []
    return ResolvedTools([tool for w in words for tool in CC_TOOLS[w]], [], warnings)


def validate(
    raw: dict,
    roles: list[str] | None = None,
    profile: str | None = None,
    chain_tests: bool = True,
    root: Path = Path("."),
) -> list[str]:
    """The static §5 checks; every failure one line, empty list = go."""
    failures: list[str] = []
    # Root-scoped once per validation, before any early return can hide it.
    failures += _agents_md_failures(root)
    names = [e.get("name", "") for e in raw.get("agents") or []]
    if roles is None:
        roles = names
    failures += [f"agent {r!r} is not in the roster" for r in roles if r not in names]
    profiles = raw.get("profiles") or {}
    for pname, patch in profiles.items():
        failures += _profile_failures(pname, patch or {}, names)
    if profile not in (None, "default") and profile not in profiles:
        return failures + [f"profile {profile!r} is not in the config"]
    budget = raw.get("budget") or {}
    if budget.get("warn", 150_000) >= budget.get("stop", 250_000):
        failures.append("budget.warn must stay below budget.stop")
    failures += _validate_commit(raw)
    if auto_deploy(raw) is None:
        failures.append(f"auto_deploy must be off, staging or prod (got {raw.get('auto_deploy')!r})")
    test = (raw.get("commands") or {}).get("test")
    if chain_tests and not test:
        failures.append("commands.test is unset and the chain tests (§5: test is required)")
    elif chain_tests and test == PLACEHOLDER_TEST:
        failures.append(PLACEHOLDER_TEST_FAILURE)
    if (bad := quality.test_timeout_failure(raw.get("commands") or {})):
        failures.append(bad)
    for agent in merged_agents(raw, profile):
        if agent.get("name") in roles:
            failures += _agent_failures(agent, root)
    return failures


def _profile_failures(pname: str, patch: dict, names: list[str]) -> list[str]:
    failures = []
    for key in patch.get("defaults") or {}:
        if key not in PROFILE_KEYS:
            failures.append(
                f"profile {pname!r} patches defaults.{key} — profiles patch only runtime/model/thinking/timeout_s/tool_choice"
            )
    for entry in patch.get("agents") or []:
        for key in entry:
            if key not in PROFILE_KEYS | {"name"}:
                failures.append(
                    f"profile {pname!r} patches {entry.get('name', '?')}.{key}"
                    " — profiles patch only runtime/model/thinking/timeout_s/tool_choice"
                )
        if entry.get("name") not in names:
            failures.append(f"profile {pname!r} patches unknown agent {entry.get('name')!r}")
    return failures


def _agents_md_failures(root: Path) -> list[str]:
    """The one AGENTS.md ceiling check: over 60 lines is a diagnostic naming the file
    and the limit. Exactly 60 passes, a missing AGENTS.md passes, and CLAUDE.md is
    never measured. `splitlines()` counts blank lines; no rules text is truncated."""
    path = Path(root) / "AGENTS.md"
    if not path.exists():
        return []
    count = len(path.read_text(encoding="utf-8").splitlines())
    if count > AGENTS_MD_LIMIT:
        return [
            f"AGENTS.md is {count} lines — over the {AGENTS_MD_LIMIT}-line limit at {path}"
        ]
    return []


_GLOSSARY_ENTRY = re.compile(r"^- \*\*(.+?)\*\*")
_GLOSSARY_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_GLOSSARY_HEADING = re.compile(r"^#{1,6}(?:\s|$)")


def glossary_entries(text: str) -> list[tuple[str, str]]:
    """(term, entry_text) for each `- **Term**` entry, in file order."""
    entries: list[tuple[str, str]] = []
    term: str | None = None
    lines: list[str] = []

    def flush() -> None:
        nonlocal term, lines
        if term is not None:
            entries.append((term, "\n".join(lines)))
        term, lines = None, []

    for line in text.splitlines():
        match = _GLOSSARY_ENTRY.match(line)
        if match:
            flush()
            term, lines = match.group(1), [line]
        elif _GLOSSARY_BULLET.match(line) or _GLOSSARY_HEADING.match(line):
            flush()
        elif not line.strip():
            continue
        elif term is not None:
            lines.append(line)
    flush()
    return entries


def context_md_failures(root: Path) -> list[str]:
    """CONTEXT.md glossary entries over the per-entry character cap, one line each."""
    path = Path(root) / "CONTEXT.md"
    if not path.exists():
        return []
    failures = []
    for term, entry in glossary_entries(path.read_text(encoding="utf-8")):
        count = len(entry)
        if count > CONTEXT_ENTRY_LIMIT:
            failures.append(
                f"CONTEXT.md entry **{term}** is {count} characters — over the {CONTEXT_ENTRY_LIMIT}-character cap at {path}"
            )
    return failures


def _agent_failures(agent: dict, root: Path) -> list[str]:
    name = agent.get("name", "?")
    failures = []
    runtime, model = agent.get("runtime"), agent.get("model")
    if runtime not in ("pi", "claude_code"):
        failures.append(f"{name}: runtime {runtime!r} is neither pi nor claude_code")
    if not model:
        failures.append(f"{name}: no model resolves (defaults set none either)")
    elif runtime == "pi":
        provider = model.partition("/")[0]
        if "/" not in model:
            failures.append(f"{name}: pi model {model!r} needs the provider/model-id form")
        elif provider == "anthropic":
            failures.append(
                f"{name}: anthropic/* on pi would spend the subscription through a third-party"
                " harness — use openrouter/anthropic/*"
            )
    elif runtime == "claude_code" and model not in CC_ALIASES and not model.startswith("claude-"):
        failures.append(f"{name}: claude_code model {model!r} is no alias {CC_ALIASES} or claude-* id")
    timeout = agent.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        failures.append(f"{name}: timeout_s {timeout!r} must be a whole number of seconds, at least 1")
    if agent.get("thinking", "medium") not in THINKING:
        failures.append(f"{name}: thinking {agent.get('thinking')!r} is not one of {THINKING}")
    tool_choice = agent.get("tool_choice", "auto")
    if tool_choice not in TOOL_CHOICES:
        failures.append(f"{name}: tool_choice {tool_choice!r} is not one of {TOOL_CHOICES}")
    unknown = [w for w in agent.get("tools") or [] if w not in VOCABULARY]
    if unknown:
        failures.append(f"{name}: unknown tool words {unknown}")
    if runtime == "pi":
        failures += _extension_failures(agent, root)
    for kind in ("system", "user"):
        configured = (agent.get("prompt_engineering") or {}).get(kind)
        if configured:
            # A configured path is the only candidate — a package default never
            # rescues a missing override (mirrors prompts.resolve_path).
            selected = root / configured
            if not selected.exists():
                failures.append(
                    f"{name}: configured {kind} prompt missing at {selected}"
                )
        else:
            default = PROMPTS_DIR / name / f"{kind}.md"
            if not default.exists():
                failures.append(
                    f"{name}: {kind} prompt missing (unconfigured; no package default either)"
                )
    return failures


def is_extension_path(entry: str) -> bool:
    """Separate a path-shaped `PI_EXTENSIONS` value (which must exist on disk) from a bare known
    package name like pi-web-access (which pi resolves by name)."""
    return "/" in entry or entry.startswith((".", "~")) or entry.endswith((".ts", ".js", ".mjs", ".cjs"))


def _extension_path(entry: str) -> Path:
    try:
        return Path(entry).expanduser()
    except RuntimeError:  # `~nosuchuser/…` — no home to resolve
        return Path(entry)


def _packaged_extension(path: Path) -> Path | None:
    """A relative entry read from the installed package's parent instead of the project — kept only
    when it lands inside HARNESS_DIR, so the fallback never admits other files beside the package."""
    packaged = (HARNESS_DIR.parent.parent / path).resolve()
    return packaged if packaged.is_relative_to(HARNESS_DIR.resolve()) else None


def resolve_extension(entry: str, root: Path) -> str | None:
    """The `-e` value pi is handed for a harness_engineering entry; None = refused (validation and the
    launch share this). Absolute, or present under the project root: the entry unchanged. Relative and
    absent there: the same path inside the installed package's extensions, absolute (a stamped project
    reaching lime/harness_engineering/*.ts)."""
    if ":" in entry:  # `npm:`, `git:`, `https://`, `git@host:` are remote sources pi would fetch — never a local file
        return None
    path = _extension_path(entry)
    if (root / path).exists():  # an absolute path ignores root
        return entry
    packaged = None if path.is_absolute() else _packaged_extension(path)
    return str(packaged) if packaged and packaged.is_file() else None


def _extension_failures(agent: dict, root: Path) -> list[str]:
    """Allowlist (audit C1): pi installs and runs any `-e` source it is handed, so an entry
    passes only as a known package (a bare PI_EXTENSIONS value), a local path that exists,
    or one of the extensions the installed package ships (resolve_extension)."""
    name = agent.get("name", "?")
    entries = [(e, "harness_engineering") for e in agent.get("harness_engineering") or []]
    entries += [(PI_EXTENSIONS[w], f"tool word {w!r}") for w in agent.get("tools") or [] if w in PI_EXTENSIONS]
    failures = []
    for entry, source in entries:
        if entry in PI_EXTENSIONS.values() and not is_extension_path(entry):
            continue  # a known package pi resolves by name (pi-web-access)
        if resolve_extension(entry, root) is not None:
            continue
        path = _extension_path(entry)
        checked = str((root / path).resolve())
        packaged = None if ":" in entry or path.is_absolute() else _packaged_extension(path)
        if packaged:
            checked += f" and {packaged}"
        failures.append(
            f"{name}: pi extension {entry!r} ({source}) is neither a known extension"
            f" nor a local path that exists (checked {checked})"
        )
    return failures


# --- environment: credentials presence only, never read or stored ---


def claude_credentials_present() -> bool:
    if (Path.home() / ".claude" / ".credentials.json").exists():
        return True
    if sys.platform != "darwin":
        return False
    # macOS stores them in the Keychain instead (spec §5)
    probe = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials"],
        capture_output=True,
    )
    return probe.returncode == 0


def _pi_auth_status(provider: str) -> str:
    try:
        probe = subprocess.run(
            ["pi", "auth", "check", "--provider", provider, "--json"],
            capture_output=True, text=True, timeout=30,
        )
    except OSError:
        return "pi is not installed"
    except subprocess.TimeoutExpired:
        return "pi auth check timed out"
    try:
        return json.loads(probe.stdout).get("status", "unknown")
    except ValueError:
        return f"unreadable pi auth output (exit {probe.returncode})"


def credential_failures(agents: list[AgentConfig]) -> tuple[list[str], list[str]]:
    failures, warnings = [], []
    for provider in sorted({a.model.partition("/")[0] for a in agents if a.runtime == "pi"}):
        status = _pi_auth_status(provider)
        if status != "ready":
            failures.append(f"pi credentials for {provider!r}: {status}")
    if any(a.runtime == "claude_code" for a in agents) and not claude_credentials_present():
        failures.append(
            "Claude Code credentials: no ~/.claude/.credentials.json and no Keychain item"
        )
    if os.environ.get("ANTHROPIC_API_KEY"):
        warnings.append(
            "ANTHROPIC_API_KEY is set — claude_code would silently bill the API,"
            " not the subscription"
        )
    return failures, warnings


def compaction_failures() -> list[str]:
    # pi auto-compacts at window−16k with no CLI flag (assets/02 §4) — settings must turn it off
    try:
        settings = json.loads(PI_SETTINGS.read_text())
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        return [f'pi settings at {PI_SETTINGS} are not a JSON object — make the file {{"compaction": {{"enabled": false}}}}']
    compaction = settings.get("compaction")
    if compaction is not None and not isinstance(compaction, dict):
        return [f'"compaction" in {PI_SETTINGS} is not a JSON object — set it to {{"enabled": false}}']
    if (compaction or {}).get("enabled") is False:
        return []
    return [f'pi compaction is on — add {{"compaction": {{"enabled": false}}}} to {PI_SETTINGS}']


# --- pi's model catalog: the (model, thinking, image, window) tuple (ticket 84) ---


def pi_catalog() -> dict[str, list[dict]] | None:
    """provider -> model entries from models-store.json (`{provider: {models}}`) and
    models.json (`{"providers": {provider: {models}}}`, pi's ModelsConfigSchema). None = neither file readable."""
    catalog: dict[str, list[dict]] = {}
    found = False
    for name in PI_CATALOGS:
        try:
            data = json.loads((PI_AGENT_DIR / name).read_text())
        except (OSError, ValueError):
            continue
        found = True
        providers = data.get("providers", data) if isinstance(data, dict) else {}
        for provider, block in providers.items():
            if isinstance(block, dict):
                catalog.setdefault(provider, []).extend(m for m in block.get("models") or [] if isinstance(m, dict))
    return catalog if found else None


def pi_model_entry(model: str, catalog: dict[str, list[dict]] | None = None) -> dict | None:
    """The catalog row for `provider/model-id`; None when pi has never heard of it (or has no catalog)."""
    provider, _, model_id = model.partition("/")
    catalog = pi_catalog() if catalog is None else catalog
    return next((e for e in (catalog or {}).get(provider, []) if e.get("id") == model_id), None)


def supported_thinking(entry: dict) -> tuple[str, ...]:
    """pi-ai getSupportedThinkingLevels: a non-reasoning model thinks `off` only; a null in
    thinkingLevelMap disables a level; xhigh/max need an explicit mapping."""
    if not entry.get("reasoning"):
        return ("off",)
    mapping = entry.get("thinkingLevelMap") or {}
    return tuple(
        level for level in THINKING
        if mapping.get(level, "unset") is not None and (level not in ("xhigh", "max") or level in mapping)
    )


def thinking_support(catalog: dict[str, list[dict]] | None = None) -> dict[str, list[str]]:
    """`provider/id` -> the levels supported_thinking() allows, for every model pi knows — the
    table the dashboard clamps and validates against (ticket 91). {} when pi has no catalog."""
    catalog = pi_catalog() if catalog is None else catalog
    return {
        f"{provider}/{e['id']}": list(supported_thinking(e))
        for provider, entries in (catalog or {}).items()
        for e in entries if e.get("id")
    }


def _num(value: object) -> float | None:
    """A finite number, else None — pi leaves a field out rather than nulling it."""
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def catalog_models(catalog: dict[str, list[dict]] | None = None) -> list[dict]:
    """Every model pi can actually run, as the dashboard's model-tray rows (ticket 92):
    `id` is the `provider/model-id` a config takes, prices are pi's own cost table in
    USD per million tokens. `provider` is the label, so an OpenRouter id keeps its
    family (`openai`, not `openrouter`). [] when pi has no catalog."""
    catalog = pi_catalog() if catalog is None else catalog
    rows = []
    for provider, entries in (catalog or {}).items():
        for e in entries:
            mid = e.get("id")
            if not mid:
                continue
            cost = e.get("cost") or {}
            window = _num(e.get("contextWindow"))
            rows.append({
                "id": f"{provider}/{mid}",
                "name": e.get("name") or mid,
                "provider": mid.split("/")[0] if "/" in mid else provider,
                "prompt_price": _num(cost.get("input")),
                "completion_price": _num(cost.get("output")),
                "context_length": None if window is None else int(window),
            })
    return rows


def capability_failures(agents: list[AgentConfig]) -> tuple[list[str], list[str]]:
    """(failures, warnings) for the pi roles: unknown model id and unsupported thinking level fail;
    a missing catalog or missing contextWindow warns."""
    pi_agents = [a for a in agents if a.runtime == "pi"]
    if not pi_agents:
        return [], []
    catalog = pi_catalog()
    if catalog is None:
        return [], [f"pi model catalog missing ({PI_AGENT_DIR}/models-store.json or models.json)"
                    " \u2014 model ids, thinking levels and context windows are unchecked until pi has run once"]
    known = [f"{p}/{e.get('id')}" for p, entries in catalog.items() for e in entries]
    failures, warnings = [], []
    for a in pi_agents:
        entry = pi_model_entry(a.model, catalog)
        if entry is None:
            close = difflib.get_close_matches(a.model, known, n=3)
            hint = f" \u2014 did you mean {', '.join(close)}?" if close else " \u2014 a typo'd id only fails mid-run (404)"
            failures.append(f"{a.name}: pi model {a.model!r} is not in pi's catalog ({PI_AGENT_DIR}){hint}")
            continue
        supported = supported_thinking(entry)
        if a.thinking not in supported:
            failures.append(f"{a.name}: thinking {a.thinking!r} is not supported by {a.model} \u2014 supported: {supported}")
        if not entry.get("contextWindow"):
            warnings.append(f"{a.name}: {a.model} has no contextWindow in pi's catalog \u2014 the budget backstop"
                            f" falls back to {budget.STOP + budget.RESERVE:,} tokens")
    return failures, warnings


def preflight_warnings(raw: dict, roles: list[str], profile: str | None = None) -> list[str]:
    """The advisory half of the preflight \u2014 `lime run`/`--resume` print these, failures come from validate_before_spawn."""
    try:
        agents = [a for a in roster(raw, profile) if a.name in roles]
    except (ValueError, TypeError):
        return []
    return capability_failures(agents)[1]


def validate_before_spawn(
    raw: dict,
    roles: list[str],
    profile: str | None = None,
    chain_tests: bool = True,
    root: Path = Path("."),
) -> list[str]:
    """Any failure = no Run, nothing left behind (spec §5); ticket 84 adds the catalog tuple."""
    failures = validate(raw, roles, profile, chain_tests, root)
    try:
        agents = [a for a in roster(raw, profile) if a.name in roles]
    except (ValueError, TypeError):
        return failures
    # offline and cheap first
    failures += capability_failures(agents)[0]
    cred, _ = credential_failures(agents)
    failures += cred
    # unconditional: "no auto-compaction on either runtime, ever" (spec §1)
    failures += compaction_failures()
    return failures


# --- write path: canonicalize, validate, span-preserving rewrite (ticket 87) ---

TOP_LEVEL_KEY = re.compile(r"^[A-Za-z0-9_-]+:")


def canonical_profile_patch(agent_entry: dict, base_merged: dict) -> dict:
    """`name` plus only the PROFILE_KEYS whose value differs from the base merged
    entry (defaults ← agent). Unknown keys are kept — they must fail validation
    loudly, never be silently dropped."""
    patch = {"name": agent_entry.get("name")}
    for key, value in agent_entry.items():
        if key == "name":
            continue
        if key in PROFILE_KEYS and value == base_merged.get(key):
            continue
        patch[key] = value
    return patch


def canonicalize_profiles(agents: list[dict], profiles: dict, defaults: dict) -> dict:
    """Rebuild the profiles patch map from the payload: per profile, per agent
    entry `name` plus only the diffs against the base merged roster survive. A
    name-only entry is kept — it says the role is in the profile, override or
    not (ticket 89). An empty profile is kept as `{}` — the dashboard's Create
    sends `{agents: []}` and the profile must survive its first save (ticket 92);
    a profile leaves the file only when the payload no longer names it.
    Keyed by (profile, name) — duplicates collapse, later entries win. An entry
    naming an unknown agent raises (surfaced as a validation error, file
    untouched)."""
    base = {e.get("name"): e for e in merged_agents({"defaults": defaults or {}, "agents": agents or []})}
    canonical: dict[str, dict] = {}
    for pname, patch in (profiles or {}).items():
        patch = patch or {}
        by_name: dict[str, dict] = {}
        for entry in patch.get("agents") or []:
            name = (entry or {}).get("name")
            if name not in base:
                raise ValueError(f"profile {pname!r} patches unknown agent {name!r}")
            by_name[name] = canonical_profile_patch(entry, base[name])
        defaults_patch = {
            k: v for k, v in (patch.get("defaults") or {}).items()
            if not (k in PROFILE_KEYS and v == (defaults or {}).get(k))
        }
        block: dict = {}
        if defaults_patch:
            block["defaults"] = defaults_patch
        if by_name:
            block["agents"] = list(by_name.values())
        canonical[pname] = block  # `{}` for an empty profile — the key is membership; only its absence deletes
    return canonical


def _render_span(key: str, value) -> str:
    return yaml.safe_dump({key: value}, sort_keys=False)


def _top_level_spans(lines: list[str]) -> dict[str, tuple[int, int]]:
    """(start, end-exclusive) line indexes per top-level `key:` block — a block
    ends at the next `^[A-Za-z0-9_-]+:` line or EOF."""
    spans: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(lines):
        if TOP_LEVEL_KEY.match(lines[i]):
            key = lines[i].split(":", 1)[0]
            j = i + 1
            while j < len(lines) and not TOP_LEVEL_KEY.match(lines[j]):
                j += 1
            spans[key] = (i, j)
            i = j
        else:
            i += 1
    return spans


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def write_profiles(root: Path, agents: list[dict], profiles: dict) -> tuple[bool, list[str]]:
    """Validate the full candidate config, then splice the rendered `agents:` and
    `profiles:` spans over the on-disk ones — every other line (comments,
    commands, budget, runs, deploy) stays byte-identical. A canonical no-op (both
    rendered spans already on disk) returns without touching the file, so a
    repeated PUT leaves the mtime — and `git diff` — empty. Static validation only
    (`validate`, never `validate_before_spawn`): credentials, catalog and
    compaction checks are environment-specific and must not block a save."""
    path = root / ".lime" / "config.yaml"
    raw = load(root)
    try:
        canonical = canonicalize_profiles(agents, profiles, raw.get("defaults") or {})
    except ValueError as error:
        return False, [str(error)]
    by_name: dict = {}
    for entry in agents or []:
        by_name[(entry or {}).get("name")] = entry
    candidate = {**raw, "agents": list(by_name.values()), "profiles": canonical}
    errors = validate(candidate, root=root)
    if errors:
        return False, errors

    agents_span = _render_span("agents", candidate["agents"])
    profiles_span = _render_span("profiles", canonical)
    full_dump = yaml.safe_dump(candidate, sort_keys=False)
    if not path.exists():
        _atomic_write(path, full_dump)
        return True, []
    lines = path.read_text().splitlines(keepends=True)
    spans = _top_level_spans(lines)
    if "agents" not in spans:
        _atomic_write(path, full_dump)
        return True, []
    a_start, a_end = spans["agents"]
    if "profiles" in spans:
        p_start, p_end = spans["profiles"]
        middle = "".join(lines[a_end:p_start])
        on_disk_profiles = "".join(lines[p_start:p_end])
        tail = "".join(lines[p_end:])
    else:
        middle, on_disk_profiles, tail = "", "", "".join(lines[a_end:])
    on_disk_agents = "".join(lines[a_start:a_end])
    agents_noop = agents_span == on_disk_agents
    profiles_noop = profiles_span == on_disk_profiles or (not canonical and "profiles" not in spans)
    if agents_noop and profiles_noop:
        return True, []  # nothing to change — the file is not touched at all
    if "profiles" in spans:
        profiles_out = on_disk_profiles if profiles_noop else profiles_span
        new_text = "".join(lines[:a_start]) + agents_span + middle + profiles_out + tail
    else:
        new_text = ("".join(lines[:a_start]) + agents_span
                    + (profiles_span if canonical else "") + tail)
    _atomic_write(path, new_text)
    return True, []


def merge_profiles(root: Path, changes: list[dict]) -> tuple[bool, bool, list[str]]:
    """Apply a list of profile model changes as one transaction.

    Each change ``{profile, role, patch}`` where ``patch`` keys are only
    ``model``, ``runtime``, and optional ``thinking``.  Returns
    ``(ok, wrote, errors)`` — ``wrote`` is ``True`` when the file was actually
    changed (vs a canonical no-op).

    * ``profile="default", role=<name>`` — modify the base agent's entry in the
      ``agents:`` block.
    * ``profile=<name>, role=<name>`` — upsert into the profile's ``agents``
      list; the profile is created if absent.
    * ``profile=<name>, role=null`` — merge into the profile's ``defaults``
      block.
    """
    errors: list[str] = []

    if not changes:
        return False, False, ["changes list must not be empty"]

    # ---- structural validation before any load or write ----
    for i, change in enumerate(changes):
        profile_name = change.get("profile")
        role = change.get("role")
        patch = change.get("patch")

        if not profile_name or not isinstance(profile_name, str):
            errors.append(f"change {i}: profile must be a non-empty string")
        if role is not None and (not isinstance(role, str) or not role):
            errors.append(f"change {i}: role must be a non-empty string or null")
        if role is None and profile_name == "default":
            errors.append(f"change {i}: null role is invalid for profile 'default'")
        if not isinstance(patch, dict):
            errors.append(f"change {i}: patch must be an object")
        else:
            invalid = set(patch) - {"model", "runtime", "thinking"}
            if invalid:
                errors.append(f"change {i}: invalid patch keys: {', '.join(sorted(invalid))}")
            model = patch.get("model")
            if not model or not isinstance(model, str):
                errors.append(f"change {i}: patch.model must be a non-empty string")
            runtime = patch.get("runtime")
            if not runtime or not isinstance(runtime, str):
                errors.append(f"change {i}: patch.runtime must be a non-empty string")
            if runtime not in ("pi", "claude_code"):
                errors.append(f"change {i}: patch.runtime must be 'pi' or 'claude_code'")
            if "thinking" in patch:
                if patch["thinking"] not in THINKING:
                    errors.append(f"change {i}: patch.thinking must be one of {THINKING}")

    if errors:
        return False, False, errors

    # ---- load current config ----
    raw = load(root)
    agents = [dict(a) for a in (raw.get("agents") or [])]
    profiles_dict: dict = {k: (dict(v) if v else {}) for k, v in (raw.get("profiles") or {}).items()}
    defaults = dict(raw.get("defaults") or {})

    # Build the base roster by name
    base = {e.get("name"): e for e in merged_agents(raw)}

    # ---- apply changes in-memory ----
    for change in changes:
        profile_name = change["profile"]
        role = change.get("role")
        patch = change["patch"]

        if profile_name == "default":
            # Modify the base agents list directly
            found = False
            for entry in agents:
                if entry.get("name") == role:
                    for key in ("model", "runtime", "thinking"):
                        if key in patch:
                            entry[key] = patch[key]
                    found = True
                    break
            if not found:
                errors.append(f"role {role!r} is not in the base roster")
        else:
            # Named profile
            if profile_name not in profiles_dict:
                profiles_dict[profile_name] = {}
            target_profile = profiles_dict[profile_name]
            if target_profile is None:
                target_profile = {}
                profiles_dict[profile_name] = target_profile

            if role is None:
                # Merge into profile's defaults block
                defaults_block = dict(target_profile.get("defaults") or {})
                for key in ("model", "runtime", "thinking"):
                    if key in patch:
                        defaults_block[key] = patch[key]
                target_profile["defaults"] = defaults_block
            else:
                # Validate role exists in base roster
                if role not in base:
                    errors.append(f"change: role {role!r} is not in the base roster")
                    continue

                # Upsert the agent entry in the profile's agents list
                agents_list = list(target_profile.get("agents") or [])
                found = False
                for entry in agents_list:
                    if entry.get("name") == role:
                        for key in ("model", "runtime", "thinking"):
                            if key in patch:
                                entry[key] = patch[key]
                        found = True
                        break

                if not found:
                    new_entry: dict = {"name": role}
                    for key in ("model", "runtime", "thinking"):
                        if key in patch:
                            new_entry[key] = patch[key]
                    agents_list.append(new_entry)

                target_profile["agents"] = agents_list

    if errors:
        return False, False, errors

    # ---- validate affected resolved agents before writing ----
    # Build a merged view of the affected roles to check model/runtime/thinking
    try:
        canonical = canonicalize_profiles(agents, profiles_dict, defaults)
    except ValueError as error:
        return False, False, [str(error)]

    by_name: dict = {}
    for entry in agents or []:
        by_name[(entry or {}).get("name")] = entry
    candidate = {**raw, "agents": list(by_name.values()), "profiles": canonical}

    # Run the standard validation first
    validation_errors = validate(candidate, root=root)
    if validation_errors:
        return False, False, validation_errors

    # ---- write via the existing atomic path ----
    path = root / ".lime" / "config.yaml"
    before = path.read_bytes() if path.exists() else None

    ok, write_errors = write_profiles(root, agents, profiles_dict)
    if not ok:
        return False, False, write_errors

    wrote = before is None or path.read_bytes() != before
    return True, wrote, errors


# --- ticket/map template check (spec §10 header) ---


def ticket_failures(root: Path = Path(".")) -> list[str]:
    failures = []
    files = sorted((root / ".lime" / "tickets").glob("*.md"))
    files += sorted((root / ".lime" / "maps").glob("*.md"))
    for path in files:
        lines = path.read_text().splitlines()

        def field(key: str) -> str | None:
            return next(
                (line[len(key) + 1 :].strip() for line in lines if line.startswith(key + ":")),
                None,
            )

        status = field("Status")
        if status is None or not status.startswith(("open", "claimed", "closed")):
            failures.append(f"{path.name}: Status must be open | claimed (who, date) | closed")
        if path.parent.name != "tickets":
            continue
        workflow, kind = field("Workflow"), field("Type")
        if workflow is not None:
            if workflow not in WORKFLOWS:
                failures.append(f"{path.name}: Workflow {workflow!r} is not one of {WORKFLOWS}")
            missing = [s for s in TICKET_SECTIONS if s not in lines]
            if missing:
                failures.append(f"{path.name}: missing {', '.join(missing)}")
        elif kind is None:
            failures.append(f"{path.name}: neither Workflow: nor Type: — kind is field presence")
        elif kind not in TICKET_TYPES:
            failures.append(f"{path.name}: Type {kind!r} is not one of {TICKET_TYPES}")
        if len(lines) > 80:
            failures.append(f"{path.name}: over one screen ({len(lines)} lines > 80)")
    return failures
