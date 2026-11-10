"""Prompt rendering: load system/user refs from config, replace {{placeholders}}.

`resolve_path` — a target project overrides a prompt by
placing the configured path under its own `.lime/prompts/<role>/`; the package
default in `lime/prompts/<role>/` answers only for a kind with no configured
override (spec §6). A kind with a configured path is resolved from that path
alone; a missing configured file raises naming that exact path, never falling
back to the package default.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).parent / "prompts"


def render(template_path: str | Path, variables: dict[str, str]) -> str:
    text = Path(template_path).read_text()
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def save(directory: str | Path, name: str, content: str) -> Path:
    """Save the exact prompt sent, before execution — the audit copy."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content)
    return path


def resolve_path(agent, kind: str, root: str | Path = Path(".")) -> Path:
    """The configured override path when one is set (root-joined, so an absolute
    path stays absolute), raising `FileNotFoundError` naming that exact path when it
    is missing — the package default is never consulted in that case. With no
    configured override, the package default `lime/prompts/<role>/<kind>.md`, raising
    when it is missing too. Same selection `lime check` validates (spec §6)."""
    configured = (agent.prompt_engineering or {}).get(kind)
    if configured:
        path = Path(root) / configured
        if not path.exists():
            raise FileNotFoundError(
                f"{agent.name}: configured {kind} prompt is missing at {path}"
            )
        return path
    default = PACKAGE_DIR / agent.name / f"{kind}.md"
    if not default.exists():
        raise FileNotFoundError(
            f"{agent.name}: no {kind} prompt at {default} and none configured"
        )
    return default
