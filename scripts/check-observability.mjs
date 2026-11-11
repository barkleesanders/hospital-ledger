// Observability lint rule (native observability, 2026-11-11).
//
// Dylan-style enforcement, Cloudflare-native: this Worker must never ship
// without the platform observability that feeds Workers Logs, Workers
// Traces, and Cloudflare Issues — and the wide-event middleware must stay
// wired into the app entry. Run via `npm run lint:observability`; it is
// part of the `predeploy` script so a regression fails the deploy gate.
//
// Why explicit blocks matter here: wrangler.jsonc previously carried only
// the shorthand `"observability": {"enabled": true}`, and a deploy
// silently reset the live traces.enabled to false. This lint requires
// the explicit logs + traces blocks so that failure cannot recur.
import { readFileSync } from "node:fs";

const failures = [];

const wrangler = readFileSync(
	new URL("../wrangler.jsonc", import.meta.url),
	"utf8",
);

// Isolate the observability object (it precedes the "ai" key in the file).
const obsMatch = /"observability"\s*:\s*\{([\s\S]*?)\n\t\},/.exec(wrangler);
const obs = obsMatch?.[1] ?? "";
if (!obs) {
	failures.push("wrangler.jsonc: top-level observability block not found");
} else {
	for (const section of ["logs", "traces"]) {
		const block =
			new RegExp(`"${section}"\\s*:\\s*\\{([^}]*)\\}`).exec(obs)?.[1] ?? "";
		// The FIRST enabled value in the block governs (a false followed by a
		// stray true is still a disabled section — proven by negative control).
		const enabledValue = /"enabled"\s*:\s*(true|false)/.exec(block)?.[1];
		if (enabledValue !== "true") {
			failures.push(
				`wrangler.jsonc: [observability.${section}] must exist with enabled = true`,
			);
		}
		if (!/"persist"\s*:\s*true/.test(block)) {
			failures.push(
				`wrangler.jsonc: [observability.${section}] must set persist = true`,
			);
		}
	}
	if (!/"invocation_logs"\s*:\s*true/.test(obs)) {
		failures.push(
			"wrangler.jsonc: [observability.logs] must set invocation_logs = true",
		);
	}
}

const entry = readFileSync(new URL("../src/index.tsx", import.meta.url), "utf8");
if (
	!entry.includes('from "./lib/wide-event"') ||
	!entry.includes("wideEvent")
) {
	failures.push("src/index.tsx: wideEvent middleware is not wired into the app");
}

// The edge-fetch Worker (wrangler.fetch.jsonc) carries the same contract:
// explicit logs + traces blocks, and wideEvent wired into its own entry.
// Its observability block is the last key in the file, so it is read to EOF.
const fetchConfig = readFileSync(
	new URL("../wrangler.fetch.jsonc", import.meta.url),
	"utf8",
);
const fetchObs =
	/"observability"\s*:\s*\{([\s\S]*)\}\s*$/.exec(fetchConfig.trim())?.[1] ?? "";
if (!fetchObs) {
	failures.push("wrangler.fetch.jsonc: top-level observability block not found");
} else {
	for (const section of ["logs", "traces"]) {
		const block =
			new RegExp(`"${section}"\\s*:\\s*\\{([^}]*)\\}`).exec(fetchObs)?.[1] ?? "";
		const enabledValue = /"enabled"\s*:\s*(true|false)/.exec(block)?.[1];
		if (enabledValue !== "true") {
			failures.push(
				`wrangler.fetch.jsonc: [observability.${section}] must exist with enabled = true`,
			);
		}
		if (!/"persist"\s*:\s*true/.test(block)) {
			failures.push(
				`wrangler.fetch.jsonc: [observability.${section}] must set persist = true`,
			);
		}
	}
	if (!/"invocation_logs"\s*:\s*true/.test(fetchObs)) {
		failures.push(
			"wrangler.fetch.jsonc: [observability.logs] must set invocation_logs = true",
		);
	}
}

const fetchEntry = readFileSync(
	new URL("../src/edge-fetch-worker.ts", import.meta.url),
	"utf8",
);
if (
	!fetchEntry.includes('from "./lib/wide-event"') ||
	!fetchEntry.includes("wideEvent")
) {
	failures.push(
		"src/edge-fetch-worker.ts: wideEvent middleware is not wired into the app",
	);
}

if (failures.length > 0) {
	console.error(failures.join("\n"));
	process.exit(1);
}
console.log(
	"observability check passed: logs + traces enabled and persisted, wideEvent wired",
);
