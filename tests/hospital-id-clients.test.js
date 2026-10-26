import {
	mkdirSync,
	mkdtempSync,
	readFileSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runInNewContext } from "node:vm";
import { afterEach, describe, expect, it, vi } from "vitest";

const source = readFileSync(
	new URL("../public/home-client.js", import.meta.url),
	"utf8",
);
const temporary = [];

afterEach(() => {
	vi.unstubAllGlobals();
	vi.unstubAllEnvs();
	vi.resetModules();
	for (const directory of temporary.splice(0))
		rmSync(directory, { recursive: true });
});

describe("homepage direct hospital lookup", () => {
	it.each(["Sutter", "Kaiser", "Baylor"])(
		"keeps hospital name %s on the search path",
		(value) => {
			let submit;
			const location = { href: "", hash: "", pathname: "/" };
			const dispatchEvent = vi.fn();
			const scrollIntoView = vi.fn();
			const query = { value: "", dispatchEvent };
			const elements = {
				"by-hosp-form": {
					addEventListener: (_event, callback) => {
						submit = callback;
					},
				},
				"by-hosp-input": { value },
				q: query,
				search: { scrollIntoView },
			};
			runInNewContext(source, {
				document: {
					getElementById: (id) => elements[id] || null,
					addEventListener: () => {},
				},
				window: {},
				location,
				console,
				setTimeout,
				clearTimeout,
				Event: class {},
				fetch: () => new Promise(() => {}),
			});
			submit({ preventDefault() {} });
			expect(location.href).toBe("");
			expect(query.value).toBe(value);
			expect(dispatchEvent).toHaveBeenCalledOnce();
			expect(scrollIntoView).toHaveBeenCalledOnce();
		},
	);

	it.each(["250128", "25063E", " 25063e "])(
		"navigates ID %s to its canonical page",
		(value) => {
			let submit;
			const location = { href: "", hash: "", pathname: "/" };
			const elements = {
				"by-hosp-form": {
					addEventListener: (_event, callback) => {
						submit = callback;
					},
				},
				"by-hosp-input": { value },
			};
			runInNewContext(source, {
				document: {
					getElementById: (id) => elements[id] || null,
					addEventListener: () => {},
				},
				window: {},
				location,
				console,
				setTimeout,
				clearTimeout,
				// Leave the unrelated async data loader pending; exercise the actual form binding.
				fetch: () => new Promise(() => {}),
			});
			expect(submit).toBeTypeOf("function");
			submit({ preventDefault() {} });
			expect(location.href).toBe(`/hospital/${value.trim().toUpperCase()}`);
		},
	);
});

describe("agent hospital lookup", () => {
	it.each(["250128", "25063e"])(
		"loads ID %s even outside the bundled CMS directory",
		async (ccn) => {
			const directory = mkdtempSync(join(tmpdir(), "hl-id-client-"));
			temporary.push(directory);
			mkdirSync(join(directory, "prices"));
			writeFileSync(join(directory, "hospitals.json"), "[]");
			writeFileSync(join(directory, "prices/index.json"), '{"hospitals":[]}');
			vi.stubEnv("HL_DATA_DIR", directory);
			vi.stubEnv("HL_API_BASE", "https://hospitalledger.test");
			vi.stubEnv("HL_OFFLINE", "0");
			const fetch = vi.fn(
				async () =>
					new Response(
						JSON.stringify({
							hospital_name: "Public price fixture",
							items: [{ code: "99213", type: "CPT" }],
						}),
						{ headers: { "content-type": "application/json" } },
					),
			);
			vi.stubGlobal("fetch", fetch);
			const { runTool } = await import("../mcp/src/tools.ts");
			const result = await runTool("get-hospital", { ccn });
			expect(fetch.mock.calls[0][0]).toBe(
				`https://hospitalledger.test/api/prices/${ccn.toUpperCase()}`,
			);
			expect(result).toMatchObject({
				found: true,
				hospital: {
					ccn: ccn.toUpperCase(),
					name: "Public price fixture",
					directory_source: "price-record-only",
				},
				prices: { source: "live-api", pagination: { total: 1 } },
			});
			expect(result.hospital).not.toHaveProperty("required");
		},
	);
});
