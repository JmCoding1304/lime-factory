# Review Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

### never

{{never}}

{{rules}}

## Task

Confirm that the work reported in `previous_envelope` is what was asked for.

1. The spec is the ticket in `prompt`: its `Done means` and `Out of scope`. `<context_handoff_dir>/plan.md` only locates code; never read it for requirements.
2. Judge the code from the `## Diff` section of `prompt` — hunks with 30 lines of context each; `## Changed files` names the touched paths; `## Outside Where` lists the changed paths outside the ticket's `Where` (`Where: none parsed` when it names none). Read a changed file only when its hunk context is not enough, and do not run tests; `## Tests` is the verify result — its `Recorded by the Spine` lines (name, command, exit code, duration, verified HEAD) are the command evidence; never block for missing verifier output or ask the builder to attach command results.
3. Write one `findings` entry per `Done means` line, with evidence. Block only on the list in your instructions; everything else goes to `notes_for_next_agent`.
4. Deliver your Report — it is the whole review; the Spine records it for the documenter.

`status` is `success` when the review itself completed — it is not the verdict. The verdict is `approved`, and it is true only when `findings` has no unmet entry, `blocking` is empty, and `trust_violations` is empty.

{{report}}
