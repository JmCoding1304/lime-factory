"""Shared, read-only diff-cap accounting (ticket 132, 193).

One evaluator — `evaluate()` — assembles the effective commit policy, the resolved
base/HEAD identity, per-file stats and exemptions into an `AccountingMeasurement`.
Verify is its only caller: it logs the measured size and never fails a Run for it.

Everything here is read-only: no staging, no index/HEAD/worktree mutation, no file
contents dumped to the trace. Failures are returned as data, never raised — only
binaries and inspection errors fail; size never does. With one deliberate
exception: an invalid pinned Run base (`InvalidRunBase`) is terminal and
propagates, never a fabricated zero measurement.
"""

from __future__ import annotations

from . import config, git_helper
from .data_types import (
    AccountingMeasurement,
    AccountingPolicy,
    DiffCapContributor,
)

# The accounting semantics version. Bump when a change would make an older
# measurement incomparable. It is part of the effective policy: an older
# measurement recorded under different rules is not reused.
ACCOUNTING_VERSION = 3


def effective_policy(run) -> AccountingPolicy:
    """The canonical effective policy: sorted/deduplicated exclude and uncounted,
    effective cap and the accounting-version constant.

    Defaults and explicitly-equivalent defaults compare equal because the pattern
    lists are sorted and deduplicated.
    """
    exclude, uncounted, cap = config.resolve_commit_settings(run.cfg)
    return AccountingPolicy(
        max_added_lines=cap,
        exclude=sorted(set(exclude)),
        uncounted=sorted(set(uncounted)),
        accounting_version=ACCOUNTING_VERSION,
    )


def _binary_failure(binaries: list[str]) -> str:
    bin_lines = "\n".join(f"  {p}" for p in binaries)
    return (
        f"Diff gate: {len(binaries)} binary file(s) detected (non-deleted):"
        f"\n{bin_lines}"
        f"\nAction: report required binaries to the engineer — `commit.uncounted` cannot"
        f" waive the binary gate. Clean up only genuinely unnecessary generated content."
    )


def evaluate(run, base: str = "HEAD") -> AccountingMeasurement:
    """One fresh, read-only measurement of the current tree against `base`.

    A pinned Run (`run.base_sha`) independently honors that pin: the exact pinned
    SHA — validated as a real ancestor of HEAD without an intervening merge — is
    the comparison commit, whatever `base` a phase caller passed. A legacy Run
    keeps the ordinary merge-base behavior.

    Returns an `AccountingMeasurement`. Size is measured and logged, never a
    failure (ticket 193): only binaries and inspection errors populate `failures`.

    Deliberate exception to that inspection-error contract: `InvalidRunBase`
    propagates instead of being folded into a fabricated zero measurement.
    Invalid pinned history is terminal — the caller must stop and report it,
    never retry.
    """
    base_sha = getattr(run, "base_sha", None)
    requested_base = base_sha or base
    policy = effective_policy(run)
    exclude, uncounted, cap = policy.exclude, policy.uncounted, policy.max_added_lines
    try:
        diff_base = git_helper.resolve_diff_base(base, base_sha)
        head_sha = git_helper.rev("HEAD")
        stats = git_helper.diff_stats(diff_base, exclude)
        exempt = git_helper.uncounted_paths(diff_base, uncounted)
    except git_helper.InvalidRunBase:
        raise
    except RuntimeError as e:
        return AccountingMeasurement(
            requested_base=requested_base,
            exclude=exclude,
            uncounted=uncounted,
            max_added_lines=cap,
            inspection_error=str(e),
            failures=[f"Diff gate inspection error: {e}"],
        )

    counted = [s for s in stats if s["path"] not in exempt]
    counted_files = sorted(
        (DiffCapContributor(path=s["path"], added_lines=s["added_lines"]) for s in counted),
        key=lambda c: c.path,
    )
    eligible_files = sorted(
        (DiffCapContributor(path=s["path"], added_lines=s["added_lines"]) for s in stats),
        key=lambda c: c.path,
    )
    uncounted_files = sorted(
        (
            DiffCapContributor(path=s["path"], added_lines=s["added_lines"])
            for s in stats
            if s["path"] in exempt
        ),
        key=lambda c: c.path,
    )
    binaries = [s["path"] for s in stats if s["binary"] and not s["deleted"]]
    counted_total = sum(s["added_lines"] for s in counted)
    uncounted_total = sum(f.added_lines for f in uncounted_files)
    eligible_total = counted_total + uncounted_total

    sorted_counted = sorted(
        [s for s in counted if s["added_lines"] > 0],
        key=lambda x: (-x["added_lines"], x["path"]),
    )
    top10 = sorted_counted[:10]
    omitted = len(sorted_counted) - len(top10)

    failures: list[str] = []

    if binaries:
        failures.append(_binary_failure(binaries))

    return AccountingMeasurement(
        requested_base=requested_base,
        head_sha=head_sha,
        merge_base_sha=diff_base,
        exclude=exclude,
        uncounted=uncounted,
        max_added_lines=cap,
        counted_total=counted_total,
        uncounted_total=uncounted_total,
        eligible_total=eligible_total,
        counted_files=counted_files,
        eligible_files=eligible_files,
        uncounted_files=uncounted_files,
        binaries=binaries,
        top_contributors=[
            DiffCapContributor(path=s["path"], added_lines=s["added_lines"]) for s in top10
        ],
        omitted_contributors=omitted,
        failures=failures,
    )