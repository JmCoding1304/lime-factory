# Real model calls are deselected by default, and the suite runs in parallel

Status: proposed

Four tests carry `@pytest.mark.real` and drive a live pi or Claude Code call with 120–180 s timeouts, billing a real model and inheriting its flakes. Nothing deselected them, so every `uv run pytest -q` — verify, and every builder full run — ran them: Runs `29d2762d` and `5a36c656` each burned a builder retry on one. The suite grew from 974 tests in 105 s (09-09) to 1,735 tests in 277 s (10-08), and each limefactory Run executes it about three times, so the same billed, flaky tail was paid on every step.

This ADR adds a marker-expression selection to `[tool.pytest.ini_options]` and adopts parallel execution once it was measured green.

1. **Selection is a marker expression, not a skip hook.** `addopts` carries `-m "not real"`; the `real` marker registration stays. The default run skips the billed tests by *deselecting* them, and an explicit `-m real` on the command line overrides `addopts`, so the tests still run on demand (`uv run pytest -m real --collect-only -q` collects all four). A skip hook would have hidden them from collection and made `-m real` unable to bring them back.

2. **The default lives in `addopts`, so every entry point inherits it.** `commands.test` in `.lime/config.yaml` is `uv run pytest -q` and was left alone: `addopts` is the one place that also covers a bare `pytest`, verify and every builder full run, without a second selection that could drift from it.

3. **`pytest-xdist` is a dev dependency; `-n auto --dist loadfile` joins `addopts` only after two consecutive greens.** The full suite passed twice in a row on the final config before the parallel flags were kept; `--dist loadfile` keeps a file's tests on one worker, so module-level state and per-file fixtures stay serial. A test that binds a port or runs Bun can still collide — a collision would have kept `-n auto` out and left the suite serial.

We considered,

- **A `conftest.py` skip hook for `real`.** Rejected: deselection keeps the tests collectable and overridable from the CLI, which is what "they still run on demand with `-m real`" requires.
- **Editing `commands.test` to pass `-m "not real"`.** Rejected: it covers only the configured command, leaving a bare `pytest` and any other caller to run the billed tests; `addopts` is the single selection every entry point reads.
- **Rewriting colliding tests to be parallel-safe.** Out of scope, and never the fix: a collision is meant to keep `-n auto` out rather than drive a rewrite.

Trade-offs accepted: the billed adapter tests no longer run in any default pass, so a regression only they would catch is seen when someone runs `-m real`, not on verify; and `-n auto` plus `--dist loadfile` runs by default, so a future test that shares a port or an external process with another file can flake until the collision is found.