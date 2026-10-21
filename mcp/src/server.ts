/**
 * HospitalLedger MCP server (stdio transport).
 *
 * Exposes the site's hospital price-transparency data to MCP clients
 * (Claude Code, Claude Desktop, etc.). All tools read the repo's bundled
 * datasets; get-hospital additionally fetches the public read-only price
 * API for full per-hospital price payloads.
 *
 * Configure in your MCP client:
 *   { "command": "node", "args": ["/path/to/hospital-ledger/mcp/dist/server.js"] }
 * Optional env: HL_DATA_DIR, HL_API_BASE, HL_OFFLINE=1
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { TOOLS, type ArgDef } from "./tools.js";

function zodShape(args: Record<string, ArgDef>): Record<string, z.ZodTypeAny> {
	const shape: Record<string, z.ZodTypeAny> = {};
	for (const [k, d] of Object.entries(args)) {
		let t: z.ZodTypeAny;
		if (d.type === "number") t = z.number();
		else if (d.type === "boolean") t = z.boolean();
		else t = z.string();
		if (!d.required) t = t.optional();
		shape[k] = t.describe(d.description);
	}
	return shape;
}

const server = new McpServer(
	{ name: "hospital-ledger", version: "1.0.0" },
	{ capabilities: { tools: {} } },
);

for (const [name, def] of Object.entries(TOOLS)) {
	server.tool(name, def.description, zodShape(def.args), async (a) => {
		const result = await def.run(a as Record<string, unknown>);
		return {
			content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }],
		};
	});
}

const transport = new StdioServerTransport();
await server.connect(transport);
