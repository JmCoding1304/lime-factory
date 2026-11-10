# The claim gate accepts a measured deletion against the Run's pinned base

The `diff_matches_claims` claim gate proved a claimed path only by `p.exists()`. A builder that honestly listed a file it had deleted was therefore called a liar: of 18 gate fails, 10 were deletions (7 of 8 since [ADR 0024](0024-diff-measures-from-a-base-sha-pinned-at-launch.md) pinned the launch base). Each false fail reopens a Session ([ADR 0038](0038-pi-session-ends-on-accepted-submit.md)) and, worse, teaches the builder to leave deletions out of `changed_files` — the opposite of the honesty the gate exists to check. The gate still has to catch real path mistakes, so it cannot simply be deleted; it needs a second, measured proof of a change.

1. **A missing claim also passes when it appears in the diff against the pin.** For a claim whose path does not exist on disk, the gate passes it when the path is in `git_helper.diff_files(base_sha)` — the Run's launch pin only, never HEAD, a branch name or a recomputed merge base. A path that neither exists nor appears in that diff still fails, naming the original claim label with the legacy `claimed changed file does not exist` text.

2. **Existence stays the first and cheapest proof.** A claim whose path exists passes on `exists, <size>` with no Git call, and its size is statted only because it exists. The measured diff is a fallback for missing claims, not a replacement.

3. **Measurement is lazy, once-per-invocation, and pin-gated.** The pin is read with `getattr(run, "base_sha", None)`; a falsy pin skips `diff_files` entirely, so a Run with no pin (a **Legacy Run**) behaves exactly as before. The diff is loaded at most once per invocation, only on the first missing claim with a truthy pin; an all-existing or empty claim set never inspects Git. Nothing is cached across invocations — a re-run remeasures from scratch.

4. **A diff-inspection failure propagates.** The gate has no try/except around `diff_files`: a helper failure raises rather than granting an unmeasured pass, so a broken inspection can never be read as a confirmed change. The gate writes no Run or repo state.

We considered,

- **Delete the gate.** Rejected: it still catches real path mistakes — the 4 claims that are neither present nor in the diff must keep failing.
- **Fall back to HEAD, a branch or a merge base when the pin is absent.** Rejected: the pin is the Run's immutable birth identity (ADR 0024); substituting another base would measure a different change than the one claimed.
- **Fail open when `diff_files` errors.** Rejected: an inspection failure is not evidence of a change; propagating it is the honest, fail-closed result.

Trade-offs accepted: the gate's contract is no longer "exists" alone, so a reader who assumes existence is the only proof must read this ADR; the gate now depends on `git_helper.diff_files` and the pin, so an inspection failure is a hard error on a missing claim rather than a silent pass; and the measured diff is remeasured on every invocation, trading a little work for the guarantee that no stale pass survives a re-run.
