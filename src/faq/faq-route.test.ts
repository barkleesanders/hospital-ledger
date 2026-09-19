import { Hono } from "hono";
import { afterEach, describe, expect, it, vi } from "vitest";
import { z } from "zod/mini";
import {
	type FaqAi,
	type FaqCorpusDoc,
	type FaqRateLimiter,
	LOCAL_BUCKET_MAX_KEYS,
	localBucketAllows,
	localBucketSize,
	MAX_BODY_BYTES,
	MAX_CORPUS_CHARS,
	MAX_QUESTION_CHARS,
	mountInfiniteFaq,
	renderCorpus,
	systemPrompt,
} from "./faq-route";

/** The wire the island parses: every frame is one of these, nothing else. */
const StreamEvent = z.union([
	z.object({ type: z.literal("text-delta"), delta: z.string() }),
	z.object({ type: z.literal("finish") }),
	z.object({ type: z.literal("error"), message: z.string() }),
]);

/**
 * POST /api/faq/ask — the wire, the validation, and the grounding contract.
 *
 * The Workers AI binding is replaced by a fake that (a) records the exact
 * request it was given, so the prompt contract can be asserted, and (b)
 * returns a hand-built SSE body in the shape llama-3.3 / gpt-oss-120b were
 * measured to emit (skill references/verification-2026-09-18.md §5),
 * including a `reasoning` frame that must never reach the client.
 */

type TestEnv = { AI: FaqAi; FAQ_RATE_LIMITER?: FaqRateLimiter };

type RunInput = Parameters<FaqAi["run"]>[1];

const SITE_DOC: FaqCorpusDoc = {
	title: "About the numbers",
	url: "https://hospitalledger.com/about-the-numbers",
	text: "Q: Why does the homepage count fewer hospitals?\nA: Only hospitals with n > 0 price rows are counted.",
};

const FALLBACK = "https://hospitalledger.com/about-the-numbers";

const CCN = "010001";

function sse(frames: string[]): ReadableStream<Uint8Array> {
	const enc = new TextEncoder();

	return new ReadableStream({
		start(controller) {
			for (const f of frames) controller.enqueue(enc.encode(`${f}\n\n`));
			controller.close();
		},
	});
}

/** The measured model frames: llama `response`, then an OpenAI-shaped chunk with reasoning. */
const MODEL_FRAMES = [
	'data: {"response":"","choices":[{"delta":{"content":""}}]}',
	'data: {"response":"Only hospitals ","choices":[{"delta":{"content":"Only hospitals "}}]}',
	'data: {"choices":[{"delta":{"reasoning":"SCRATCHPAD-MUST-NOT-LEAK","content":null}}]}',
	// A digit-only token arrives as a bare JSON number (measured 2026-09-18; see ChunkSchema).
	'data: {"response":4,"choices":[{"delta":{"content":"4"}}]}',
	'data: {"response":",625 ","choices":[{"delta":{"content":",625 "}}]}',
	'data: {"choices":[{"delta":{"content":"7 \\n"}}],"response":7}',
	'data: {"response":0}',
	'data: {"response":"with rows.","choices":[{"delta":{"content":"with rows."}}]}',
	'data: {"response":"","usage":{"completion_tokens":7,"total_tokens":300}}',
	"data: [DONE]",
];

/**
 * Each build() gets its own client IP: without a Rate Limiting binding the
 * route falls back to a per-isolate bucket (10/min per IP), and that bucket is
 * module state shared by every test in this file.
 */
let nextIp = 1;

function build(options: {
	frames?: string[];
	limiter?: FaqRateLimiter;
	globalLimiter?: FaqRateLimiter;
	runImpl?: FaqAi["run"];
}) {
	const ip = `203.0.113.${nextIp++}`;
	const calls: RunInput[] = [];
	let corpusCalls = 0;
	const contextCalls: { kind: string; id: string; url: string }[] = [];

	const ai: FaqAi = {
		run:
			options.runImpl ??
			(async (_model, input) => {
				calls.push(input);

				return sse(options.frames ?? MODEL_FRAMES);
			}),
	};

	const app = new Hono<{ Bindings: TestEnv }>();
	mountInfiniteFaq(app, {
		siteName: "Hospital Ledger",
		fallbackUrl: FALLBACK,
		corpus: async () => {
			corpusCalls += 1;

			return [SITE_DOC];
		},
		rateLimiter: (env) => env.FAQ_RATE_LIMITER,
		globalRateLimiter: () => options.globalLimiter,
		// The site re-reads the record by id; only the one CCN "exists".
		contextDoc: async (_env, ctx, request) => {
			contextCalls.push({ kind: ctx.kind, id: ctx.id, url: request.url });

			return ctx.kind === "hospital" && ctx.id === CCN
				? {
						title: "This hospital",
						url: "https://hospitalledger.com/",
						text: 'Hospital: "Test General". Compliance grade: A, score 95 out of 100.',
					}
				: null;
		},
		extraRules: ["Never say whether a hospital is breaking the law."],
	});
	const env: TestEnv = { AI: ai, FAQ_RATE_LIMITER: options.limiter };

	const post = (body: BodyInit | null, headers: Record<string, string>) =>
		app.request(
			"/api/faq/ask",
			{
				method: "POST",
				body,
				headers: { "cf-connecting-ip": ip, ...headers },
			},
			env,
		);

	const ask = (question: string, context?: unknown) =>
		post(JSON.stringify(context ? { question, context } : { question }), {
			"content-type": "application/json",
		});

	return {
		app,
		ask,
		post,
		calls,
		corpusCalls: () => corpusCalls,
		contextCalls,
		ip,
		env,
	};
}

afterEach(() => {
	vi.restoreAllMocks();
});

describe("POST /api/faq/ask — validation", () => {
	it("rejects an empty question with 400 and never calls the model or the corpus", async () => {
		const { ask, calls, corpusCalls } = build({});
		const res = await ask("");

		expect(res.status).toBe(400);
		expect(calls).toHaveLength(0);
		expect(corpusCalls()).toBe(0);
	});

	it("rejects a whitespace-only question with 400", async () => {
		const { ask } = build({});

		expect((await ask("   ")).status).toBe(400);
	});

	it(`rejects ${MAX_QUESTION_CHARS + 1} chars with 400 and accepts exactly ${MAX_QUESTION_CHARS}`, async () => {
		const { ask, calls } = build({});

		expect((await ask("x".repeat(MAX_QUESTION_CHARS + 1))).status).toBe(400);
		expect(calls).toHaveLength(0);
		const ok = await ask("x".repeat(MAX_QUESTION_CHARS));

		expect(ok.status).toBe(200);
		expect(calls).toHaveLength(1);
	});

	it("rejects a non-JSON body with 400", async () => {
		const { post, calls } = build({});
		const res = await post("nope", { "content-type": "application/json" });

		expect(res.status).toBe(400);
		expect(calls).toHaveLength(0);
	});

	it("rejects JSON without a question key with 400", async () => {
		const { post } = build({});

		const res = await post(JSON.stringify({ q: "hi" }), {
			"content-type": "application/json",
		});

		expect(res.status).toBe(400);
	});

	it(`refuses a body over ${MAX_BODY_BYTES} bytes with 413 before parsing it — declared, and chunked with no Content-Length`, async () => {
		const { post, calls, app, env, ip } = build({});
		const big = JSON.stringify({ question: "x".repeat(MAX_BODY_BYTES) });

		const declared = await post(big, {
			"content-type": "application/json",
			"content-length": String(big.length),
		});
		expect(declared.status).toBe(413);

		const chunked = new ReadableStream<Uint8Array>({
			start(controller) {
				const enc = new TextEncoder();
				for (let i = 0; i < big.length; i += 1024)
					controller.enqueue(enc.encode(big.slice(i, i + 1024)));
				controller.close();
			},
		});
		// A streamed body has no Content-Length; node's fetch needs duplex:"half" to send one.
		const streamed = await app.fetch(
			new Request("http://x/api/faq/ask", {
				method: "POST",
				body: chunked,
				headers: { "content-type": "application/json", "cf-connecting-ip": ip },
				duplex: "half",
			} as RequestInit),
			env,
		);
		expect(streamed.status).toBe(413);
		expect(calls).toHaveLength(0);
	});

	it("rejects a malformed context with 400 naming the page reference, and never reads the store", async () => {
		const { ask, calls, contextCalls } = build({});

		for (const bad of [
			{ kind: "hospital", id: "not-a-ccn" },
			{ kind: "report", id: "010001" },
			{ kind: "payer", id: "Aetna Inc" },
			{ kind: "procedure", id: "x".repeat(11) },
			"010001",
		]) {
			const res = await ask("hello", bad);

			expect(res.status).toBe(400);
			expect(await res.json()).toEqual({
				error: "The page reference sent with the question was not readable.",
			});
		}

		expect(calls).toHaveLength(0);
		expect(contextCalls).toHaveLength(0);
	});
});

describe("POST /api/faq/ask — cross-site posts", () => {
	it("refuses a browser's cross-site POST with 403 before the limiter or the model run", async () => {
		const seen: string[] = [];
		const limiter: FaqRateLimiter = {
			limit: async ({ key }) => {
				seen.push(key);

				return { success: true };
			},
		};
		const { post, calls } = build({ limiter });
		const form = new URLSearchParams({ question: "hi" }).toString();

		const byFetchMeta = await post(form, {
			"content-type": "application/x-www-form-urlencoded",
			"sec-fetch-site": "cross-site",
			origin: "https://evil.example",
		});
		expect(byFetchMeta.status).toBe(403);

		const byOriginOnly = await post(form, {
			"content-type": "application/x-www-form-urlencoded",
			origin: "https://evil.example",
		});
		expect(byOriginOnly.status).toBe(403);

		const nullOrigin = await post(form, {
			"content-type": "application/x-www-form-urlencoded",
			origin: "null",
			"sec-fetch-site": "cross-site",
		});
		expect(nullOrigin.status).toBe(403);

		expect(seen).toHaveLength(0);
		expect(calls).toHaveLength(0);
	});

	it("accepts the site's own form (same-origin Origin) and a client that sends no Origin", async () => {
		const { post, ask, calls } = build({});
		const form = new URLSearchParams({ question: "hi" }).toString();

		const own = await post(form, {
			"content-type": "application/x-www-form-urlencoded",
			origin: "http://localhost",
			"sec-fetch-site": "same-origin",
		});
		expect(own.status).toBe(200);
		expect((await ask("hi")).status).toBe(200); // no Origin at all (curl)
		expect(calls).toHaveLength(2);
	});
});

describe("POST /api/faq/ask — wire shape", () => {
	it("streams text-delta frames, then finish, then [DONE], as text/event-stream that is never cacheable", async () => {
		const { ask } = build({});
		const res = await ask("Why does the homepage count fewer hospitals?");

		expect(res.status).toBe(200);
		expect(res.headers.get("content-type")).toContain("text/event-stream");
		expect(res.headers.get("cache-control")).toBe("private, no-store");
		expect(res.headers.get("cdn-cache-control")).toBe("no-store");
		const body = await res.text();

		const events = body
			.split("\n")
			.filter((l) => l.startsWith("data:"))
			.map((l) => l.slice(5).trim());

		const deltas = events
			.filter((e) => e.startsWith("{"))
			.map((e) => StreamEvent.parse(JSON.parse(e)))
			.filter((e) => e.type === "text-delta")
			.map((e) => e.delta);

		expect(deltas).toEqual([
			"Only hospitals ",
			"4",
			",625 ",
			"7 \n", // the content copy keeps the token's whitespace; the numeric copy does not
			"0",
			"with rows.",
		]);
		expect(events.at(-2)).toBe(JSON.stringify({ type: "finish" }));
		expect(events.at(-1)).toBe("[DONE]");
	});

	it("stays private/no-store even when an earlier middleware materialised c.res (the site's header middleware does)", async () => {
		// Hono's `set res()` copies the headers of the PREVIOUS c.res onto the
		// returned Response, and streamSSE() writes `Cache-Control: no-cache`
		// into that stored response — so a header set only on the returned
		// Response is overwritten. Measured live on wrangler dev 2026-09-18.
		const ai: FaqAi = { run: async () => sse(MODEL_FRAMES) };
		const app = new Hono<{ Bindings: TestEnv }>();
		app.use("*", async (c, next) => {
			c.header("x-seen", "1");
			void c.res; // materialise the context response before the handler
			await next();
		});
		mountInfiniteFaq(app, {
			siteName: "Hospital Ledger",
			fallbackUrl: FALLBACK,
			corpus: async () => [SITE_DOC],
		});

		const res = await app.request(
			"/api/faq/ask",
			{
				method: "POST",
				body: JSON.stringify({ question: "hi" }),
				headers: {
					"content-type": "application/json",
					"cf-connecting-ip": "203.0.113.250",
				},
			},
			{ AI: ai },
		);

		expect(res.status).toBe(200);
		expect(res.headers.get("cache-control")).toBe("private, no-store");
		expect(res.headers.get("cdn-cache-control")).toBe("no-store");
	});

	it("keeps a chunk whose text is all digits (Workers AI serialises `response` as a JSON number)", async () => {
		const { ask } = build({
			frames: [
				'data: {"choices":[{"delta":{"content":"December 196"}}],"response":"December 196"}',
				'data: {"choices":[{"delta":{"content":"7 \\n"}}],"response":7}',
				'data: {"response":"- next"}',
				"data: [DONE]",
			],
		});
		const body = await (await ask("when?")).text();
		const deltas = body
			.split("\n")
			.filter((l) => l.startsWith("data: {"))
			.map((l) => StreamEvent.parse(JSON.parse(l.slice(5))))
			.filter((e) => e.type === "text-delta")
			.map((e) => e.delta);

		expect(deltas).toEqual(["December 196", "7 \n", "- next"]);
	});

	it("never forwards the model's reasoning channel", async () => {
		const { ask } = build({});
		const body = await (await ask("anything")).text();

		expect(body).not.toContain("SCRATCHPAD-MUST-NOT-LEAK");
		expect(body).not.toContain("reasoning");
	});

	it("answers a no-JS form POST with text/plain, never cacheable", async () => {
		const { post } = build({});
		const form = new URLSearchParams({
			question: "Why does the homepage count fewer hospitals?",
		});

		const res = await post(form.toString(), {
			"content-type": "application/x-www-form-urlencoded",
		});

		expect(res.status).toBe(200);
		expect(res.headers.get("content-type")).toContain("text/plain");
		expect(res.headers.get("cache-control")).toBe("private, no-store");
		expect(res.headers.get("cdn-cache-control")).toBe("no-store");
		expect(await res.text()).toBe("Only hospitals 4,625 7 \n0with rows.");
	});

	it("reads a form POST's context from the hidden field", async () => {
		const { post, contextCalls, calls } = build({});
		const form = new URLSearchParams({
			question: "What grade is this hospital?",
			context: JSON.stringify({ kind: "hospital", id: CCN }),
		});

		const res = await post(form.toString(), {
			"content-type": "application/x-www-form-urlencoded",
		});

		expect(res.status).toBe(200);
		expect(contextCalls).toEqual([
			{ kind: "hospital", id: CCN, url: "http://localhost/api/faq/ask" },
		]);
		expect(calls[0].messages[0].content).toContain("### This hospital");
	});

	it("returns 503 JSON when the model call throws", async () => {
		const { ask } = build({
			runImpl: async () => {
				throw new Error("upstream down");
			},
		});

		const res = await ask("hello");

		expect(res.status).toBe(503);
		expect(res.headers.get("content-type")).toContain("application/json");
	});
});

describe("POST /api/faq/ask — grounding contract", () => {
	it("puts the corpus, the site name, the site rules and the refusal instruction in the system prompt", async () => {
		const { ask, calls, corpusCalls } = build({});
		await ask("Why does the homepage count fewer hospitals?");

		expect(corpusCalls()).toBe(1);
		expect(calls).toHaveLength(1);
		const [system, user] = calls[0].messages;

		expect(system.role).toBe("system");
		expect(system.content).toContain("ONLY the reference material");
		expect(system.content).toContain("Hospital Ledger");
		expect(system.content).toContain("### About the numbers");
		expect(system.content).toContain(
			"URL: https://hospitalledger.com/about-the-numbers",
		);
		expect(system.content).toContain("Only hospitals with n > 0 price rows");
		expect(system.content).toContain(`point the visitor to ${FALLBACK}`);
		expect(system.content).toContain(
			"Never invent a URL, price, date, phone number or name",
		);
		expect(system.content).toContain(
			"- Never say whether a hospital is breaking the law.",
		);
		expect(system.content).not.toContain("The visitor is looking at");
		expect(user).toEqual({
			role: "user",
			content: "Why does the homepage count fewer hospitals?",
		});
		expect(calls[0].stream).toBe(true);
	});

	it("puts a known record FIRST in the prompt, with the record intro, re-read from the store by id", async () => {
		const { ask, calls, contextCalls } = build({});
		await ask("What grade is this hospital?", { kind: "hospital", id: CCN });

		expect(contextCalls).toEqual([
			{ kind: "hospital", id: CCN, url: "http://localhost/api/faq/ask" },
		]);
		const system = calls[0].messages[0].content;

		expect(system).toContain("The visitor is looking at one hospital's page");
		expect(system).toContain("treat them as data about the record");
		expect(system.indexOf("### This hospital")).toBeGreaterThan(0);
		expect(system.indexOf("### This hospital")).toBeLessThan(
			system.indexOf("### About the numbers"),
		);
		expect(system).toContain("Compliance grade: A, score 95 out of 100.");
	});

	it("ignores an unknown record id: no record document, no record intro, logged with the id length only", async () => {
		const logged: string[] = [];
		vi.spyOn(console, "log").mockImplementation((...args: unknown[]) => {
			logged.push(args.map(String).join(" "));
		});
		const { ask, calls } = build({});
		const res = await ask("hello", { kind: "hospital", id: "999999" });

		expect(res.status).toBe(200);
		const system = calls[0].messages[0].content;

		expect(system).not.toContain("### This hospital");
		expect(system).not.toContain("The visitor is looking at");
		expect(system).not.toContain("999999");
		expect(logged.join("\n")).toContain('"event":"faq.context.ignored"');
		expect(logged.join("\n")).toContain('"idLen":6');
		expect(logged.join("\n")).not.toContain("999999");
	});

	it("never logs the question text", async () => {
		const logged: string[] = [];
		vi.spyOn(console, "log").mockImplementation((...args: unknown[]) => {
			logged.push(args.map(String).join(" "));
		});
		const secret = "my-name-is-Jane-Doe-SSN-123";
		const { ask } = build({});
		await (await ask(secret)).text();

		expect(logged.length).toBeGreaterThan(0);
		expect(logged.join("\n")).not.toContain(secret);
		expect(logged.join("\n")).toContain('"event":"faq.ask.start"');
		expect(logged.join("\n")).toContain(`"qLen":${secret.length}`);
	});
});

describe("POST /api/faq/ask — abort and error wire", () => {
	it("honours a Stop that arrived while the model call was pending: no delta is streamed and abort is logged, not finish", async () => {
		const logged: string[] = [];
		vi.spyOn(console, "log").mockImplementation((...args: unknown[]) => {
			logged.push(args.map(String).join(" "));
		});
		const ac = new AbortController();
		// build()'s app.request() cannot carry a signal; go through app.fetch with a real Request.
		const app = new Hono<{ Bindings: TestEnv }>();
		mountInfiniteFaq(app, {
			siteName: "Hospital Ledger",
			fallbackUrl: FALLBACK,
			corpus: async () => [SITE_DOC],
		});

		const req = new Request("http://x/api/faq/ask", {
			method: "POST",
			body: JSON.stringify({ question: "hello" }),
			headers: {
				"content-type": "application/json",
				"cf-connecting-ip": "203.0.113.251",
			},
			signal: ac.signal,
		});

		const out = await app.fetch(req, {
			AI: {
				run: async () => {
					ac.abort(); // the visitor pressed Stop during ai.run()

					return sse(MODEL_FRAMES);
				},
			},
		});

		const body = await out.text();

		expect(body).not.toContain("text-delta");
		expect(logged.join("\n")).toContain('"event":"faq.ask.abort"');
		expect(logged.join("\n")).not.toContain('"event":"faq.ask.finish"');
	});

	it("puts only the sanitised error frame on the wire when the model stream breaks, never the raw message", async () => {
		vi.spyOn(console, "log").mockImplementation(() => undefined);

		const broken = new ReadableStream<Uint8Array>({
			start(controller) {
				controller.enqueue(
					new TextEncoder().encode('data: {"response":"partial"}\n\n'),
				);
				controller.error(new Error("INTERNAL upstream detail"));
			},
		});

		const { ask } = build({ runImpl: async () => broken });
		const body = await (await ask("hello")).text();

		// (erroring a ReadableStream discards its queued chunk, so no delta is expected)
		expect(body).toContain("Hit a snag. Please try again in a moment.");
		expect(body).not.toContain("INTERNAL upstream detail");
		expect(body).not.toMatch(/^event: error/m);
	});
});

describe("POST /api/faq/ask — rate limiting", () => {
	it("returns 429 with retry-after when the binding refuses", async () => {
		const seen: string[] = [];

		const limiter: FaqRateLimiter = {
			limit: async ({ key }) => {
				seen.push(key);

				return { success: false };
			},
		};

		const { ask, calls, ip } = build({ limiter });
		const res = await ask("hello");

		expect(res.status).toBe(429);
		expect(res.headers.get("retry-after")).toBe("60");
		expect(seen).toEqual([`faq:${ip}`]);
		expect(calls).toHaveLength(0);
	});

	it("without a binding, the per-isolate bucket refuses the 11th question in a minute from one IP", async () => {
		const { ask, calls } = build({});
		const statuses: number[] = [];

		for (let i = 0; i < 11; i++) statuses.push((await ask("hello")).status);
		expect(statuses.slice(0, 10).every((s) => s === 200)).toBe(true);
		expect(statuses[10]).toBe(429);
		expect(calls).toHaveLength(10);
	});

	it("returns 429 when the account-wide limiter refuses, after the per-IP one allowed", async () => {
		const keys: string[] = [];
		const limiter: FaqRateLimiter = {
			limit: async ({ key }) => {
				keys.push(key);

				return { success: true };
			},
		};
		const globalLimiter: FaqRateLimiter = {
			limit: async ({ key }) => {
				keys.push(key);

				return { success: false };
			},
		};
		const { ask, calls, ip } = build({ limiter, globalLimiter });

		expect((await ask("hello")).status).toBe(429);
		expect(keys).toEqual([`faq:${ip}`, "faq:global"]);
		expect(calls).toHaveLength(0);
	});

	it("caps the per-isolate bucket at LOCAL_BUCKET_MAX_KEYS live IPs and sweeps expired ones", () => {
		const t0 = 1_000_000;

		for (let i = 0; i < LOCAL_BUCKET_MAX_KEYS + 5; i++)
			localBucketAllows(`sweep:${i}`, t0);
		// Every entry is live, so the oldest insertions were evicted to make room.
		expect(localBucketSize()).toBe(LOCAL_BUCKET_MAX_KEYS);
		// A minute later every one of those has expired; the next call sweeps them.
		expect(localBucketAllows("sweep:new", t0 + 60_001)).toBe(true);
		expect(localBucketSize()).toBe(1);
	});

	it("does not charge the account-wide budget for a body that never reaches the model", async () => {
		const globalKeys: string[] = [];
		const globalLimiter: FaqRateLimiter = {
			limit: async ({ key }) => {
				globalKeys.push(key);

				return { success: true };
			},
		};
		const { ask, post, calls } = build({ globalLimiter });

		expect((await ask("")).status).toBe(400);
		expect(
			(
				await post("x".repeat(MAX_BODY_BYTES + 1), {
					"content-type": "application/json",
				})
			).status,
		).toBe(413);
		expect(globalKeys).toEqual([]);
		expect((await ask("hello")).status).toBe(200);
		expect(globalKeys).toEqual(["faq:global"]);
		expect(calls).toHaveLength(1);
	});

	it("lets the question through when the binding allows", async () => {
		const limiter: FaqRateLimiter = { limit: async () => ({ success: true }) };
		const { ask, calls } = build({ limiter });

		expect((await ask("hello")).status).toBe(200);
		expect(calls).toHaveLength(1);
	});
});

describe("renderCorpus", () => {
	const doc = (title: string, len: number): FaqCorpusDoc => ({
		title,
		url: `https://hospitalledger.com/${title}`,
		text: "x".repeat(len),
	});

	it("keeps every doc whole when they fit, and reports truncated=false", () => {
		const out = renderCorpus([doc("a", 100), doc("b", 100)], MAX_CORPUS_CHARS);

		expect(out.truncated).toBe(false);
		expect(out.dropped).toEqual([]);
		expect(out.text).toContain("### a\nURL: https://hospitalledger.com/a\n");
		expect(out.text).toContain("### b");
	});

	it("cuts the FIRST doc that does not fit with a marker and drops the rest, naming them", () => {
		const out = renderCorpus(
			[doc("a", 100), doc("b", 5000), doc("c", 10)],
			1_000,
		);

		expect(out.truncated).toBe(true);
		expect(out.dropped).toEqual(["b", "c"]);
		expect(out.text).toContain(
			"[… truncated; the full page is at the URL above]",
		);
		expect(out.text).not.toContain("### c");
		expect(out.text.length).toBeLessThanOrEqual(1_000);
	});
});

describe("systemPrompt", () => {
	it("carries the record intro only when a record kind is given", () => {
		expect(systemPrompt("S", FALLBACK, "corpus")).not.toContain(
			"The visitor is looking at",
		);
		expect(systemPrompt("S", FALLBACK, "corpus", "procedure")).toContain(
			"The visitor is looking at one procedure's page",
		);
		expect(systemPrompt("S", FALLBACK, "corpus", "payer")).toContain(
			"one insurance plan's page",
		);
	});
});
