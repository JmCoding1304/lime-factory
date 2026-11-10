// budget.ts — pi extension. Delivers the Orchestrator's 150k budget nudge on pi
// (ADR 0014): Claude Code gets it from the `lime hook budget` UserPromptSubmit
// hook; pi has no hooks, so the launch line loads this one file with
// `-e "$(lime hook budget --path)"` (`--no-extensions` still honours explicit -e).
//
// One text, two deliveries: the message lives in Python. On agent_end (once per
// user prompt, after the last tool call) this reads the session's context usage
// and shells out to `lime hook budget --tokens N`; whatever it prints is queued
// with deliverAs "nextTurn" — the model sees it with the next user prompt, which
// is exactly when Claude Code's hook speaks. Fails open: no lime, no nudge.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawnSync } from "node:child_process";

export default function (pi: ExtensionAPI) {
	pi.on("agent_end", (_event, ctx) => {
		const tokens = ctx.getContextUsage()?.tokens;
		if (tokens == null) return; // right after compaction, before the next response
		const proc = spawnSync("lime", ["hook", "budget", "--tokens", String(tokens)], { encoding: "utf8" });
		if (proc.status !== 0 || !proc.stdout.trim()) return; // under warn, or lime unavailable
		pi.sendMessage(
			{ customType: "budget-warn", content: proc.stdout.trim(), display: true },
			{ deliverAs: "nextTurn" },
		);
		if (ctx.hasUI) ctx.ui.notify(`lime budget: ~${tokens.toLocaleString()} tokens — handoff time`, "warning");
	});
}
