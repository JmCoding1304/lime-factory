// submit.ts — pi extension (ticket 74). One tool: submit(<the role's envelope>).
// `parameters` is the role's pydantic JSON Schema, read from LIME_SUBMIT_SCHEMA already inlined — no
// `$ref`/`$defs` (ticket 79: OpenRouter's Gemini translation drops unresolved refs). pi-ai validates a
// non-TypeBox schema as plain JSON Schema.
// terminate:true ends the turn on the call; the Spine (lime/agent_pi.py) reads the
// args off tool_execution_start and latches the FIRST non-error submit as the
// envelope, then stops the process at that transition.
// When LIME_TOOL_CHOICE is "required", a before_provider_request hook injects
// tool_choice: "required" into the OpenAI-compatible provider payload.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { readFileSync } from "node:fs";

export default function (pi: ExtensionAPI) {
	const path = process.env.LIME_SUBMIT_SCHEMA;
	if (!path) throw new Error("submit.ts: LIME_SUBMIT_SCHEMA is not set — the Spine always sets it for pi roles");
	const schema = JSON.parse(readFileSync(path, "utf8"));
	pi.registerTool({
		name: "submit",
		label: "Submit Report",
		description:
			`Deliver your final ${schema.title ?? "Report"} envelope. Call this exactly once, as your last action — ` +
			"the turn ends when it is accepted. Never write the report as text; only this call is read.",
		parameters: schema,
		async execute(_toolCallId, params) {
			return { content: [{ type: "text", text: "Report accepted." }], details: params, terminate: true };
		},
	});

	const toolChoice = process.env.LIME_TOOL_CHOICE;
	if (toolChoice === "required") {
		pi.on("before_provider_request", (_event, _ctx) => {
			const payload = _event.payload as Record<string, unknown>;
			if (payload && typeof payload === "object") {
				payload.tool_choice = "required";
				return payload;
			}
			return undefined;
		});
	}
}