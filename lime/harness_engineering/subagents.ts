// subagents.ts — pi extension (ticket 51). Loaded by the subagent vocabulary word on pi.
// Registers four tools: subagent_create, subagent_continue, subagent_list, subagent_remove.
// Each child is a separate `pi -p --mode json` process (own context window).
// Children run with --no-extensions, so they carry no Guard — acceptable for this ticket
// (behaviour beyond loading is out of scope).
// Loaded by absolute path from config.PI_EXTENSIONS because pi resolves a bare -e word
// against cwd (run 3662b24b).

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { randomUUID } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

interface Sub {
	id: string;
	task: string;
	model: string;
	sessionDir: string;
	turns: number;
	last: string;
}

const subs = new Map<string, Sub>();

function finalText(stdout: string): string {
	const parts: string[] = [];
	for (const line of stdout.split("\n")) {
		const trimmed = line.trim();
		if (!trimmed) continue;
		let parsed: any;
		try {
			parsed = JSON.parse(trimmed);
		} catch {
			continue;
		}
		if (parsed.type === "message_end" && parsed.message?.role === "assistant") {
			const content = parsed.message.content;
			if (Array.isArray(content)) {
				for (const c of content) {
					if (c.type === "text") parts.push(c.text);
				}
			} else if (typeof content === "string") {
				parts.push(content);
			}
		}
	}
	return parts.join("\n");
}

async function runChild(pi: ExtensionAPI, sub: Sub, prompt: string, signal?: AbortSignal) {
	const [provider, ...rest] = sub.model.split("/");
	const args = [
		"-p", "--mode", "json",
		"--provider", provider,
		"--model", rest.join("/"),
		"--session-id", sub.id,
		"--session-dir", sub.sessionDir,
		"--no-context-files",
		"--no-skills",
		"--no-prompt-templates",
		"--no-extensions",
		"--no-approve",
		"--", prompt,
	];
	const r = await pi.exec("pi", args, { signal, timeout: 600_000 });
	sub.turns += 1;
	sub.last = finalText(r.stdout) || r.stderr.slice(-800);
	return {
		content: [{ type: "text", text: `[${sub.id}] ${sub.last}` }],
		details: { id: sub.id, model: sub.model, turns: sub.turns, code: r.code },
		isError: r.code !== 0,
	};
}

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "subagent_create",
		label: "Subagent Create",
		description: "Spawn a subagent to complete a task in its own context window. Returns a result with the subagent id.",
		parameters: Type.Object({
			task: Type.String({ description: "The task for the subagent to complete" }),
			model: Type.Optional(
				Type.String({
					description:
						"provider/model-id; defaults to this session's model. Example: openrouter/anthropic/claude-sonnet-5",
				}),
			),
		}),
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const model = params.model ?? (ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "");
			if (!model) {
				return {
					content: [{ type: "text", text: "No model available: pass `model` parameter explicitly" }],
					isError: true,
				};
			}
			const id = randomUUID();
			const sessionDir = mkdtempSync(join(tmpdir(), "lime-subagent-"));
			const sub: Sub = { id, task: params.task, model, sessionDir, turns: 0, last: "" };
			subs.set(id, sub);
			return runChild(pi, sub, params.task, signal);
		},
	});

	pi.registerTool({
		name: "subagent_continue",
		label: "Subagent Continue",
		description: "Send a follow-up prompt to an existing subagent by id.",
		parameters: Type.Object({
			id: Type.String({ description: "Subagent id returned by subagent_create" }),
			prompt: Type.String({ description: "Follow-up prompt for the subagent" }),
		}),
		async execute(_toolCallId, params, signal) {
			const sub = subs.get(params.id);
			if (!sub) {
				return {
					content: [{ type: "text", text: `Unknown subagent id: ${params.id}` }],
					isError: true,
				};
			}
			return runChild(pi, sub, params.prompt, signal);
		},
	});

	pi.registerTool({
		name: "subagent_list",
		label: "Subagent List",
		description: "List all active subagents with their id, model, turn count and task.",
		parameters: Type.Object({}),
		async execute() {
			if (subs.size === 0) {
				return { content: [{ type: "text", text: "No subagents" }] };
			}
			const lines: string[] = [];
			for (const sub of subs.values()) {
				const task = sub.task.length > 80 ? sub.task.slice(0, 80) + "…" : sub.task;
				lines.push(`${sub.id} · ${sub.model} · ${sub.turns} turns · ${task}`);
			}
			return { content: [{ type: "text", text: lines.join("\n") }] };
		},
	});

	pi.registerTool({
		name: "subagent_remove",
		label: "Subagent Remove",
		description: "Remove a subagent from the active list by id.",
		parameters: Type.Object({
			id: Type.String({ description: "Subagent id to remove" }),
		}),
		async execute(_toolCallId, params) {
			if (subs.delete(params.id)) {
				return { content: [{ type: "text", text: `Removed subagent ${params.id}` }] };
			}
			return {
				content: [{ type: "text", text: `Unknown subagent id: ${params.id}` }],
				isError: true,
			};
		},
	});
}