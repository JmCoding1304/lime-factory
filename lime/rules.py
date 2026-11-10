"""The `rules` prompt variable: the project's AGENTS.md (or CLAUDE.md fallback) plus the ADR index.

`rules_block(root)` is read fresh on every agent call and handed to the templates as
`{{rules}}` — the roles' own instruction to respect the project's rules. The block is
built from filenames only: an ADR body is never opened.
"""

from __future__ import annotations

from pathlib import Path

# AGENTS.md wins when both exist; CLAUDE.md answers otherwise (the cookbook's
# `CLAUDE.md -> AGENTS.md` symlink still resolves through the first entry).
RULES_FILES = ("AGENTS.md", "CLAUDE.md")
ADR_DIR = ("docs", "adr")


def rules_block(root: str | Path) -> str:
    """The rules block for `root`, or `""` when neither AGENTS.md nor CLAUDE.md exists.

    The selected file's text is preserved verbatim. `\\n\\n## ADRs\\n` is always
    appended, then the sorted filename-derived entries (one per direct
    `root/docs/adr/*.md`), one per line. Only the first hyphen of a filename stem
    becomes a space (`0001-some-slug.md` -> `0001 some-slug`); every entry is kept.
    """
    root = Path(root)
    selected = next((root / name for name in RULES_FILES if (root / name).exists()), None)
    if selected is None:
        return ""
    text = selected.read_text(encoding="utf-8")
    adr_dir = root.joinpath(*ADR_DIR)
    names = sorted(p.name for p in adr_dir.glob("*.md")) if adr_dir.is_dir() else []
    entries = "\n".join(_entry(name) for name in names)
    block = text + "\n\n## ADRs\n"
    if entries:
        block += entries + "\n"
    return block


def _entry(filename: str) -> str:
    """`0001-some-slug.md` -> `0001 some-slug`: strip `.md`, first hyphen only."""
    return filename.removesuffix(".md").replace("-", " ", 1)
