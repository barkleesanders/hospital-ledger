import { afterEach, describe, expect, it, vi } from "vitest";
import { agentProfileBackend, FRONT_DOOR_URL } from "./faq-ai";
import type { FaqAi } from "./faq-route";

/**
 * The ask box's model backend (src/faq/faq-ai.ts): gpt-6.2-astra through the
 * agentprofile front door when a token is configured, Workers AI otherwise —
 * and a front-door failure falls back per call instead of failing the ask.
 */

const INPUT = {
	messages: [
		{ role: "system", content: "sys" },
		{ role: "user", content: "q" },
	] as const,
	stream: true as const,
	max_tokens: 1200,
	temperature: 0.2,
};

function fallbackAi(seen: string[]): FaqAi {
	return {
		run: async (model) => {
			seen.push(model);

			return { response: "fallback answer" };
		},
	};
}

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe("agentProfileBackend", () => {
	it("is undefined without a token (Workers AI answers, as before)", () => {
		expect(agentProfileBackend({ AI: fallbackAi([]) })).toBeUndefined();
	});

	it("posts the question to the front door as gpt-6.2-astra and returns its stream", async () => {
		const body = new ReadableStream<Uint8Array>();
		const fetchMock = vi.fn(async () => ({ ok: true, status: 200, body }));

		vi.stubGlobal("fetch", fetchMock);
		const backend = agentProfileBackend({
			AGENTPROFILE_TOKEN: "ap_test",
			AI: fallbackAi([]),
		});

		expect(backend?.model).toBe("gpt-6.2-astra");
		const out = await backend?.ai.run("gpt-6.2-astra", INPUT as never);

		expect(out).toBe(body);
		const [url, init] = fetchMock.mock.calls[0] as unknown as [
			string,
			RequestInit,
		];

		expect(url).toBe(FRONT_DOOR_URL);
		expect((init.headers as Record<string, string>).authorization).toBe(
			"Bearer ap_test",
		);
		const sent = JSON.parse(init.body as string) as {
			model: string;
			stream: boolean;
			max_completion_tokens?: number;
			max_tokens?: number;
			temperature?: number;
		};

		expect(sent.model).toBe("gpt-6.2-astra");
		expect(sent.stream).toBe(true);
		// The Astra lane rejects max_tokens and any non-default temperature.
		expect(sent.max_completion_tokens).toBe(1200);
		expect(sent.max_tokens).toBeUndefined();
		expect(sent.temperature).toBeUndefined();
	});

	it("falls back to Workers AI llama when the front door refuses", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () => ({ ok: false, status: 503, body: null })),
		);
		vi.spyOn(console, "error").mockImplementation(() => undefined);
		const seen: string[] = [];
		const backend = agentProfileBackend({
			AGENTPROFILE_TOKEN: "ap_test",
			AI: fallbackAi(seen),
		});
		const out = await backend?.ai.run("gpt-6.2-astra", INPUT as never);

		expect(seen).toEqual(["@cf/meta/llama-3.3-70b-instruct-fp8-fast"]);
		expect(out).toEqual({ response: "fallback answer" });
	});

	it("falls back when the fetch itself throws", async () => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () => {
				throw new Error("network down");
			}),
		);
		vi.spyOn(console, "error").mockImplementation(() => undefined);
		const seen: string[] = [];
		const backend = agentProfileBackend({
			AGENTPROFILE_TOKEN: "ap_test",
			AI: fallbackAi(seen),
		});

		await backend?.ai.run("gpt-6.2-astra", INPUT as never);
		expect(seen).toEqual(["@cf/meta/llama-3.3-70b-instruct-fp8-fast"]);
	});
});
