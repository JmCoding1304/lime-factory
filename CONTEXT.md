# limefactory — Context

Ubiquitous language for limefactory. One line per term, one or two sentences saying what it **is**; how it works lives in code, tests and `docs/adr/`.

## The Factory

- **Factory** — limefactory itself: the reusable system that runs agentic software workflows against a target project.
- **Target project** — a codebase the Factory is installed into and operates on (e.g. acmeweb, inventory-app); never the Factory's own code.
- **Foundation** — phase 1 of the Factory: the smallest version that runs every Workflow end to end.
- **Runtime** — the program that runs an agent loop: **Claude Code CLI** (Claude subscription) or **pi** (OpenRouter and Grok billed per call, `openai-codex` on the ChatGPT plan).
- **Role** — a named agent job with its own prompt, model, tools and write boundary, bound to one Runtime.
- **Foundation roster** — the seven Roles: scout, researcher, planner, builder, reviewer, documenter, designer. Researcher and designer are on demand, the scout is a fixed Phase, and testing is a code Phase, never a Role.
- **Prompt variant** — the same Role given a different task prompt, such as the bug-fix Chain's reproduce-first planner and reviewer prompts. Never a new Role.
- **Profile** — a named patch of Runtime, model and thinking per Role, layered over the roster when a Run starts; `default` is the roster itself. Never changes a Role's tools, write boundary or prompts.
- **Profile sync** — copying profile-model changes from one registered project to every other selected one at once; each target succeeds or fails on its own.
- **Model catalog** — every model pi can run, read from pi's own store: the whole vocabulary of the dashboard's model picker and of `lime check`, across every provider pi is signed in to (ADR 0013).
- **Thinking support** — the thinking levels each model accepts, derived in Python from the Model catalog; the dashboard reads it and never keeps its own copy.
- **Adapter** — the module that runs one Runtime headless for one Session and turns its output into an `AgentResult`; the Spine never sees Runtime flags.
- **Skill** — the Markdown instructions a Runtime loads to run lime (`lime/skill/lime/`, stamped into each target project). Behaviour, not documentation: the builder owns it and the documenter never edits it (ADR 0020).
- **Install** — the attended procedure that puts the Factory into a target project: `lime init` stamps `.lime/` and the Skill, then `/lime install` brings the docs to the **Doc standard**, fills config and Trust file, and opens a PR.
- **Doc standard** — the only docs a target project keeps: AGENTS.md (≤40 lines), CONTEXT.md (this glossary) and `docs/adr/`. Anything else must be readable from code, or it is deleted.
- **Entry cap** — the 350-character ceiling on one CONTEXT.md glossary entry: its `- **Term**` line and the lines under it. Keeps each term to a definition; a term that already exists is changed in place, never duplicated.

## Upstream

- **Upstream** — everything before a Run, in the Orchestrator with the engineer present: mapping, grilling, prototyping, locking a spec. All human decisions live here.
- **Orchestrator** — the one interactive session the engineer talks to. It picks the Workflow and launches it on the Spine; it routes and never decides the phase sequence.
- **Captain** — the Orchestrator driving a whole map to its locked spec: it runs the ticket sessions and is the map's single writer (map file, new tickets, numbering). It never resolves a ticket itself.
- **Four-line prompt** — the shape of every Workflow request: the ask, Where, Done means, Out of scope.
- **Where paths** — the paths a ticket's `## Where` names, one per line as that line's first backticked token; what the **Overlap guard** compares.
- **Spec** — the locked document a map produces and Runs consume; it outlives the map and its tickets.
- **Product skeleton** — the one-screen document, locked once per product before its first slice, naming the destination, the data model and the cross-slice interfaces with their owners. Every slice spec cites it.
- **Vertical slice** — a thin cut of a product that runs end to end and is usable on its own; the unit a map charts. Never a layer ("all backend, then all UI").
- **Map close** — ending a map once its spec is locked: a fresh session cuts the implementation tickets, ending with an **Owner QA ticket**; the engineer ratifies them, and the map and its wayfinder tickets are deleted.
- **Owner QA ticket** — the last ticket of a cut, owned by the engineer and never a Run: try the slice, file each bug as a ticket, accept the slice's ADRs, feed the next map.
- **Prototype** — a throwaway page showing two or three variants of an ask, plus notes on what is fixed, free and open; a front-end Run reads it through its `Prototype:` header. A shared prototype directory goes only when the last ticket naming it closes (ADR 0037).
- **Designer** — the Role that turns a front-end ask into a Prototype, Upstream, with the engineer iterating in the same Session. Never runs inside a Run.

## Runs

- **Workflow** — a named end-to-end procedure for one kind of request: greenfield, feature, bug-fix, front-end. Greenfield and front-end are feature tickets carrying a Spec or a Prototype; only bug-fix has its own Chain.
- **Spine** — the deterministic Python control plane that runs a Workflow: fixed phase order, retries, gates, exit status. "Agent proposes, code disposes."
- **Run** — one execution of one Workflow, named by a run id; the unit a Trace describes.
- **Try** — one attempt at a Run's Phases: the first, then each resume that replays the Phases it reuses. Distinct from a **Session**, which is one Role's context window.
- **Chain** — the ordered Phases of one Workflow, built only from the Phase library. Two exist: feature, and bug-fix, which runs the feature Phases with a **Prompt variant**.
- **Phase** — one step of a Chain. Kinds: *engineer* (human input), *agent* (a Role runs and returns an Envelope), *code* (tests, lint, git).
- **Stage** — the Run-canvas row a Phase is drawn in: Prepare, Build, Review or Ship, declared when the Phase is created. Phase rows written before stages existed carry `Other` when their name is unknown.
- **Phase library** — the fixed set of whole steps a Chain may use (engineer, setup, scout, plan, build, verify, review, revise, commit, changes, document, push). A step never changes meaning by flag; different behaviour is a Prompt variant.
- **Scout findings** — the scout's ≤20-line brief for the next agent: where the ask's files, conventions and tests live, as paths and notes, never file contents. A scout that fails leaves a notice saying why, and planning goes on.
- **Plan** — the planner's output for one Run: repo facts plus ordered **Steps**, each sized to one builder Session. The only thing the builder reads.
- **Step** — one unit of a Plan: one builder Session, then one verify. A red Step fails the Run there.
- **Step contract** — the two lines every Step carries: `failure:` (the outcome on error, re-run or interrupt, or `pure`) and `writers:` (every existing writer of the state it touches, each `migrated` or `guarded by <test>`). A Step missing either fails the plan.
- **Verify** — the code Phase that runs the target project's test, lint, typecheck and build commands, then the **Diff gate**; failures go back to the builder.
- **Command evidence** — what verify records per command (command, exit code, duration); the reviewer reads it as the Spine's own measurement, never the builder's word.
- **Gate** — a check in code the Spine runs on an agent's work: a *claim gate* tests an Envelope's claims against reality, a *diff gate* tests the diff against policy. A failed Gate re-prompts the Session that did the work.
- **Diff gate** — the Gate in verify that fails a diff only for added binary files. It also measures counted added lines against the **Sizing budget**, but size never fails it.
- **Sizing budget** (`commit.max_added_lines`) — the counted added lines a Run's plan is sized to. Measured and logged, never a stop.
- **Run size** — a Run's counted added lines at its last verify, shown against its Sizing budget so the engineer sees whether the ticket was cut too big.
- **Staging exclusion** (`commit.exclude`) — a path pattern that is never committed. Distinct from an **Accounting exemption** (`commit.uncounted`): committed, but not counted against the Sizing budget.
- **Envelope** — the typed JSON report a Role returns from an agent Phase; the Spine parses and checks it.
- **Submit tool** — the `submit` tool a pi Role calls to deliver its Envelope, checked against the Role's schema before it runs; the first accepted call is the Envelope and closes the Session (ADR 0038). A **Submit rejection** (the schema refused the call) is relayed back for a corrected call, never counted as a Guard denial.
- **Report block** — the Envelope contract a Role's prompt carries (fields, choices, one example), generated in code from the output type per Runtime: pi's tells the Role to call `submit`, Claude Code's asks for the JSON as its final message.
- **Trace** — the recorded timeline of a Run: Phases, tool calls, Gate results, tokens and cost per Session. Raw JSONL is the source of truth; SQLite (`lime.db`) is the queryable mirror.
- **Tool result** — the Trace event recording one tool call's outcome: whether it succeeded, how long it took, its output size and the masked last 2 KiB of its output. Written beside its `tool_call`; older Runs have none.
- **Masker** — the pass that replaces credentials and known secret values with `[masked]` in every Trace event before it is written; `raw_output.jsonl` is never masked.
- **Turn** — one model answer inside a Session; the unit at which tokens and cost are recorded, so spend shows while a Phase is still running.
- **Cash** — spend billed as real API dollars: Turns served through OpenRouter. Everything else is subscription, shown notional and flagged `+sub`.
- **Reported cost baseline** (`cost_reported`) — the cost a Runtime already reported for a Session, kept so a running total (Claude Code across `--resume`) books only the increase.
- **Worktree** — a Run's own git checkout on its own branch, outside the target project, where all its edits and tests happen. Removed at deploy or discard; the Trace outlives it.
- **Heartbeat** — the `heartbeat_at` stamp an open process row carries (a Run, research, prototype, deploy or batch process) while its process is alive; it shows the process still exists, not that it is making progress.
- **Liveness** — the one verdict (`live`, `starting` or `dead`) on whether a Run or a batch is still running, read by `lime status` and by the **Live feed**'s `running` list; rows written before heartbeats fall back to the pid. Distinct from the pid check that launch guards keep.
- **Run base pin** (`base_sha`) — the commit a Run was born on, fixed at launch; every diff, measurement and PR stat is taken from it, never from the moving default branch.
- **Outside Where** — the files a Run changed that its ticket's Where paths do not cover; the reviewer sees them as a flag and may block on one no Done means bullet asks for. The Run is never refused for them.
- **Legacy Run** — a Run recorded before the Run base pin existed; it keeps branch-based measurement and is never given a pin later.
- **Invalid pinned base** — a Run base pin that is no longer a straight-line ancestor of HEAD (the branch was rebased or merged past it). Terminal: the Run fails loud with its work kept, and the engineer starts a fresh Run.
- **Resume re-verify** — on `--resume`, the Run's latest verify re-runs against the current tree; earlier verifies replay as recorded.
- **Plan provenance** / `--replan` — what a plan records about its inputs (ticket, planner prompt, resolved limits). A plain `--resume` refuses a plan whose inputs changed; `--replan` re-plans and keeps the work.
- **Rereview decision** (`--rereview`) — the engineer's recorded decision to reopen a Run whose review kept blocking: verify re-measures and a fresh review runs, while scout, plan and builds replay.

## Sessions and limits

- **Session** — one Role's context window, from spawn to exit. One agent Phase is one Session; Gate corrections reuse it.
- **Closed Session** — a Session the Spine marked so a resume starts fresh instead of reattaching: its submit was accepted, or it hit its stop and then failed or was interrupted. A rejected submit or an ordinary failure stays resumable.
- **Handoff** — the note and files a Session leaves in `context_handoff/` for the next one; with the Envelope, the only thing that crosses a Session boundary. The **Handoff note** has a fixed shape (Done · Left · Decisions · Don't redo · Open) and carries intent only; state comes from git.
- **Continuation** — a fresh Session that resumes the same Phase from a Handoff note after the previous one stopped at a ceiling or blocked. At most one per Phase.
- **Blocked partial** — a partial Envelope from a Session that hit no ceiling. A second one fails the Run as `blocked`, not `budget_exhausted`.
- **Stream retry** — re-sending the prompt in the same Session after a transient stream error (ADR 0008); when retries run out, a **Fresh-session retry** follows.
- **Fresh-session retry** — one brand-new Session for the same Phase after stream retries run out, seeded from the interrupted one so corrupted provider state is dropped; a second outage fails the Phase (ADR 0009).
- **Crash** — a Phase killed by SIGTERM or SIGINT, recorded as a `crash_detected` event.
- **Smart-zone** — the context an agent session should stay inside: ~150k tokens, 250k hard ceiling.
- **Budget** — the Spine's per-Session token ceiling (the Smart-zone's warn and stop). A backstop, not the plan: every warn means the plan step was too big.
- **Time ceiling** (`timeout_s`) — the per-Role wall-clock bound on one Session; retries never extend it. At the ceiling the Session gets one nudge to hand off and submit, then stops.
- **Bash deadline** — the per-call time limit on a builder's shell call under pi; a call past it returns a timeout and the Session goes on. Distinct from the Time ceiling, which bounds the whole Session.
- **Repeat stop** — the third identical tool call in a row in one Session; it stops the Session the same way the Time ceiling does (ticket 173).
- **Builder limits** — the builder's Session ceilings (time, token warn and stop) as one line, resolved from profile and config. The planner's copy adds the Sizing budget; the builder never sees a line count.
- **Floor** — the context a Session carries before its first turn (system prompt, tool schemas, skills, memory), measured rather than estimated. Distinct from growth.

## Safety

- **Trust file** — the target project's list of what no Run may touch: protected files, secrets, branches, forbidden commands. Code fences the files; the rest reaches Roles as the **Never list**.
- **Never list** — the Trust file's secrets, branches and forbidden commands, handed to every Role in its prompt; the reviewer checks the diff against it. Distinct from **Rules**.
- **Rules** — the target project's `AGENTS.md` (or `CLAUDE.md`) verbatim plus an index of ADR filenames, handed to every Role. A change that breaks a rule is blocking, never bypassed.
- **Fence** — a limit in code on what a Session may change: its write boundary and the Trust file's protected files. Fences never ask; they refuse at call time (**Write fence**) and roll back after the fact.
- **Write fence** — the call-time half of the Fence: the Guard refuses a `write`/`edit` outside the handoff dir and the Role's `writes`. When in doubt it lets the call through, and the phase-end Fence rolls back a breach.
- **Guard** — the in-Runtime hook the Spine uses to speak to a running Session (pi extension, Claude Code `PreToolUse`); it carries the Budget's warn and stop and the Write fence.
- **Ledger** — the paths a Session's own tool calls named, so a Run sharing the engineer's tree can tell its edits from the engineer's (ticket 54). _Avoid_ for the numbering refs (say **Reservation**) or `lime.db` (say **Trace**).

## Launch and deploy

- **Batch** — one `lime run` given several ticket arguments: it runs each as a child Run and owns a `batch` process row in lime.db while it is alive. Distinct from a **Run**, one execution of one Workflow.
- **Batch header** — the title over one live batch's tickets in the **Notch**; a **Retry batch**'s header also gives the time its first try died. Distinct from the **Batch** itself, which has no header once it is dead.
- **Watchdog** — the process a batch starts beside itself, in its own session, to notice that the batch died with its row still open and retry it once. Distinct from a **Heartbeat**, which only shows a process is alive.
- **Retry batch** — the one batch a **Watchdog** launches for a dead **Batch**, over that batch's `queued` and `started` children; a retry is never itself retried. Distinct from a **Stream retry** and a **Fresh-session retry**, which re-send a Session's prompt.
- **Take-over** — a **Retry batch**'s hold on a `started` child its dead **Batch** left behind: the child's existing Run is adopted, deployed, resumed or left as it is, and never given a second Run.
- **Picked up** — a dead **Batch**'s queued child that a Run has since started or whose ticket is closed, so `lime status` no longer counts it as not started. Distinct from **Held**, a live batch's ticket that an overlap keeps from being picked.
- **Where lint** — the read-only check that a ticket's `## Where` names at least one path and hides none, and that its `Prototype:` exists. `lime run` refuses a faulty ticket before anything else; `lime check` lists them.
- **Live overlap** — an unmerged Run whose Where paths intersect a candidate's: a pushed `origin/lime/*` branch, or this project's own unmerged, unremoved Run (a **Local Run holder**, holding from its first minute). A candidate's `Blocked by:` tickets are exempt.
- **Overlap guard** — the launch rule that refuses a ticket with a live overlap unless `--allow-overlap` is given; a batch never picks one. An observation, not a lock. Distinct from **Where lint**, which checks that paths are well formed.
- **Held** — a batch ticket not picked because of a live overlap, cleared when that branch merges. Distinct from a **Stale ticket** and a dependency-blocked ticket.
- **Refusal** — a launch, batch pick or deploy lime declined, with its reason (overlap, CI, merge conflict, a hand-merged PR), recorded in the Trace; one raised before any Run exists is only a SQLite row. Distinct from a **Gate** failure, which hands work back to a Session.
- **Reservation** — a git ref on origin that claims a ticket number (`refs/lime/tickets/<NN>`) or ADR number (`refs/lime/adr/<NNNN>`) before any worktree exists; a racing second claim is rejected. Never deleted.
- **ADR number** — the number the factory reserves for a Run's ADR and hands to the documenter; gaps are expected. A new ADR merges as `Status: proposed`; only the owner accepts it, in the Owner QA ticket.
- **Downstream** — everything after a Run opens its PR: QA and deploy. A Run has no human in it; deploy is attended unless a `--deploy` batch merges each green child through the same checks.
- **Deploy session** — the interactive Claude Code session that carries a merged Run to its platforms. Not a Role: Roles are headless; the engineer is present here. Runs only when the project's `deploy:` list is non-empty.
- **Merged-tree gate** — the test `lime deploy` runs on the real default-plus-PR tree before merging a Run whose base has moved; red refuses the deploy. Distinct from **Verify**, which tests the Run branch against its own base.
- **Base movement** — the read-only note `lime deploy --check` gives of how many merges the default branch gained since a Run's pin. Advisory only.
- **Stale ticket** — a ticket file still on the default branch after its PR merged. `lime deploy` warns about it; the engineer removes it by hand.
- **Finishing deploy** — a re-run of `lime deploy` that completes a deploy lime itself began and merged, so it merges nothing again. A MERGED PR with no earlier deploy row for its Run is a hand merge, refused.
- **Insert-only hunk** — a docs merge conflict where each side only added lines beside the shared base text, so every line of both sides can be kept. Distinct from a hunk where a side rewrote a base line, which is never guessed at.
- **Docs merge** — the one merge commit `lime deploy` may push onto a Run's PR branch to settle an **Insert-only hunk** before merging at that exact head (ADR 0127). Never made on the local Run branch.

## Dashboard

- **Dashboard** — the web view of every Run's Trace (`lime dash`); it reads Traces and never writes to a Run. Its workspace page is **Home**.
- **Home** — the Dashboard's workspace page (`#/`), and `~/.lime/dashboard/`, the owner's private repo of cross-project state; every write commits and pushes.
- **Last visit** — Home's `previous` and `current` open stamps; an open after a 30-minute gap moves `previous` forward. "Done" means done since `previous`.
- **File API** — the Dashboard's access to one registered project's files: list, read, save, create, rename, delete (to the macOS Trash). Never a Run's Trace or `.git/`.
- **File tree** — Home's left pane: one project's folder over the File API, with a file-name search. Names only, never contents.
- **Editor** — Home's middle pane: per project, a strip of file tabs and **Run tabs**, each keeping its own edit state.
- **Run tab** — an Editor tab showing one Run's **Run canvas** over its **Terminal**, read-only; it has no path and nothing to save.
- **Run line** — the one quiet line at the start of the terminal's top bar showing one Run's status, ticket, elapsed time, cash and warning count; a click opens the `[Run]` tab in its **Dock**. Distinct from the **Notch**, which lists every running Run.
- **Run canvas** — a pan/zoom plane showing one Run's planned Phases as cards, one row per Stage (Prepare, Build, Review, Ship, then Other), each carrying its Phase's live state; a Phase run again in a resumed **Try** shows as a smaller branch card, never a second spine card.
- **Terminal** — the read-only, dark live tail of one Run's tool calls, phase events and gate results, one line per event; it takes no typed input.
- **Card detail** — the read-only **Dock** tab a Run canvas card click opens, showing one Phase's status, facts, outcome and files. Tool calls are never shown in it; they stay in the terminal.
- **Dock** — the pane beside a Run's terminal holding its open card detail tabs and its `[Run]` tab; not the Editor's **Run tab**.
- **Find bar** — ⌘F find within the Editor's open file. Distinct from the File tree's file-name search.
- **Runs sheet** — the slide-over list of Runs by day, opened from the top bar; a row opens its Run tab. Not a page.
- **Notch** — the dark pill on the top bar listing the Runs running across every registered project, fed by the Live feed; a row opens its Run tab. Not a Runs sheet: it shows what is running now, plus a live batch's waiting tickets, dim under its **Batch header** and opening nothing.
- **Run name** — the label a Run carries in the Runs sheet: its ticket's number and short name, or, for a Run with no ticket, its request's first sentence.
- **Settings modal** — the Dashboard's settings dialog (Cost, Profiles, Appearance). Not a page.
- **Focus list** — the owner's 5–10 goals in `~/.lime/dashboard/focus.md`; rows, not tickets. Only the owner writes it, through `lime focus`.
- **Focus tab** — Home's right pane: the open Focus-list goals and open Report items, the open project's first.
- **Project filter** — the Focus tab's searchable `Project:` dropdown, which narrows the Focus list and its done footer to one project. Remembered in the browser only, never in Home.
- **Live Focus** — the Focus tab updating itself, with no reload, when a watched file changes: a project's tickets, ADRs or Run db, or the Focus list. Driven by file events, never by a timer.
- **Live feed** — the Dashboard's one server-sent-events stream per browser tab, `GET /api/live`, carrying the Focus list, the Runs now running with each live batch's waiting tickets, and an open Run's header and events.
- **Ticket close** — the owner, the session that resolved a ticket, or the agent guiding the owner (once they confirm it done) ticks it off with `lime ticket close` (`reopen` undoes it): its file stays on disk with `Status: closed`, moving from Needs you to Done since last visit. Distinct from **Map close**, which deletes files.
- **Tripwire** — the refusal that stops an owner-only write (`lime focus add`, `done`, `edit`) or a `lime ticket close`/`reopen` inside a Run Role Session only; interactive sessions pass. A catch for mistakes, not a security boundary.
- **Report** — what `lime status` computes at each call, never stored except each item's **Summary**: **Needs you** (only a person can move it), **Failed** (next step is a resume), **Ready to deploy** (PR open, CI green or absent, merges cleanly) and Done since last visit. Only a ticket's **newest Run** speaks for it.
- **Summary** — the short plain "what to do" shown on one Needs-you, Failed or Ready to deploy item of the Report, at most two sentences. It is kept per item until that item's source changes, and never written into a ticket; goals, per-project count rows and dead-batch lines never get one.
- **Summarizer** — the Role that writes a Summary: no tools and no write paths. Outside the Foundation roster, and its Sessions belong to no Run.
