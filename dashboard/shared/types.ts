/**
 * Types shared by the read-only server and the Vue client.
 *
 * Every interface mirrors a table in lime.db one-for-one (see lime/tracer.py —
 * the schema's single source of truth). Nothing here is derived state except
 * `isCashModel` — the single cash rule; phase durations, run progress and lane
 * layout are computed in the UI, never stored.
 *
 * One addition over the tables: `project` on run rows. The server reads every
 * registered project's lime.db, so each row says which repo it came from.
 */

/**
 * The one cash rule (spec `dashboard-workspace.md`): a Session bills real
 * dollars iff its model starts with `openrouter/` — `sessions.model LIKE
 * 'openrouter/%'`, which is ASCII case-insensitive, which is why the rule
 * lowercases. Everything else rides a subscription: `openai-codex/…`, Claude
 * Code aliases and `claude-*` ids, an unknown model, an empty model.
 */
export const CASH_MODEL_PREFIX = "openrouter/";
export function isCashModel(model: string | null | undefined): boolean {
  return !!model && model.toLowerCase().startsWith(CASH_MODEL_PREFIX);
}

/** runs.status — a run is running until it earns success. */
export type RunStatus =
  | "running"
  | "success"
  | "fail"
  | "budget_exhausted"
  | "needs_rescope"
  | "deployed"
  | "discarded";

/** phases.status — queued only for manifest-declared phases not yet entered. */
export type PhaseStatus = "queued" | "running" | "success" | "fail";

/** phases.kind — decides which lane a block renders in. */
export type PhaseKind = "engineer" | "code" | "agent";

/** runs.kind — what booked the row (spec §11). */
export type RunKind = "run" | "research" | "scout" | "prototype";

/** events.type — the types tracer.py emits. */
export type EventType =
  | "phase_start"
  | "phase_end"
  | "agent_start"
  | "agent_end"
  | "tool_call"
  | "handoff"
  | "gate_pass"
  | "gate_fail"
  | "log"
  | "error"
  | "budget_warn"
  | "budget_stop"
  | "budget_kill"
  | "continuation"
  | "turn"
  | "crash_detected"
  | "timeout_nudge"
  | "timeout_kill"
  | "repeat_stop"
  | "refusal"
  | "tool_result";

export interface Run {
  run_id: string;
  /** Which registered project's lime.db this row came from (repo dir name). */
  project: string;
  workflow: string | null;
  request: string | null;
  status: RunStatus | null;
  engineer: string | null;
  started_at: string | null;
  ended_at: string | null;
  total_tokens: number | null;
  total_cost: number | null;
  /** 1 once archived out of the review list. Review state, not run state. */
  archived: number | null;
  ticket: string | null;
  branch: string | null;
  worktree: string | null;
  port: number | null;
  db_name: string | null;
  profile: string | null;
  kind: RunKind | null;
}

/**
 * The Runs-sheet row source. One line per Run draws its status, ticket or
 * request, project, started_at/ended_at and last_event_at; the per-Run cash
 * and subscription flag are still folded from the Run's turns under the one
 * cash rule (`isCashModel`) for the sheet's spend line.
 */
export interface RunSummary {
  run_id: string;
  project: string;
  workflow: string | null;
  request: string | null;
  status: RunStatus | null;
  ticket: string | null;
  started_at: string | null;
  ended_at: string | null;
  /** Every booked dollar including notional, used for the cost tooltip. */
  total_cost: number | null;
  /** budget_warn events recorded for this run. */
  warns: number;
  /** SUM of turn cost over the Run's turns whose session model isCashModel. */
  cash: number;
  /** True when any session row or turn group has a non-cash model. */
  sub: boolean;
  /** max `events.started_at`, null without events. */
  last_event_at: string | null;
}

/**
 * runs.kind narrowed to what the live feed lists — a scout never books a live
 * row, so it is not one of these.
 */
export type LiveKind = "run" | "research" | "prototype" | "deploy";

/** The latest `status='running'` phase of a live Run, from `phases`. */
export interface RunningPhase {
  seq: number | null;
  name: string | null;
  /** The Phase's stage from `phases.stage`; null before ticket 251. */
  stage: string | null;
  status: PhaseStatus | null;
}

/** One live Run of the `running` frame — a Run with a live `run`/`deploy` process row. */
export interface RunningRun {
  /** Registry name (repo dir name), never a path. */
  project: string;
  run_id: string;
  kind: LiveKind;
  workflow: string | null;
  ticket: string | null;
  /**
   * `runs.batch_id`, the batch that launched this Run; null for a hand launch
   * or a pre-254 ledger.
   */
  batch_id: string | null;
  /**
   * The position of its `started` `batch_children` row (matched by ticket
   * stem), read only while its batch, or the retry that adopted it, is live;
   * null otherwise.
   */
  batch_position: number | null;
  /** The live process row's `started_at`. */
  attempt_started_at: string | null;
  /** The live process row's `heartbeat_at`. */
  heartbeat_at: string | null;
  /** The latest `status='running'` phase; null when the Run has none. */
  phase: RunningPhase | null;
  /** The last Session whose role equals the phase's owner; null without a phase. */
  session: { role: string; model: string | null } | null;
  /**
   * Context use: the latest turn's `tokens` (falling back to the Session's
   * `context_tokens`) and the Session's resolved budget. Null without a phase.
   */
  context: { tokens: number | null; warn: number | null; stop: number | null } | null;
  /** `MAX(events.started_at)`, null without events. */
  last_event_at: string | null;
}

/**
 * A `batch_children` row in state `queued` whose batch's `batch` process row
 * reads live; `ticket` is the stem or raw arg; `reason` is `slot` ·
 * `blocked <NN>` · `overlap <holder>` · null.
 */
export interface QueuedChild {
  project: string;
  batch_id: string;
  position: number;
  ticket: string;
  reason: string | null;
}

/**
 * A batch whose `batch` process row reads live, i.e. one notch batch header.
 * `retry_of` is the dead batch this one retries (`processes.retry_of`), null
 * on a first try. `died_at` is the dead batch's last `heartbeat_at`, null on
 * a first try or when it never beat.
 */
export interface LiveBatch {
  project: string;
  batch_id: string;
  retry_of: string | null;
  died_at: string | null;
}

/**
 * The `running` frame. The server always sends both lists; they are optional
 * only so a frame from a pre-262 server reads as none.
 */
export interface LiveRunning {
  runs: RunningRun[];
  queued?: QueuedChild[];
  batches?: LiveBatch[];
}

export interface Phase {
  phase_id: string;
  run_id: string;
  seq: number | null;
  name: string | null;
  kind: PhaseKind | null;
  owner: string | null;
  description: string | null;
  status: PhaseStatus | null;
  attempt: number | null;
  retries: number | null;
  error: string | null;
  started_at: string | null;
  ended_at: string | null;
  /**
   * The Phase's stage (Prepare/Build/Review/Ship/Other) from `phases.stage`.
   * Null on a ledger written before ticket 251.
   */
  stage?: string | null;
}

export interface Event {
  /** SQLite rowid — the polling cursor. Monotonic, insertion-ordered. */
  rowid: number;
  event_id: number;
  run_id: string;
  phase_id: string | null;
  /** Span nesting: an agent phase expands into its tool-call children. */
  parent_id: number | null;
  type: EventType | null;
  name: string | null;
  /** Raw JSON string as written by the tracer; parse at the point of display. */
  payload_json: string | null;
  tokens: number | null;
  started_at: string | null;
  ended_at: string | null;
}

export interface Envelope {
  envelope_id: number;
  run_id: string;
  phase_id: string | null;
  agent: string | null;
  /** Name of the data_types model the response was parsed against. */
  output_type: string | null;
  payload_json: string | null;
  /** SQLite integer boolean. */
  valid: number | null;
  attempt: number | null;
  created_at: string | null;
}

export interface GateResult {
  id: number;
  run_id: string;
  phase_id: string | null;
  attempt: number | null;
  gate: string | null;
  /** SQLite integer boolean. */
  passed: number | null;
  /** JSON array of violation strings; "[]" on a pass. */
  violations_json: string | null;
  /**
   * JSON array of GateCheck — the per-item evidence behind the verdict, so a
   * green gate can say WHAT it verified rather than only that it passed.
   * Null on rows written without checks; fall back to the verdict alone.
   */
  checks_json: string | null;
  created_at: string | null;
}

/** One item a gate inspected — the parsed element of `checks_json`. */
export interface GateCheck {
  item: string;
  ok: boolean;
  note: string;
}

/** sessions — one row per Session (a continuation is its own row). Supplies lane labels (`role · model`). */
export interface Session {
  session_id: string;
  run_id: string;
  phase_id: string;
  role: string;
  /** 0 for a phase's first Session, 1 for its continuation. */
  continuation: number | null;
  runtime: string | null;
  model: string | null;
  /**
   * The role's lane color from config.yaml, e.g. "#a78bfa". Null on roles
   * with no configured color — fall back to the UI's own palette.
   */
  color: string | null;
  /**
   * How full the Session's context window was after its last turn, and the
   * model's ceiling. Null on a Session still running — the lane draws no bar
   * rather than a misleading empty one.
   */
  context_tokens: number | null;
  context_window: number | null;
  /**
   * The resolved budget lime wrote when the Session was created. Null before
   * ticket 251 and on an agent_start fallback lane.
   */
  budget_warn?: number | null;
  budget_stop?: number | null;
  created_at: string | null;
  last_used_at: string | null;
}

// ── payload_json shapes ──────────────────────────────────────────────────────
// events.payload_json is stored as a string. These are the parsed shapes for
// the payloads the UI renders; every field is optional because the tracer
// writes what the runtime adapter reported, which varies by runtime.

/** Parsed `agent_start` payload — the live source of a lane's label and color. */
export interface AgentStartPayload {
  model?: string;
  thinking?: string;
  session_id?: string;
  color?: string;
  runtime?: string;
  purpose?: string;
  /** Tool allowlist; null means all tools. */
  tools?: string[] | null;
  harness_engineering?: string[];
}

/** Dollars per component — mirrors lime.data_types.CostBreakdown. */
export interface CostBreakdown {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  total: number;
}

/**
 * Tokens per component for one Session, summed across every send it made.
 * Mirrors lime.data_types.UsageBreakdown: `input` EXCLUDES cache reads,
 * which bill at their own rate. `reasoning` is the thinking SHARE of
 * `output`, not a fifth component — adding it would double-count.
 */
export interface UsageBreakdown {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  reasoning?: number;
  total: number;
  cost?: CostBreakdown;
}

/** Parsed `agent_end` payload — closes out a Session with its cost and context use. */
export interface AgentEndPayload {
  cost?: number;
  usage?: UsageBreakdown;
  /** Window occupancy after the final turn, and the model's ceiling. */
  context_tokens?: number;
  context_window?: number;
}

/**
 * Parsed `tool_call` payload — one event per real tool call, emitted when the
 * tool returns. `result_snippet` and `duration_ms` are absent when the
 * runtime never reported a result.
 */
export interface ToolCallPayload {
  tool?: string;
  tool_call_id?: string;
  args?: Record<string, unknown>;
  result_snippet?: string;
  ok?: boolean;
  duration_ms?: number;
  agent?: string;
}

// ── API responses ────────────────────────────────────────────────────────────

/** GET /api/runs — every registered project's runs, merged. */
export type RunsResponse = RunSummary[];

/** GET /api/projects — the registry, resolved. */
export interface ProjectInfo {
  /** Repo dir name — the value run rows carry in `project`. */
  name: string;
  /** Absolute path of the registered project. */
  path: string;
  /** False when the project has no lime.db yet (registered, never ran). */
  db_found: boolean;
}

export type ProjectsResponse = ProjectInfo[];

/**
 * GET /api/spend — turn-event costs of the last 7 days summed per provider,
 * merged across projects. Same semantics as `lime cost` (spec §11): provider
 * is the model prefix; a subscription (not cash by `isCashModel`) rides the
 * subscription, so its figure is notional and the row is flagged instead of
 * priced.
 */
export interface ProviderSpend {
  provider: string;
  cost: number;
  subscription: boolean;
}

export type SpendResponse = ProviderSpend[];

/**
 * GET /api/cost?days=N — one row per Session of every run that STARTED in the
 * window and has an outcome (status != running), merged across projects.
 * Two wallets, kept apart: `api_cost` is real API dollars (turn costs on a
 * cash model, 0 otherwise), `notional_cost` is what a subscription Session
 * would have cost at API rates. Never add them.
 */
export interface SessionCost {
  project: string;
  run_id: string;
  run_status: RunStatus | null;
  role: string;
  runtime: string | null;
  model: string;
  /** Turn-event costs summed on a cash model (`isCashModel`); 0 otherwise. */
  api_cost: number;
  /**
   * claude_code only: Claude Code's own total_cost_usd, read from the Session's
   * agent_end (first Session; null when no agent_end was recorded). Turn costs
   * on any other non-cash model; 0 on cash.
   */
  notional_cost: number | null;
  /** Tokens billed over the Session's turns: input + output + cache_read + cache_write. */
  tokens: number;
  /**
   * Wall-clock ms from the Phase's agent_start to its agent_end. null when no
   * agent_end was recorded (killed mid-flight) and on continuation rows — a
   * Phase is timed once, on its first Session, like notional_cost.
   */
  duration_ms: number | null;
}
export type CostResponse = SessionCost[];

/**
 * What actually moved through a run, summed across every Session.
 *
 * Deliberately NOT the billed total: `runs.total_tokens` also counts every
 * cached re-read, which is the same context charged again on each turn.
 */
export interface RunUsage {
  /** Raw prompt tokens read for the first time: new input + cache writes. */
  read: number;
  /** Tokens generated. Each produced exactly once, so this needs no adjusting. */
  written: number;
}

/** GET /api/projects/:project/runs/:run_id */
export interface RunDetail {
  run: Run;
  /** Derived from agent_end payloads, so historical runs have it too. */
  usage: RunUsage;
  /** Ordered by seq. */
  phases: Phase[];
  /**
   * One entry per Session that has run OR is running under this run_id — lane
   * labels come from here. Finished Sessions come from the sessions table;
   * a Session still in flight has no row there yet, so its entry is built
   * from its agent_start event (runtime is null until it finishes).
   */
  sessions: Session[];
}

/** The `run` frame: a Run's full detail plus its envelopes, gate results and liveness. */
export interface FeedRun extends RunDetail {
  envelopes: Envelope[];
  gates: GateResult[];
  live: boolean;
}

/**
 * GET /api/projects/:project/runs/:run_id/events?after=<rowid>&limit=500
 *
 * Poll with `after` = the cursor from the previous response. `cursor` is the
 * highest rowid in this page (or the `after` you sent, when the page is empty),
 * so it can be fed straight back in. `has_more` means the page hit the limit.
 */
export interface EventsPage {
  events: Event[];
  cursor: number;
  has_more: boolean;
}

/**
 * One event of the live feed. Links between events travel as `seq`, never as
 * `event_id`: `parent_seq` is the `seq` of the parent event (an agent phase's
 * tool-call child), so a client can correlate events across runs without
 * knowing the raw ids.
 */
export interface FeedEvent {
  seq: number;
  type: EventType | null;
  name: string | null;
  phase_id: string | null;
  parent_seq: number | null;
  /** The parsed `payload_json`; null when absent or malformed. */
  payload: unknown;
  tokens: number | null;
  started_at: string | null;
}

/**
 * One page of the live feed's events, read by `seq` cursor. `next` is the seq
 * of the last event (or the `after` sent, when the page is empty), so it feeds
 * straight back in; `has_more` means the page filled its limit.
 */
export interface FeedEventsPage {
  events: FeedEvent[];
  next: number;
  has_more: boolean;
}

/** The `events` frame: one Run's new events, at most 1000, from `next` on. */
export interface FeedEvents {
  project: string;
  run_id: string;
  events: FeedEvent[];
  next: number;
}

/**
 * GET /api/projects/:project/runs/:run_id/roles/:role/prompts
 *
 * The exact compiled prompts sent to a role's Session, read from
 * `.lime/runs/<run_id>/<seq>-<role>[.<n>]/prompts/`. These live only as
 * files — the db has no copy. Either field is null when that file isn't on
 * disk, which is the normal state for a role that never ran in this run, so
 * a 200 with two nulls is a valid answer rather than an error.
 */
export interface RolePrompts {
  system: string | null;
  user: string | null;
}

/** Alias matching the naming of the other endpoint payloads. */
export type PromptsResponse = RolePrompts;

/** GET /api/projects/:project/runs/:run_id/envelopes */
export type EnvelopesResponse = Envelope[];

/** GET /api/projects/:project/runs/:run_id/gates */
export type GatesResponse = GateResult[];

/** GET /api/health */
export interface HealthResponse {
  ok: boolean;
  registry: string;
  projects: ProjectInfo[];
  runs: number;
}

export interface ApiError {
  error: string;
}

// ── profiles merge: batch-sync types (ticket 91 / step 2) ───────────────────

/**
 * A single profile-model change for the merge endpoint: `{profile, role, patch}`.
 * Matches the ``changes`` items ``lime profiles merge`` reads from stdin.
 * ``role`` is a role name for agent entries, or ``null`` for a profile's defaults block.
 * ``patch`` requires ``model`` and ``runtime`` and may carry ``thinking``.
 */
export interface ProfileModelPatch {
  model: string;
  runtime: string;
  thinking?: string;
}

export interface ProfileModelChange {
  profile: string;
  role: string | null;
  patch: ProfileModelPatch;
}

/**
 * One target project's result row from a sync call.
 */
export interface ProfileSyncResult {
  project: string;
  ok: boolean;
  wrote: boolean;
  errors: string[];
}

/**
 * PUT /api/projects/:project/profiles/sync body — the source project's name is
 * the route segment; ``projects`` lists every target (including the source) and
 * ``changes`` is the delta list.
 */
export interface ProfileSyncRequest {
  projects: string[];
  changes: ProfileModelChange[];
}

export interface ProfileSyncResponse {
  results: ProfileSyncResult[];
}

/**
 * One raw entry of the `agents:` block in .lime/config.yaml (ticket 87).
 * Python owns the schema — the UI passes values through untouched.
 */
export type AgentEntry = Record<string, unknown>;

/**
 * PUT /api/projects/:project/profiles body — full replacement of exactly the
 * `agents:` and `profiles:` blocks. The server validates by handing it to
 * `lime profiles set`; a rejected payload never touches the file.
 */
export interface ProfilesPayload {
  agents: AgentEntry[];
  profiles: Record<
    string,
    { defaults?: Record<string, unknown>; agents?: AgentEntry[] }
  >;
}

/**
 * GET/PUT /api/projects/:project/profiles
 *
 * The raw blocks, the resolved defaults, and `merged` — the defaults ← agent ←
 * profile roster per profile (`default` included). `profiles_list` names the
 * profiles a picker can offer, `default` first.
 */
export interface ProfilesResponse {
  project: string;
  config_path: string;
  agents: AgentEntry[];
  profiles: ProfilesPayload["profiles"];
  defaults: Record<string, unknown>;
  merged: Record<string, AgentEntry[]>;
  profiles_list: string[];
  /** pi's per-model thinking levels, `provider/id` → levels, from `lime profiles get` (ticket 91).
   *  `{}` when pi has no catalog; a model absent here is unconstrained client-side — `lime check`
   *  stays the last line of defence. Python owns the table; nothing here duplicates it. */
  thinking_support: Record<string, string[]>;
}

/** One row of GET /api/models/catalog. `id` is what a config takes: a pi
 *  `provider/model-id`, or a claude_code alias. Prices in USD per million
 *  tokens (pi's own cost table); null unknown. */
export interface ModelCatalogEntry {
  id: string;
  name: string;
  provider: string;
  prompt_price: number | null;
  completion_price: number | null;
  context_length: number | null;
}

/**
 * GET /api/models/catalog — pi's own catalog (`lime models`, ticket 92): every
 * model pi can run, which is exactly the set `lime check` accepts. Falls back
 * to the ledger (distinct runtime/model pairs this installation has actually
 * run, no prices) when pi has no catalog here.
 */
export interface CatalogResponse {
  source: "pi" | "ledger";
  models: ModelCatalogEntry[];
}

// ── file API (ticket 222) ───────────────────────────────────────────────────

/** Read cap for GET …/files/content (spec, Decided at the cut). */
export const FILE_READ_CAP_BYTES = 2 * 1024 * 1024;

/** A create request's kind — POST …/files. */
export type FileKind = "file" | "dir";

/** One entry of GET …/files?path= — path is project-relative, "/"-joined. */
export interface FileEntry { name: string; path: string; dir: boolean }

/** GET …/files?path= — the directory at `path`, entries dirs-first. */
export interface FileListing { path: string; entries: FileEntry[] }

/** GET …/files/index — every tracked and untracked non-ignored path. */
export interface FileIndex { paths: string[] }

/** GET …/files/content — a file's text, its byte size and mtime. */
export interface FileContent { path: string; content: string; size: number; mtime_ms: number }

/** PUT …/files/content — the file to overwrite and its new text. */
export interface FileSaveRequest { path: string; content: string }

/** PUT …/files/content response — the overwritten file's size and mtime. */
export interface FileSaved { path: string; size: number; mtime_ms: number }

/** POST …/files — the path to create (parents are made) and its kind. */
export interface FileCreateRequest { path: string; kind: FileKind }

/** POST …/files response — the created path and kind. */
export interface FileCreated { path: string; kind: FileKind }

/** POST …/files/rename — the source path and its new path. */
export interface FileRenameRequest { from: string; to: string }

/** POST …/files/rename response — the moved entry's old and new paths. */
export interface FileRenamed { from: string; to: string }

/** POST …/files/trash — the entry to hand to the macOS Trash. */
export interface FileTrashRequest { path: string }

/** POST …/files/trash response — the path that was moved to the Trash. */
export interface FileTrashed { path: string }

/** Why a file route refused — the `reason` on a FileRefusalBody. */
export type FileRefusalReason = "bad_path" | "outside_root" | "git_dir" | "not_found"
  | "not_a_file" | "not_a_dir" | "exists" | "too_large" | "binary";

/** Any refused file route's body — the reason plus a human message. */
export interface FileRefusalBody extends ApiError { reason: FileRefusalReason }

// ── Home: Focus tab (ticket 227) — mirrors lime's JSON; the server only spawns lime ──

/** lime/status.py groups — the report's ranking bands. */
export type ReportGroup = "needs_you" | "failed" | "ready" | "done";

/** lime/status.py done reasons — a deployed Run, a merged PR, a person-ticket closed since the last visit (244). tests/test_status.py mirrors this union against status.py's done items. */
export type DoneReason = "deployed" | "merged" | "closed";

/** lime/status.py ITEM_KEYS — one ranked report item; count rows carry `ticket: null`. */
export interface ReportItem {
  group: ReportGroup;
  reason: string;
  project: string | null;
  ticket: string | null;
  title: string | null;
  run_id: string | null;
  branch: string | null;
  since: string | null;
  text: string | null;
  cause: string | null;
  count: number | null;
  kind: string | null;
  focus: string | null;
  actions: string[];
  pr: unknown;
  note: string | null;
  /** ticket 235: the AI "what to do" line; null on count rows and Done items. */
  summary: string | null;
  summary_state: "ok" | "pending" | "failed" | null;
}

/** lime/status.py `home` block — the dashboard repo's push and visit state. */
export interface HomeBlock {
  ahead: number;
  push_error: string | null;
  last_visit: { previous: string; current: string } | null;
}

/** lime/status.py `status --json` — other keys are ignored. */
export interface StatusReport {
  generated_at: string;
  items: ReportItem[];
  home: HomeBlock;
  notes: string[];
}

/** lime/focus.py `focus list --json` — one goal row. */
export interface FocusGoal {
  id: string;
  title: string;
  due: string | null;
  project: string | null;
  done: boolean;
  group: "today" | "this_week" | "upcoming" | "completed";
  overdue: boolean;
}

/** GET /api/home/focus — both spawns' parsed output; errors kept per spawn, status first. */
export interface HomeFocusResponse {
  report: StatusReport | null;
  goals: FocusGoal[] | null;
  errors: string[];
}

/** POST /api/home/visit — lime dash visit's outcome; home_missing marks the init line. */
export interface HomeVisitResponse {
  ok: boolean;
  home_missing: boolean;
  message: string | null;
}

/** POST /api/home/tick — the owner's tick as lime args: a goal's done flag or a ticket's close. */
export type HomeTickRequest =
  | { kind: "goal"; id: string; done: boolean }
  | { kind: "ticket"; project: string; number: string; done: boolean };

/** POST /api/home/tick — the spawned tick command's outcome; message is its stderr line. */
export interface HomeTickResponse {
  ok: boolean;
  message: string | null;
}
