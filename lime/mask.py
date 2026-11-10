"""Trace masker (ticket 249).

Redacts credential-shaped spans and known secret values from trace text.
`Tracer.event()` runs it over every payload string before anything is written;
mask first, cut second, so a downstream clip never leaves half a secret.
`raw_output.jsonl` is never masked.
"""

from __future__ import annotations

import bisect
import os
import re
from collections.abc import Iterable, Mapping

MARKER = "[masked]"
MIN_ENV_VALUE = 6

CREDENTIAL_ENV_NAMES = frozenset(
    {
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "CONTEXT7_API_KEY",
    }
)
_CREDENTIAL_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")

PATTERNS = (
    ("openrouter", re.compile(r"sk-or-v1-[A-Za-z0-9_-]{20,}")),
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}")),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer", re.compile(r"\b[Bb]earer[ \t]+(?P<v>[A-Za-z0-9._~+/=-]{16,})")),
    (
        "pem",
        re.compile(
            r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----"
            r"(?:.*?-----END[A-Z0-9 ]*PRIVATE KEY-----"
            r"|(?:\r?\n[A-Za-z0-9+/=]+(?=\r?\n|\Z))*)",
            re.S,
        ),
    ),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")),
    (
        "assignment",
        re.compile(
            r"""(?<![A-Za-z0-9_])(?:[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)"""
            r"""|[A-Za-z0-9_]*_(?:key|token|secret|password)|password|secret)"""
            r"""[ \t]*=(?:[ \t]*"(?P<dq>[^"\n]+)"|[ \t]*'(?P<sq>[^'\n]+)'"""
            r"""|["']?(?P<v>[^\s"'`]+))"""
        ),
    ),
    (
        "json_field",
        re.compile(
            r'"(?:access_token|refresh_token|api_key|apiKey|password|secret)"'
            r'\s*:\s*"(?P<v>(?:[^"\\]|\\.)+)"'
        ),
    ),
    ("url_password", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+:(?P<v>[^\s@/]+)@")),
)


def env_secrets(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Credential-shaped environment values (6+ chars), sorted and de-duped."""
    if environ is None:
        environ = os.environ
    return tuple(
        sorted(
            {
                value
                for name, value in environ.items()
                if (
                    name in CREDENTIAL_ENV_NAMES
                    or name.upper().endswith(_CREDENTIAL_SUFFIXES)
                )
                and len(value) >= MIN_ENV_VALUE
            }
        )
    )


def mask(text: str, secrets: Iterable[str] | None = None) -> str:
    """Mask every pattern hit and known secret value in `text`."""
    values = tuple(v for v in (env_secrets() if secrets is None else secrets) if v)
    out = _one_pass(text, values)
    while out != text:
        text = out
        out = _one_pass(text, values)
    return out


def _one_pass(text: str, values: tuple[str, ...]) -> str:
    markers = [m.span() for m in re.finditer(re.escape(MARKER), text)]
    starts = [s for s, _ in markers]

    spans: list[tuple[int, int]] = list(markers)

    for value in values:
        i = text.find(value)
        while i != -1:
            j = i + len(value)
            k = bisect.bisect_left(starts, j) - 1
            if not (k >= 0 and markers[k][1] > i):
                spans.append((i, j))
            i = text.find(value, i + 1)

    for _name, rx in PATTERNS:
        for m in rx.finditer(text):
            got = [m.span(n) for n, v in m.groupdict().items() if v is not None] or [
                m.span()
            ]
            for span in got:
                if span[0] < span[1]:
                    spans.append(span)

    if not spans:
        return text

    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            last_s, last_e = merged[-1]
            merged[-1] = (last_s, max(last_e, e))
        else:
            merged.append((s, e))

    parts: list[str] = []
    pos = 0
    for s, e in merged:
        parts.append(text[pos:s])
        parts.append(MARKER)
        pos = e
    parts.append(text[pos:])
    return "".join(parts)


def mask_tree(value, secrets: Iterable[str] | None = None):
    """Recursively mask every string in a JSON-like tree; never mutates input."""
    values = tuple(v for v in (env_secrets() if secrets is None else secrets) if v)

    def walk(node):
        if isinstance(node, str):
            return mask(node, values)
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [walk(v) for v in node]
        return node

    return walk(value)
