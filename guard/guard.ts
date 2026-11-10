// guard.ts — pi extension (ticket 30). Delivers the Guard on pi (spec §8, probe Q4):
// shells out to `lime guard` for the decision (the ~20-line Python original —
// one decision, two delivery adapters) and translates its
// `hookSpecificOutput`-shaped JSON into pi's tool_call return / sendMessage.
//
// deny -> {block: true, reason} (no `terminate` — deny works without ending the
// run, spec §8, so the model can still pivot to writing its handoff note).
// allow+additionalContext -> pi.sendMessage(..., {deliverAs: "steer"}), the pi
// equivalent of Claude Code's inline additionalContext (probe Q3 vs Q4b).
// Loaded per-Role with `-e` (ticket loaded lime/harness_engineering/context7.ts
// the same way); env LIME_BUDGET_FILE/LIME_HANDOFF_DIR/LIME_HANDOFF_PATH come
// from the Spine (lime/agent_pi.py) via the process env, not extension config.
// The builder's per-`bash` deadline cap rides this same hook (ticket 198, env
// LIME_BASH_DEADLINE_S).
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawnSync } from "node:child_process";

interface HookOutput {
	hookSpecificOutput?: {
		permissionDecision?: "allow" | "deny";
		permissionDecisionReason?: string;
		additionalContext?: string;
	};
}

// Ticket 198: the Spine exports LIME_BASH_DEADLINE_S for the builder only. A `bash` call
// with no `timeout`, a non-number, or one above the deadline runs with the deadline; a
// smaller number is kept. pi validated the args before this hook and hands this same
// object to the tool, so the in-place write is what runs; pi then kills the process tree
// and returns "Command timed out after N seconds" as a tool result — the Session goes on.
function capBashTimeout(event: { toolName: string; input: unknown }): void {
	const deadline = Number(process.env.LIME_BASH_DEADLINE_S);
	if (event.toolName !== "bash" || !Number.isInteger(deadline) || deadline <= 0) return;
	const input = event.input as { timeout?: unknown } | null;
	if (!input || typeof input !== "object") return;
	if (typeof input.timeout === "number" && input.timeout <= deadline) return;
	input.timeout = deadline;
}

export default function (pi: ExtensionAPI) {
	pi.on("tool_call", (event) => {
		const payload = JSON.stringify({ tool_name: event.toolName, tool_input: event.input });
		capBashTimeout(event);
		const proc = spawnSync("lime", ["guard"], { input: payload, encoding: "utf8" });
		if (proc.status !== 0 || !proc.stdout) return; // guard unavailable — fail open, never block on our own error

		let parsed: HookOutput;
		try {
			parsed = JSON.parse(proc.stdout);
		} catch {
			return;
		}
		const out = parsed.hookSpecificOutput;
		if (!out) return;

		if (out.permissionDecision === "deny") {
			return { block: true, reason: out.permissionDecisionReason || "budget stop" };
		}
		if (out.additionalContext) {
			pi.sendMessage(
				{ customType: "guard-warn", content: out.additionalContext, display: true },
				{ deliverAs: "steer" },
			);
		}
	});
}
