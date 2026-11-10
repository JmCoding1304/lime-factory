"""Regression coverage for the project's default pytest selection.

``pyproject.toml`` registers a ``real`` marker for API-billed adapter tests and
selects ``not real`` through ``addopts``. These tests pin that contract with an
isolated synthetic module so no adapter, model, or the repository suite itself
is ever collected or executed by the child process.
"""

import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
TIMEOUT_S = 30

REAL_TEST_NAMES = (
    "test_real_alpha",
    "test_real_beta",
    "test_real_gamma",
    "test_real_delta",
)


def _synthetic_module(tmp_path: pathlib.Path) -> pathlib.Path:
    """One ordinary passing test plus four marked tests that raise if run."""
    lines = [
        "import pytest",
        "",
        "",
        "def test_ordinary_passes():",
        "    assert True",
    ]
    for name in REAL_TEST_NAMES:
        lines += [
            "",
            "",
            "@pytest.mark.real",
            f"def {name}():",
            f'    raise AssertionError("{name} body must not run")',
        ]
    module = tmp_path / "test_synthetic_real_markers.py"
    module.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return module


def _run_child(tmp_path: pathlib.Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    module = _synthetic_module(tmp_path)
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        str(PYPROJECT),
        "--rootdir",
        str(tmp_path),
        str(module),
        "-q",
        "-n",
        "0",
        *extra_args,
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        env=env,
    )


def test_default_selection_deselects_real(tmp_path):
    result = _run_child(tmp_path)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "1 passed" in output, output
    assert "4 deselected" in output, output
    for name in REAL_TEST_NAMES:
        assert f"{name} body must not run" not in output, output


def test_explicit_real_selection_overrides_default(tmp_path):
    result = _run_child(tmp_path, "-m", "real", "--collect-only")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output

    selected = [line for line in output.splitlines() if "::" in line]
    assert len(selected) == len(REAL_TEST_NAMES), output
    for name in REAL_TEST_NAMES:
        assert any(line.endswith(f"::{name}") for line in selected), output
    assert "test_ordinary_passes" not in output, output