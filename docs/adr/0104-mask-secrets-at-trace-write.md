# Secrets are masked in the Trace when it is written

Status: accepted

A Session inherits the operator's full environment, and its shell has no sandbox, so a tool call can print a provider key, a token or a database password. Until now nothing scrubbed the Trace. `Tracer.event()` wrote each payload to `lime.db` and `events.jsonl` unfiltered, and the 400-character args clip cut without scrubbing. The Trace is the source of truth, and every reader (the dashboard feed, `lime trace`, `lime status`) reads what was written. A secret written once stays in the history: re-masking later cannot reach the rows already on disk.

1. **The masker runs at write, inside `Tracer.event()`.** Every string in the payload, at any depth, is masked before the db row or the `events.jsonl` line is written. The caller's payload is not changed. `_clip_args` masks the whole string before it cuts, so a clip never leaves half a secret.

2. **Detection is a fixed pattern list plus known values.** The known values are credential-named environment values of six or more characters. The patterns are the list in `specs/assets/dashboard-live-runs-tool-results.md` § Secrets. A secret that matches neither is written in the clear, and nothing downstream can find it.

3. **The masker is a backstop, not a sandbox.** It does not stop a Session from reading a credential file or an environment variable. Limiting what a Session can see (an env allowlist or a shell sandbox) is a separate, later decision.

4. **`raw_output.jsonl` is not masked.** The owner kept it as is (Live Runs decision 4). Nothing from it reaches the feed, and it never leaves the machine that ran the Session.

We considered,

- **Mask when the dashboard or a CLI reads.** Rejected: every reader would need its own masker, and the file on disk would keep the secret in clear.
- **Only an env allowlist for Sessions.** Rejected for this slice: it changes what agents can do, which is a larger decision than trace hygiene. It remains open.
- **Key-name masking and encoded copies of env values** (JSON-escaped, URL-quoted, base64). Rejected for this ticket: the owner's call as a follow-up.

Trade-offs accepted: pattern matching is a heuristic. A secret in a shape outside the list, or one an agent splits or encodes, reaches the Trace and cannot be taken out of the history afterwards. A masked value is what gets stored, so the plan-replay check in `lime/workflows/phases.py` (which compares the stored provenance prompt with the raw prompt) can see a changed prompt when the ticket holds a secret, and a plan may be re-made instead of replayed. Every event string now passes a regex and a substring scan.
