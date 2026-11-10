# Resume re-runs only the latest verify

Status: proposed

[ADR 0023](0023-early-diff-checks-and-provenance-gated-resume.md) item 4 let a resume replay a passed verify only when a content fingerprint of the tree, base and quality commands still matched, and [ADR 0053](0053-rereview-replays-earlier-verifies-as-recorded.md) item 2 exempted the verifies before a `--rereview` point from that check. The fingerprint cost about 290 source lines of git plumbing and hashing, and it let a resume skip a passed verify in one Run out of about 300. What it decided was whether the newest verify had to run again. A rule that always re-runs the newest verify answers that question without any content identity.

1. **On resume, the latest verify always re-runs.** The latest verify is the highest-seq verify row of any status: the one the next review or commit relies on. It re-runs against the current tree. A passing re-run keeps every later success replayable. A failing re-run is handled as a failed verify in a live Run: it invalidates every stale dependent from that verify's first row onward, including dependents that an earlier passing resume preserved. It does not stop at the newest replay entry, because a later passing resume would have left those dependents behind it.

2. **Every earlier verify replays as recorded.** A verify that is not the latest replays its recorded result, passed or failed, with or without `--rereview`, and never triggers a rebuild. This supersedes ADR 0023 item 4 and ADR 0053 item 2: no fingerprint and no provenance check gate a replay.

3. **No fingerprint, one kept fact.** `VerificationProvenance`, the content fingerprint and the replay-matching code are deleted. The verify keeps the HEAD sha it measured, on `VerifyOutput.head_sha`, so the reviewer's `## Tests` still prints `verified HEAD <sha>`. Traces written before this change still load: the old provenance key is ignored and old `verify_cache_miss` events are inert.

We considered,

- **Keep the fingerprint (ADR 0023 item 4 as written).** Rejected: about 290 lines to answer a question the latest-verify rule answers by re-running one verify.
- **Re-run every verify on resume.** Rejected: an earlier verify measured against a tree that later builds have moved past would fail on code it never saw and invalidate finished builds, which is the rebuild ADR 0053 refused.
- **Apply a provenance check to earlier verifies too.** Rejected for the same reason ADR 0053 gave: the tree has moved past them, so the check would rebuild finished work.

Trade-offs accepted: every resume pays one full quality pass even when the tree has not changed. An earlier verify's pass is trusted without re-measurement. What a review or a commit relies on is always re-measured on resume, so no reported success rests on a pass against a tree that no longer exists.
