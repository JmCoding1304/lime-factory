# A tool result keeps the masked tail and lime's own verdict

Status: accepted

Until now the Trace recorded each tool call as its name and clipped args, never its outcome. The owner's terminal needs to show whether a call worked, how long it took and what it printed. The two Runtimes report a result in different shapes, and only lime's own clock is present on every call in both. Results are a new and permanent part of the Trace, so the choices below are hard to undo once Runs carry them.

1. **One `tool_result` event per call, joined on the call id.** Each Adapter normalizes its harness's result line; the event forwarder pairs it with the call it recorded in the same send and writes it. A result with no recorded call (an orphan, a duplicate, a stale id) is dropped. A result is never a call: it does not feed the repeat stop or the Budget.

2. **The output is masked, then cut to its last 2 KiB.** The whole output is masked first, and only then is its tail kept, so a cut never leaves half a secret. `out_bytes` is the raw size before masking and the cut. The tail never splits a multibyte character.

3. **`duration_ms` is lime's receipt clock in both Runtimes.** It runs from the call's receipt to its result's receipt. The harnesses' own durations are not used: pi reports one on 17% of its tool ends and Claude Code on a small share of its results.

4. **A `submit`'s `ok` is lime's envelope verdict.** pi's acceptance flag only says the schema took the call. A `submit` is `ok` only when its args became the delivered envelope and lime's own `model_validate` accepts them, so a call pi accepted but lime rejects is recorded as failed.

5. **Nothing is backfilled, and a killed call keeps only its `tool_call`.** Runs recorded before this change have no tool results. A call whose Session ended before its result gets no synthesized result.

We considered,

- **Keep the full output.** Rejected: a median Run produces 585 KB of tool output, and the full text would bloat every Trace. The unmasked full copy already lives in `raw_output.jsonl`.
- **A larger tail (8 KiB).** Rejected for now: across 601 measured Runs the tail growth is about 131 MB at 2 KiB against about 284 MB at 8 KiB, for a cut that still hides 11% of calls' output.
- **Harness durations.** Rejected: present on too few calls to be the measure in both Runtimes.
- **pi's own flag for `submit`.** Rejected: it would show a success for an envelope lime refuses.

Trade-offs accepted: the Trace keeps only the end of each result, so a long output's start and middle exist only in `raw_output.jsonl`, which is unmasked and never leaves the machine. The duration is the span lime observed, not the harness's own timer. A `submit` lime rejects shows `ok: false` while pi reports the call accepted; the two disagree on purpose. Runs before this change will never show tool results.
