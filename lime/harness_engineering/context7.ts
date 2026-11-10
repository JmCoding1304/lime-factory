// context7.ts — pi extension (ticket 15). One tool: context7_docs(library, query).
// Documented REST surface (https://context7.com/docs/api-guide): v2 libs/search + context.
// Probed 2026-08-31: works unauthenticated (`context7-quota-tier: anonymous`,
// ratelimit-limit 200/day shared across endpoints); CONTEXT7_API_KEY (ctx7sk…) optional
// via `Authorization: Bearer` for a higher tier. Ships at lime/harness_engineering/context7.ts;
// loaded per-Role with `-e` (no MCP — ticket 04).
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "@sinclair/typebox";

const BASE = "https://context7.com/api/v2";

function headers(): Record<string, string> {
	const key = process.env.CONTEXT7_API_KEY;
	return key ? { Authorization: `Bearer ${key}` } : {};
}

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "context7_docs",
		label: "Context7 Docs",
		description:
			"Fetch current, version-accurate documentation snippets for a library from context7.com. " +
			"Give the library name (e.g. 'supabase', 'next.js') and a topic query; returns curated doc/code snippets with source URLs.",
		parameters: Type.Object({
			library: Type.String({ description: "Library name to look up, e.g. 'supabase', 'next.js', 'tailwindcss'" }),
			query: Type.String({ description: "Topic within the library, e.g. 'auth password reset', 'app router middleware'" }),
		}),
		async execute(_toolCallId, params, signal) {
			// 1) resolve library name -> context7 id (top result)
			const su = `${BASE}/libs/search?libraryName=${encodeURIComponent(params.library)}&query=${encodeURIComponent(params.query)}`;
			const sr = await fetch(su, { headers: headers(), signal });
			if (!sr.ok) return { content: [{ type: "text", text: `context7 search failed: HTTP ${sr.status}` }], details: {}, isError: true };
			const sj = (await sr.json()) as { results?: Array<{ id: string; title: string }> };
			const top = sj.results?.[0];
			if (!top) return { content: [{ type: "text", text: `context7: no library found for '${params.library}'` }], details: {}, isError: true };
			// 2) fetch docs for that id, filtered by the query
			const du = `${BASE}/context?libraryId=${encodeURIComponent(top.id)}&query=${encodeURIComponent(params.query)}&type=txt`;
			const dr = await fetch(du, { headers: headers(), signal });
			if (!dr.ok) return { content: [{ type: "text", text: `context7 docs failed for ${top.id}: HTTP ${dr.status}` }], details: {}, isError: true };
			const text = await dr.text();
			return {
				content: [{ type: "text", text: `Library: ${top.title} (${top.id})\n\n${text}` }],
				details: { library: top.id, remaining: dr.headers.get("ratelimit-remaining") },
			};
		},
	});
}
