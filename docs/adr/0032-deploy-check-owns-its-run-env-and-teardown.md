# The merged-tree check owns its Run env and tears itself down

The **Merged-tree gate** (ADR 0029) runs the target project's `commands.setup` and `commands.test` in a throwaway worktree via `quality.run_check`, but every command ran under `operator_env()` alone — no `LIME_RUN_ID`, `LIME_PORT` or `LIME_DB_NAME`. A project whose setup does `createdb "$LIME_DB_NAME"` therefore failed every moved-base deploy with `createdb ""` (shop-api), and the check had no way to give a project the per-Run resources a Run gets. The gate also had no teardown, so anything setup created had no owner to remove it.

1. **The check gets a fresh Run env of its own.** Before running setup, the gate mints a new uuid and installs `{**operator_env(), **_run_env(check_id, check_port, f"lime_{check_id}")}` as the full subprocess environment of the throwaway `_DeployCheckRun`. It is never the deploying Run's id, port or database, so the check can neither collide with a live Run nor leave a Run row behind.

2. **The port belongs to the check alone.** Allocation excludes the deploying Run's recorded port, every currently held Run port (`_held_ports()`) and the whole configured `port_range` pool, so the check cannot steal a slot from a Run launched while it runs. The probe is a bind handoff, not an OS-wide lease; a bounded probe that finds nothing free refuses the deploy before any setup rather than falling back to a parent, fixed or empty port.

3. **Teardown runs once, in the check's own env, pass or fail.** After setup and test, the configured `commands.teardown` runs exactly once through the same frozen environment and cwd, inside a `finally` before the worktree is removed — on green, on setup failure, on test failure and on interrupt. No configured teardown means no subprocess, warning or artifact. A failing teardown only warns, naming the check's database, and leaves the verdict unchanged; a teardown failure never turns a green check red or a red one green.

4. **The Run's own lifecycle is untouched.** `quality._run` reads an explicit `command_env` only when a Run carries one; every ordinary Run — `lime run`, post-merge `lime deploy` and `lime runs rm` — still gets `operator_env()` and its recorded port and database, exactly as before.

We considered,

- **Give the check the deploying Run's own env.** Rejected: it would make the check share the Run's port and database, so a moved-base deploy would either collide with a live Run or mutate Run resources the check does not own.
- **Run the check with no teardown, and clean up some other way.** Rejected: the project is the only thing that knows how its `commands.setup` provisioned resources; an unowned check database outlives the check (the ticket's whole point), and a fixed teardown rule would fight the project's own.
- **A fixed, empty or inherited port fallback when the pool is exhausted.** Rejected: a check sharing a held port can corrupt a live Run; refusing before setup fails loud and cheap.
- **Let a failed teardown fail the deploy.** Rejected: the check's verdict is about the merged tree, not about cleanup, and a teardown flake must not block a merge the tree passed.

Trade-offs accepted: every moved-base deploy now allocates a port and runs the project's teardown, so deploy carries one more command and one more resource dependency — and `commands.teardown` runs in the check even when setup/test never created anything. We accepted that because a check that fails on `createdb ""` blocks every deploy for a class of projects, and a check database with no teardown owner survives its check, while the added teardown is exactly the command the project already wrote to remove what its setup creates. The check still refuses before any merge and leaves the Run's own status and resources untouched.
