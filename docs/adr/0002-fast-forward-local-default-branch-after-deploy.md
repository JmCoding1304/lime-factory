# Fast-forward the local default branch after a deploy merge

`gh pr merge` (used by `lime deploy`) merges the PR on the remote and leaves the engineer's local default branch behind — the working tree keeps serving the old code. We considered two responses and chose the middle one:

1. **Leave it** — simplest, but every deploy silently makes the working tree stale; the engineer has to `git pull --ff-only` manually before the dev server picks up the merged code.
2. **Auto fast-forward, warn on failure** — fetch and `git merge --ff-only` (or `git fetch origin <branch>:<branch>` when the default branch isn't checked out). If the local branch has diverged or the tree is dirty, print a clear warning and finish the deploy anyway. This is what we built.
3. **Force update** — reset or stash away the divergence — rejected because deploy must never destroy local state.

The trade-off in (2) is between convenience (the working tree is automatically up to date after every green deploy) and the risk of a surprising failure (if the engineer had local work on the default branch, the ff refusal prints a warning mid-deploy). We accepted that risk because the warning is clear, the deploy still completes, and the local work is untouched. The alternative — leaving the tree stale — was the original bug that ticket 45 fixed.