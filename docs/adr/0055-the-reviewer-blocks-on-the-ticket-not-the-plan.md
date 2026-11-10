# The reviewer blocks on the ticket, not on the plan

Status: proposed

Ticket 161 made `plan.md` the reviewer's spec: `lime/prompts/reviewer/system.md` said "the plan is the refined ask", and every Step's `failure:` and `writers:` line became a requirement whose `unproven` blocked. The planner, forced to name an outcome on error, re-run, stale completion and interrupt for every step, invented behaviour wherever the ticket was silent — and the reviewer then demanded a test for each invented case. Across 202 reviewed Runs (2026-09-20 to 10-14) 146 blocking items accumulated, ~55% of them asking for tests of plan-invented failure cases; shop-web Run `0cd99579` (ticket 51) burned three review rounds (117 min, $8.70) with 11 of its 12 blockers traced to plan text and every `Done means` line met by round 2 (`.lime/audit/2026-10-15-plan-review/`). A plan is a route through the code, not a second spec: when its guesses become requirements, the ask stops being the thing reviewed.

1. **The spec is the ticket.** The reviewer reads the ticket's `Done means` and `Out of scope` as the requirements; `plan.md` only locates code and is never a source of requirements. One `findings` entry is written per `Done means` line.

2. **Blocking is a closed list.** The reviewer blocks only on: an unmet `Done means` line; a change contradicting `rules`, or an ADR whose title names a changed file; an existing writer of state the diff changes that the diff leaves unguarded, named as `file:line` and the missing guard (ticket 196 narrowed this item); a `never` hit; `Out of scope` work; a forbidden or generated file; missing behaviour-bearing Markdown (including `lime/skill/lime/` and `.claude/skills/lime/`). Nothing else is a blocker.

3. **Everything else is a note.** Edge cases, plan sentences the code does not honour, tests the ticket never asked for, and tests the harness cannot run go to `notes_for_next_agent` — never to `blocking` and never as an unmet finding. Notes alone never withhold `approved`.

4. **Why a plan-only `failure:` outcome never blocks.** `revise()` hands the builder exactly `blocking + trust_violations + every unmet finding`; a note is none of them. A `failure:` outcome the plan states is a design choice made where the ticket was silent, so it cannot be an unmet ticket requirement. The planner now writes `failure: pure` unless the ticket or the code makes the outcome matter, and `failure: assumed — <outcome>` when the ticket is silent and two readings would produce different code; `plan_has_failure_lines` accepts any non-empty value, so the gate never enforced more than the line's presence.

5. **Ticket 161 is narrowed, not reversed.** 161's real clobber — an existing writer the diff leaves unguarded — remains a blocker, named as `file:line` and the missing guard. A mismatch between the plan's `writers:` list and the scout's list is judged in the code instead: with no unsafe code it is a note, never a blocker (ticket 196). What is dropped is the plan-invented `failure:` outcome, and the plan's `writers:` list, as requirements.

We considered,

- **Keep the plan as the spec and drop only the `failure:` clause.** Rejected: under that rule `writers:` and `Done means` still live in the plan, so the reviewer keeps ruling on plan text the ticket never asked for.
- **Carry the previous review into a re-review.** Deferred (ticket 186, deleted 2026-10-15): if review rounds still flip-flop after this ships, reopen it.
- **A `## Failure` ticket section.** Rejected: the tracker keeps four sections; the ticket stays the ask, not a failure catalogue.

Trade-offs accepted: a plan sentence the code does not honour is now invisible to approval — it surfaces as a note, not a blocker. The reviewer's closed list trades that signal for a review that ends when the ticket is met.
