"""Tests for lime/mask.py — the trace masker (ticket 249)."""

from __future__ import annotations

import random
import string

import pytest

from lime.mask import (
    CREDENTIAL_ENV_NAMES,
    MARKER,
    env_secrets,
    mask,
    mask_tree,
)

OR = "sk-or-v1-" + "0123456789abcdef" * 4
ANT = "sk-ant-api03-" + "A1b2C3d4" * 6
GHP = "ghp_" + "a1B2c3D4e5" * 4
GHO = "gho_" + "Z9y8X7w6V5" * 4
PAT = "github_pat_" + "11ABCDEFG0" * 3 + "_" + "xYz" * 10
AKIA = "AKIA" + "ABCDEFGHIJ234567"
JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
)
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA1234567890abcdef\n"
    "ZZZZ1234567890abcdefgh==\n"
    "-----END RSA PRIVATE KEY-----"
)


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        (f"key {OR} end", "key [masked] end"),
        (f"x={ANT};", "x=[masked];"),
        (f"token {GHP}.", "token [masked]."),
        (f"token {GHO}", "token [masked]"),
        (f"pat {PAT} ok", "pat [masked] ok"),
        (f"aws {AKIA} ok", "aws [masked] ok"),
        ("Authorization: Bearer abcdefghijklmnop1234", "Authorization: Bearer [masked]"),
        (f"before\n{PEM}\nafter", "before\n[masked]\nafter"),
        (
            "-----BEGIN PRIVATE KEY-----\n"
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\nabcd",
            "[masked]",
        ),
        (f"jwt {JWT} ok", "jwt [masked] ok"),
        ("export OPENROUTER_API_KEY=abc123xyz && run", "export OPENROUTER_API_KEY=[masked] && run"),
        ("DB_PASSWORD='hunter2' x", "DB_PASSWORD='[masked]' x"),
        ('API_KEY="abc def" x', 'API_KEY="[masked]" x'),
        ("?access_token=abc&x=1", "?access_token=[masked]"),
        (
            '{"access_token": "tok-123", "refresh_token":"r1", "api_key": "k",'
            ' "apiKey":"K", "password": "p w", "secret": "s"}',
            '{"access_token": "[masked]", "refresh_token":"[masked]", "api_key": "[masked]",'
            ' "apiKey":"[masked]", "password": "[masked]", "secret": "[masked]"}',
        ),
        ("postgres://app:devpass@localhost:5432/db", "postgres://app:[masked]@localhost:5432/db"),
    ],
)
def test_every_secrets_pattern_is_masked(inp, expected):
    assert mask(inp, ()) == expected


@pytest.mark.parametrize(
    "inp",
    [
        "sk-or-v1-abc123",
        "API_KEY=",
        "API_KEY= \nnext",
        'API_KEY=""',
        '"password": ""',
        "Bearer tokens are short",
        "http://localhost:8080/path",
        "postgres://app:@localhost/db",
        "-----BEGIN PUBLIC KEY-----",
        "ghp_short",
        "AKIA123",
    ],
)
def test_near_misses_stay_unchanged(inp):
    assert mask(inp, ()) == inp


@pytest.mark.parametrize(
    "inp",
    [
        "the quick brown fox jumps over the lazy dog",
        "run a4f20cfe phase 04-planner",
        "commit 635f30b9a1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6",
        "/Users/dev/.lime/worktrees/limefactory/a4f20cfe/lime/mask.py",
        "session 3f2b8c1e-9d4a-4e5b-8c7d-1a2b3c4d5e6f",
        "sorted(xs, key=lambda x: x)",
        "monkey business",
    ],
)
def test_unknown_values_stay_unchanged(inp):
    assert mask(inp, ()) == inp


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ("API_KEY=[masked]", "API_KEY=[masked]"),
        ('API_KEY="[masked]"', 'API_KEY="[masked]"'),
        ("Bearer [masked]", "Bearer [masked]"),
        ('"password": "[masked]"', '"password": "[masked]"'),
        ("API_KEY=[masked]tail", "API_KEY=[masked]"),
        ("[masked][masked] x", "[masked] x"),
    ],
)
def test_patterns_match_through_markers(inp, expected):
    assert mask(inp, ()) == expected


def test_env_values_are_masked_and_merge_with_patterns():
    assert mask("echo zq9-unique-value", ["zq9-unique-value"]) == "echo [masked]"
    assert mask(f"x {OR}", [OR[:10]]) == "x [masked]"


def test_env_hit_on_a_marker_is_ignored():
    assert mask("[masked]x", ["]x"]) == "[masked]x"
    assert mask("a[masked]", ["a"]) == "[masked]"
    assert mask('"x"', ['"']) == "[masked]x[masked]"


def test_env_secrets_picks_credential_names_of_six_plus_chars():
    assert env_secrets(
        {
            "X_KEY": "secret1",
            "X_TOKEN": "short",
            "PATH": "/usr/bin:/bin",
            "GH_TOKEN": "gho_abcdef",
            "my_password": "pw123456",
        }
    ) == ("gho_abcdef", "pw123456", "secret1")
    for name in CREDENTIAL_ENV_NAMES:
        assert "value-" + name in env_secrets({name: "value-" + name})


def test_mask_reads_the_environment_by_default(monkeypatch):
    monkeypatch.setenv("LIME_TEST_API_KEY", "zq9-env-only-249")
    assert mask("cat zq9-env-only-249") == "cat [masked]"


def test_mask_tree_masks_strings_at_any_depth():
    src = {"a": [{"b": OR}], "t": (OR, 3), "n": 3, "none": None, OR: "k"}
    assert mask_tree(src, ()) == {
        "a": [{"b": "[masked]"}],
        "t": ["[masked]", 3],
        "n": 3,
        "none": None,
        OR: "k",
    }
    assert src == {"a": [{"b": OR}], "t": (OR, 3), "n": 3, "none": None, OR: "k"}


_NOISE = [
    "a",
    "x",
    "]",
    "[",
    '"',
    "'",
    " ",
    "\n",
    "=",
    ":",
    "@",
    "/",
    "masked",
    "[masked]",
    "[mas",
    "ked]",
    "]x",
    "API_KEY=",
    "Bearer ",
    "sk-or-v1-",
    "ghp_",
    "eyJ",
    ".",
    "-----BEGIN PRIVATE KEY-----",
    "word",
    "a4f20cfe",
]


def _alnum(r, n):
    return "".join(r.choice(string.ascii_letters + string.digits) for _ in range(n))


def _upnum(r, n):
    return "".join(r.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def test_random_text_never_keeps_a_planted_secret():
    for seed in range(300):
        r = random.Random(seed)
        envs = [_alnum(r, r.randint(6, 12)) for _ in range(3)] + ["a", "]x", '"']
        pieces: list[str] = []
        planted: list[str] = []
        for _ in range(r.randint(5, 40)):
            roll = r.random()
            if roll < 0.6:
                pieces.append(r.choice(_NOISE))
            elif roll < 0.8:
                pieces.append(" " + r.choice(envs) + " ")
            else:
                kind = r.randrange(7)
                if kind == 0:
                    s = "sk-or-v1-" + _alnum(r, 64)
                    planted.append(s)
                    pieces.append(" " + s + " ")
                elif kind == 1:
                    s = "sk-ant-api03-" + _alnum(r, 40)
                    planted.append(s)
                    pieces.append(" " + s + " ")
                elif kind == 2:
                    s = "ghp_" + _alnum(r, 36)
                    planted.append(s)
                    pieces.append(" " + s + " ")
                elif kind == 3:
                    s = "AKIA" + _upnum(r, 16)
                    planted.append(s)
                    pieces.append(" " + s + " ")
                elif kind == 4:
                    v = _alnum(r, 20)
                    planted.append(v)
                    pieces.append(" Bearer " + v + " ")
                elif kind == 5:
                    v = _alnum(r, 12)
                    planted.append(v)
                    pieces.append(" DB_PASSWORD=" + v + " ")
                else:
                    v = _alnum(r, 10)
                    planted.append(v)
                    pieces.append(f" postgres://u:{v}@h/db ")

        text = "".join(pieces)
        out = mask(text, envs)

        assert mask(out, envs) == out

        small = out.replace(MARKER, "")
        it = iter(text)
        assert all(c in it for c in small)

        segments = out.split(MARKER)
        for secret in planted + envs:
            assert not any(secret in seg for seg in segments), (seed, secret, out)
