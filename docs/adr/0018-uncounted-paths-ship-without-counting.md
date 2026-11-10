# Uncounted paths — line-budget exemptions without hiding work

Ticket 122's diff gate ([ADR 0017](0017-diff-gate-before-agent-review.md)) measures the working-tree diff against base and fails when added lines exceed `commit.max_added_lines` or when a non-deleted binary is present. It shipped with one knob: `commit.exclude`, which both keeps a path out of `git add -A` *and* removes it from the measurement. That conflation is the problem this ADR resolves.

Sometimes a ticket genuinely requires a large file that must also ship — a project-selected lockfile, a generated schema, a vendored fixture. Before this ADR there were only bad answers:

- **Hide it.** Delete the file, or `.gitignore` it and rely on ordinary ignore behavior. The work never ships, or the next command re-creates it and the gate re-fires.
- **Exclude it.** Put the path in `commit.exclude` so it is never staged. The required file is silently dropped from the commit — the diff passes by discarding content the ticket asked for.
- **Raise the global cap.** Lift `max_added_lines` for everyone, which weakens the gate for every file, not just the one required one.
- **`.gitattributes -diff`.** Mark the file as `-diff` so Git treats it as binary and it stops counting — but then it also trips the non-deleted-binary check, so this trades one failure for another.

None of these is a decision the engineer actually wants. The missing primitive is *shipping a file without counting its lines*.

## Two independent lists

`main` now resolves `commit.exclude` and `commit.uncounted` separately:

- **`exclude` is a staging exclusion.** Matched paths are unstaged and kept out of `git add -A`; they are also ineligible for the line budget because they never enter the diff.
- **`uncounted` is an accounting exemption.** Matched paths are still staged and committed normally; only their **added lines** are kept out of the line budget and the top-offender ranking. Everything else about them — ignoring, history, the binary check — is unchanged.

Both are lists of worktree-root-relative Git path patterns, with the same validation and the same directory/glob matching Git already applies. Defaults are `exclude: [context_handoff/]` and `uncounted: []`; there is no built-in lockfile list — a project opts a path in.

## Validation

`commit.uncounted` is validated exactly like `commit.exclude`: a list of non-empty, non-absolute, literal-ish strings (no `..` components, no pathspec magic `:`/`^`/`!`). Missing or null is the default; an explicit `[]` is an explicit no-op.

The two lists must not share an exact string. A pattern that is simultaneously an accounting exemption and a staging exclusion is ambiguous — exclude is meant to keep a path out of the commit and uncounted is meant to ship it — so exact cross-list duplicates are rejected, naming the pattern and both settings. This is a duplicate check, not a disjointness analysis: distinct overlaps are fine. `exclude: [generated/private/]` and `uncounted: [generated/]` remain valid, and on any overlap **exclude takes precedence** — a path with any excluded component cannot be exempted. Duplicates *within* one list are not rejected.

## Accounting vs. eligibility in verify

`verify` collects `diff_stats(merge_commit, exclude_patterns)` once — the eligible stats — and never passes `uncounted` to `diff_stats` or to any staging call. It then resolves the uncounted matches with a single **read-only, batched** Git call:

```
git ls-files -z --cached --others --exclude-standard --with-tree=<base> -- <patterns...>
```

Every pattern is its own argv element after `--`; nothing is shell-expanded and no command runs per pattern or per file. `--with-tree` folds in base paths removed from the index (so base deletions are covered) without writing the real index, so the call is read-only with respect to HEAD, the index, and the worktree. The raw NUL-delimited stdout becomes a set (whitespace preserved, duplicates collapsed); a nonzero exit raises `RuntimeError`, which flows into verify's existing inspection-error failure path — the gate **fails closed** and grants no partial exemption.

The stats list is partitioned by membership in that set. `total_added` and the top-ten ranking use **counted stats only**; the binary check still runs over **all eligible stats**, so an uncounted binary is still a violation. The eligible exemption set is logged per path as `uncounted_files` (a path-sorted list of `{path, added_lines}`, including zero-line, deleted, and binary entries) alongside the existing payload, for both passing and failing gates. The console and structured evidence come from the existing `PhaseHandle.log`.

Because classification is a plain read-only query over the branch/index/worktree, repeat verification is idempotent: no index write, no diff rescan, no re-match of individual files.

## Preserved behavior, engineer-owned decisions

Nothing here changes `commit_all`, the ignore rules, or history. Neither list force-adds an ignored file; neither removes content already committed on the branch; the binary rule is unchanged. `diff_stats` still excludes only `exclude`. An exempt path must still be a real eligible change to be exempted — unchanged tracked matches the matcher happens to return do not count, because the exemption is granted only by intersecting with the collected stats.

The line-budget and binary gate remediation text now points at the engineer instead of telling the builder to delete or hide artefacts. A builder that needs a large or binary file reports it and asks for an **explicit `commit.uncounted` decision**; it never autonomously exempts its own work, and `commit.uncounted` cannot waive the binary gate at all.

There are no CLI, environment, or ticket overrides, and no new phases or trace events.