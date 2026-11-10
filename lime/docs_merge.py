"""Resolve the insert-only hunks a docs file was left with by a `git merge` diff3 conflict.

Pure — no file reads, no git — and called nowhere until ticket 275 wires it into `lime deploy`.
Ours = main (the checked-out side), theirs = the branch being merged.

Decisions (assumed, see the ticket's Repo facts):
1. "Only added lines around the base" = the side contains the base lines as one contiguous block
   (first occurrence); its new lines = prefix + suffix, so an insertion between two base lines counts as changed.
2. Duplicate-Term rule: refuse when any term appears on two or more glossary lines of the whole resolved text,
   including duplicates that survive outside every resolved hunk.
3. Marker lines: `<<<<<<<`/`|||||||`/`>>>>>>>` alone or followed by a space + label, `=======` exactly; a stray,
   out-of-order, or EOF-open marker is malformed -> None.
4. Text with no hunk at all is returned unchanged.
"""

from __future__ import annotations

from collections import Counter

from .config import glossary_entries


def _split(text: str) -> list[str]:
    """Split on `"\\n"` only, keeping each line's `"\\n"`; a final piece without newline is kept as-is."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _marker(line: str) -> str | None:
    """`<<<<<<<`/`|||||||`/`>>>>>>>` (alone or + space label) or `=======` exactly; else None."""
    body = line[:-1] if line.endswith("\n") else line
    if body.endswith("\r"):
        body = body[:-1]
    for marker in ("<<<<<<<", "|||||||", ">>>>>>>"):
        if body == marker or body.startswith(marker + " "):
            return marker
    if body == "=======":
        return "======="
    return None


def _added(side: list[str], base: list[str]) -> list[str] | None:
    """The side's added lines when it contains `base` as one contiguous block (first occurrence); else None."""
    for i in range(len(side) - len(base) + 1):
        if side[i:i + len(base)] == base:
            return side[:i] + side[i + len(base):]
    return None


def _resolve_hunk(ours: list[str], base: list[str], theirs: list[str]) -> list[str] | None:
    """Merge one insert-only hunk, or None when both sides changed/removed a base line."""
    theirs_new = _added(theirs, base)
    if theirs_new is not None:
        return ours + theirs_new
    ours_new = _added(ours, base)
    if ours_new is not None:
        return theirs + ours_new
    return None


def resolve(text: str) -> str | None:
    """Resolve every insert-only diff3 hunk in `text`; None when any hunk or the whole file cannot be resolved."""
    ours: list[str] = []
    base: list[str] = []
    theirs: list[str] = []
    output: list[str] = []
    state = "outside"

    for line in _split(text):
        marker = _marker(line)
        if state == "outside":
            if marker == "<<<<<<<":
                state, ours = "ours", []
            elif marker is not None:
                return None
            else:
                output.append(line)
        elif state == "ours":
            if marker == "|||||||":
                state, base = "base", []
            elif marker is not None:
                return None
            else:
                ours.append(line)
        elif state == "base":
            if marker == "=======":
                state, theirs = "theirs", []
            elif marker is not None:
                return None
            else:
                base.append(line)
        else:  # theirs
            if marker == ">>>>>>>":
                resolved = _resolve_hunk(ours, base, theirs)
                if resolved is None:
                    return None
                output.extend(resolved)
                state = "outside"
            elif marker is not None:
                return None
            else:
                theirs.append(line)

    if state != "outside":
        return None

    result = "".join(output)
    counts = Counter(term for term, _ in glossary_entries(result))
    if any(count > 1 for count in counts.values()):
        return None
    return result
