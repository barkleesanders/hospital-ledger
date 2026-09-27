/**
 * hl — thin CLI over the HospitalLedger tool set.
 *
 *   hl <tool> [--arg value ...]
 *
 * Examples:
 *   hl search-hospitals --query Stanford --state CA
 *   hl get-hospital --ccn 050441 --items_limit 5
 *   hl get-procedure --code 99213 --limit 10
 *   hl search-procedures --query mri
 *   hl get-price-index
 *   hl list-states
 *   hl search-payers --query aetna
 *   hl get-payer --slug aetna
 *   hl status
 *
 * Output is JSON on stdout. Errors go to stderr with a non-zero exit.
 * Env: HL_DATA_DIR, HL_API_BASE, HL_OFFLINE=1 (see README.md).
 */

import { TOOLS, TOOL_NAMES, runTool } from "./tools.js";

function printHelp(): void {
	const lines = [
		"hl — HospitalLedger CLI: query hospital price-transparency data",
		"",
		"Usage: hl <tool> [--arg value ...]",
		"",
		"Tools:",
	];
	for (const name of TOOL_NAMES) {
		const def = TOOLS[name];
		const argList = Object.entries(def.args)
			.map(([k, d]) => `${d.required ? `--${k} <${d.type}>` : `[--${k} <${d.type}>]`}`)
			.join(" ");
		lines.push(`  ${name}${argList ? " " + argList : ""}`);
		lines.push(`      ${def.description}`);
	}
	lines.push("", "Output: JSON on stdout. Env: HL_DATA_DIR, HL_API_BASE, HL_OFFLINE=1.");
	console.log(lines.join("\n"));
}

function parseArgs(argv: string[], def: (typeof TOOLS)[string]): Record<string, unknown> {
	const args: Record<string, unknown> = {};
	const declared = def.args;
	let i = 0;
	while (i < argv.length) {
		const tok = argv[i];
		if (!tok.startsWith("--")) {
			throw new Error(`unexpected positional argument "${tok}" (use --key value)`);
		}
		const eq = tok.indexOf("=");
		const key = (eq === -1 ? tok.slice(2) : tok.slice(2, eq)).replace(/-/g, "_");
		const spec = declared[key];
		if (!spec) throw new Error(`unknown argument --${key} for this tool`);
		let raw: string | undefined;
		if (eq !== -1) raw = tok.slice(eq + 1);
		else if (spec.type === "boolean") raw = "true";
		else {
			i++;
			raw = argv[i];
			if (raw === undefined || raw.startsWith("--")) {
				throw new Error(`--${key} expects a value`);
			}
		}
		if (spec.type === "number") {
			const n = Number(raw);
			if (!Number.isFinite(n)) throw new Error(`--${key} expects a number, got "${raw}"`);
			args[key] = n;
		} else if (spec.type === "boolean") {
			args[key] = raw === "true" || raw === "1" || raw === "yes";
		} else {
			args[key] = raw;
		}
		i++;
	}
	return args;
}

async function main(): Promise<void> {
	const [tool, ...rest] = process.argv.slice(2);
	if (!tool || tool === "help" || tool === "--help" || tool === "-h") {
		printHelp();
		return;
	}
	const def = TOOLS[tool];
	if (!def) {
		console.error(`unknown tool "${tool}". Available: ${TOOL_NAMES.join(", ")}`);
		process.exit(2);
	}
	let args: Record<string, unknown>;
	try {
		args = parseArgs(rest, def);
	} catch (e) {
		console.error(`error: ${e instanceof Error ? e.message : String(e)}`);
		process.exit(2);
	}
	const result = await runTool(tool, args);
	console.log(JSON.stringify(result, null, 2));
	if (result !== null && typeof result === "object" && "error" in result) {
		process.exit(1);
	}
}

main().catch((e) => {
	console.error(`error: ${e instanceof Error ? e.message : String(e)}`);
	process.exit(1);
});
