// scout.ts — pi extension (ADR 0014). One tool: scout.
// Invokes `lime scout "<question>"` via the native Python CLI, which routes through
// the roster's scout Role (phases.scout() / agents.py). This is the Orchestrator's
// dedicated bridge for repo questions — never reads roster configuration, selects a
// model, launches pi directly, or duplicates scout prompts/schema handling in
// TypeScript. The Python side handles all that.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "@sinclair/typebox";

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "scout",
		label: "Scout",
		description:
			"Ask a repo question and get back paths and one-line notes. " +
			"Never reads file contents — use this instead of reaching into target source yourself.",
		parameters: Type.Object({
			question: Type.String({ description: "The repo question to answer" }),
			profile: Type.Optional(
				Type.String({ description: "Roster profile override for the scout Role" }),
			),
		}),
		async execute(_toolCallId, params, signal) {
			const args: string[] = ["scout", params.question];
			if (params.profile) {
				args.push("--profile", params.profile);
			}
			const r = await pi.exec("lime", args, { signal });
			return {
				content: [{ type: "text", text: r.stdout }],
				isError: r.code !== 0,
			};
		},
	});
}