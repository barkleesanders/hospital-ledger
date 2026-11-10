import { afterEach, describe, expect, it, vi } from "vitest";
import app from "../../index";
import { PART_SIZE_BYTES } from "../../lib/submissions";

/**
 * In-memory R2 stand-in with real multipart semantics: parts are stored
 * per (key, uploadId), completion concatenates them in part order, and
 * head/get see only completed or directly-put objects.
 */
function fakeBucket() {
	const objects = new Map<string, { body: Uint8Array; contentType?: string }>();
	const uploads = new Map<
		string,
		{ key: string; parts: Map<number, { etag: string; bytes: Uint8Array }> }
	>();
	let seq = 0;
	const bucket = {
		async get(key: string) {
			const obj = objects.get(key);
			if (!obj) return null;
			return {
				text: async () => new TextDecoder().decode(obj.body),
				body: new Response(obj.body).body,
				httpEtag: '"etag"',
				httpMetadata: { contentType: obj.contentType ?? "application/json" },
			};
		},
		async head(key: string) {
			const obj = objects.get(key);
			return obj ? { size: obj.body.byteLength } : null;
		},
		async put(
			key: string,
			value: string | ArrayBuffer | Uint8Array,
			opts?: { httpMetadata?: { contentType?: string } },
		) {
			const bytes =
				typeof value === "string"
					? new TextEncoder().encode(value)
					: value instanceof Uint8Array
						? value
						: new Uint8Array(value);
			objects.set(key, {
				body: bytes,
				contentType: opts?.httpMetadata?.contentType,
			});
			return {};
		},
		async createMultipartUpload(key: string) {
			const uploadId = `up-${++seq}`;
			uploads.set(uploadId, { key, parts: new Map() });
			return {
				key,
				uploadId,
				async uploadPart(n: number, value: ArrayBuffer) {
					const etag = `etag-${n}`;
					uploads
						.get(uploadId)
						?.parts.set(n, { etag, bytes: new Uint8Array(value) });
					return { partNumber: n, etag };
				},
				async complete(parts: { partNumber: number; etag: string }[]) {
					const up = uploads.get(uploadId);
					if (!up) throw new Error("no such upload");
					const ordered = [...parts].sort(
						(a, b) => a.partNumber - b.partNumber,
					);
					const total = ordered.reduce(
						(s, p) => s + (up.parts.get(p.partNumber)?.bytes.byteLength ?? 0),
						0,
					);
					const merged = new Uint8Array(total);
					let off = 0;
					for (const p of ordered) {
						const b = up.parts.get(p.partNumber)?.bytes ?? new Uint8Array();
						merged.set(b, off);
						off += b.byteLength;
					}
					objects.set(key, { body: merged });
					uploads.delete(uploadId);
					return {};
				},
				async abort() {
					uploads.delete(uploadId);
				},
			};
		},
		resumeMultipartUpload(key: string, uploadId: string) {
			const created = bucket.createMultipartUpload.bind(bucket);
			void created;
			return {
				async uploadPart(n: number, value: ArrayBuffer) {
					const up = uploads.get(uploadId);
					if (!up || up.key !== key) throw new Error("no such upload");
					const etag = `etag-${n}`;
					up.parts.set(n, { etag, bytes: new Uint8Array(value) });
					return { partNumber: n, etag };
				},
				async complete(parts: { partNumber: number; etag: string }[]) {
					const up = uploads.get(uploadId);
					if (!up) throw new Error("no such upload");
					const ordered = [...parts].sort(
						(a, b) => a.partNumber - b.partNumber,
					);
					const total = ordered.reduce(
						(s, p) => s + (up.parts.get(p.partNumber)?.bytes.byteLength ?? 0),
						0,
					);
					const merged = new Uint8Array(total);
					let off = 0;
					for (const p of ordered) {
						const b = up.parts.get(p.partNumber)?.bytes ?? new Uint8Array();
						merged.set(b, off);
						off += b.byteLength;
					}
					objects.set(key, { body: merged });
					uploads.delete(uploadId);
					return {};
				},
				async abort() {
					uploads.delete(uploadId);
				},
			};
		},
	};
	return { bucket: bucket as unknown as R2Bucket, objects };
}

function respWithUrl(body: string, status: number, url: string): Response {
	const resp = new Response(body, { status });
	Object.defineProperty(resp, "url", { value: url });
	return resp;
}

const validInit = {
	hospital_name: "Example General Hospital",
	ccn: "050075",
	contact_name: "Pat Example",
	contact_email: "pat@example.org",
	hospital_website: "example.org",
	opt_in_listing: true,
	filename: "123456789_example-general_standardcharges.csv",
	size_bytes: 100,
};

async function initOk(env: unknown, overrides = {}) {
	const resp = await app.request(
		"/api/submissions/init",
		{
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ ...validInit, ...overrides }),
		},
		env as never,
	);
	expect(resp.status).toBe(200);
	return (await resp.json()) as {
		id: string;
		token: string;
		part_size_bytes: number;
		verify_token: string;
	};
}

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe("submission init", () => {
	it("creates a record, stores only the token hash, and starts a multipart upload", async () => {
		const { bucket, objects } = fakeBucket();
		const init = await initOk({ HL_MRF_PARSED: bucket });
		expect(init.part_size_bytes).toBe(PART_SIZE_BYTES);
		expect(init.verify_token).toContain(init.id);
		const stored = objects.get(`submissions/${init.id}/submission.json`);
		expect(stored).toBeTruthy();
		const rec = JSON.parse(new TextDecoder().decode(stored?.body));
		expect(rec.status).toBe("uploading");
		expect(rec.ccn_claimed).toBe("050075");
		expect(rec.token_sha256).toMatch(/^[0-9a-f]{64}$/);
		expect(JSON.stringify(rec)).not.toContain(init.token);
		expect(rec.listing.status).toBe("awaiting_processing");
	});

	it.each([
		["bad CCN shape", { ccn: "12345" }, 400],
		["quarantined CCN", { ccn: "150042" }, 422],
		["unsupported extension", { filename: "evil.exe" }, 422],
		["zero size", { size_bytes: 0 }, 400],
		["bad email", { contact_email: "not-an-email" }, 400],
	])("rejects %s", async (_label, patch, status) => {
		const { bucket } = fakeBucket();
		const resp = await app.request(
			"/api/submissions/init",
			{
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ ...validInit, ...patch }),
			},
			{ HL_MRF_PARSED: bucket } as never,
		);
		expect(resp.status).toBe(status);
	});
});

describe("multipart upload + completion", () => {
	it("accepts parts with the token, completes, and exposes a redacted status view", async () => {
		const { bucket, objects } = fakeBucket();
		const env = { HL_MRF_PARSED: bucket };
		const size = PART_SIZE_BYTES + 5;
		const init = await initOk(env, { size_bytes: size });
		const auth = { "x-submission-token": init.token };

		const badAuth = await app.request(
			`/api/submissions/${init.id}/parts/1`,
			{
				method: "POST",
				headers: { "x-submission-token": "nope" },
				body: new Uint8Array(8),
			},
			env as never,
		);
		expect(badAuth.status).toBe(401);

		const p1 = await app.request(
			`/api/submissions/${init.id}/parts/1`,
			{ method: "POST", headers: auth, body: new Uint8Array(PART_SIZE_BYTES) },
			env as never,
		);
		expect(p1.status).toBe(200);
		const part1 = (await p1.json()) as { part_number: number; etag: string };
		const p2 = await app.request(
			`/api/submissions/${init.id}/parts/2`,
			{ method: "POST", headers: auth, body: new Uint8Array(5) },
			env as never,
		);
		const part2 = (await p2.json()) as { part_number: number; etag: string };

		const done = await app.request(
			`/api/submissions/${init.id}/complete`,
			{
				method: "POST",
				headers: { ...auth, "content-type": "application/json" },
				body: JSON.stringify({
					parts: [part1, part2].map((p) => ({
						partNumber: p.part_number,
						etag: p.etag,
					})),
				}),
			},
			env as never,
		);
		expect(done.status).toBe(200);
		const raw = [...objects.keys()].find((k) => k.includes("/raw/"));
		expect(objects.get(raw ?? "")?.body.byteLength).toBe(size);

		const status = await app.request(
			`/api/submissions/${init.id}/status?token=${init.token}`,
			{},
			env as never,
		);
		expect(status.status).toBe(200);
		expect(status.headers.get("cache-control")).toContain("no-store");
		const view = (await status.json()) as Record<string, unknown>;
		expect(view.status).toBe("uploaded");
		expect(view.token_sha256).toBeUndefined();
		expect(view.upload_id).toBeUndefined();
	});

	it("marks a size mismatch and refuses to treat the file as uploaded", async () => {
		const { bucket, objects } = fakeBucket();
		const env = { HL_MRF_PARSED: bucket };
		const init = await initOk(env, { size_bytes: 1000 });
		const p1 = await app.request(
			`/api/submissions/${init.id}/parts/1`,
			{
				method: "POST",
				headers: { "x-submission-token": init.token },
				body: new Uint8Array(10),
			},
			env as never,
		);
		const part = (await p1.json()) as { part_number: number; etag: string };
		const done = await app.request(
			`/api/submissions/${init.id}/complete`,
			{
				method: "POST",
				headers: {
					"x-submission-token": init.token,
					"content-type": "application/json",
				},
				body: JSON.stringify({
					parts: [{ partNumber: part.part_number, etag: part.etag }],
				}),
			},
			env as never,
		);
		expect(done.status).toBe(422);
		const rec = JSON.parse(
			new TextDecoder().decode(
				objects.get(`submissions/${init.id}/submission.json`)?.body,
			),
		);
		expect(rec.status).toBe("size_mismatch");
	});

	it("rejects a gapped parts list", async () => {
		const { bucket } = fakeBucket();
		const env = { HL_MRF_PARSED: bucket };
		const init = await initOk(env, { size_bytes: PART_SIZE_BYTES + 1 });
		const done = await app.request(
			`/api/submissions/${init.id}/complete`,
			{
				method: "POST",
				headers: {
					"x-submission-token": init.token,
					"content-type": "application/json",
				},
				body: JSON.stringify({ parts: [{ partNumber: 2, etag: "x" }] }),
			},
			env as never,
		);
		expect(done.status).toBe(400);
	});
});

describe("downloads before processing", () => {
	it("404s report and normalized file until the fleet writes them", async () => {
		const { bucket } = fakeBucket();
		const env = { HL_MRF_PARSED: bucket };
		const init = await initOk(env);
		for (const kind of ["report", "normalized"]) {
			const resp = await app.request(
				`/api/submissions/${init.id}/${kind}?token=${init.token}`,
				{},
				env as never,
			);
			expect(resp.status).toBe(404);
		}
	});
});

describe("site verification", () => {
	it("verifies when the token file is on the hospital's own domain", async () => {
		const { bucket, objects } = fakeBucket();
		const env = { HL_MRF_PARSED: bucket };
		const init = await initOk(env);
		vi.stubGlobal(
			"fetch",
			vi.fn(async (url: string) =>
				String(url).includes(".well-known")
					? respWithUrl(init.verify_token, 200, String(url))
					: respWithUrl("not found", 404, String(url)),
			),
		);
		const resp = await app.request(
			`/api/submissions/${init.id}/verify-site`,
			{
				method: "POST",
				headers: {
					"content-type": "application/json",
					"x-submission-token": init.token,
				},
				body: JSON.stringify({ domain: "example.org" }),
			},
			env as never,
		);
		expect(resp.status).toBe(200);
		const body = (await resp.json()) as { verify: { status: string } };
		expect(body.verify.status).toBe("verified");
		const rec = JSON.parse(
			new TextDecoder().decode(
				objects.get(`submissions/${init.id}/submission.json`)?.body,
			),
		);
		expect(rec.verify.domain).toBe("example.org");
	});

	it("fails closed when the token is absent", async () => {
		const { bucket } = fakeBucket();
		const env = { HL_MRF_PARSED: bucket };
		const init = await initOk(env);
		vi.stubGlobal(
			"fetch",
			vi.fn(async (url: string) =>
				respWithUrl("some other text", 200, String(url)),
			),
		);
		const resp = await app.request(
			`/api/submissions/${init.id}/verify-site`,
			{
				method: "POST",
				headers: {
					"content-type": "application/json",
					"x-submission-token": init.token,
				},
				body: JSON.stringify({ domain: "example.org" }),
			},
			env as never,
		);
		const body = (await resp.json()) as { verify: { status: string } };
		expect(body.verify.status).toBe("failed");
	});
});

describe("verification edge cases", () => {
	it("rejects an IP-literal domain", async () => {
		const { bucket } = fakeBucket();
		const env = { HL_MRF_PARSED: bucket };
		const init = await initOk(env);
		const resp = await app.request(
			`/api/submissions/${init.id}/verify-site`,
			{
				method: "POST",
				headers: {
					"content-type": "application/json",
					"x-submission-token": init.token,
				},
				body: JSON.stringify({ domain: "192.168.1.1" }),
			},
			env as never,
		);
		expect(resp.status).toBe(422);
	});

	it("re-arms the listing when verification lands after processing", async () => {
		const { bucket, objects } = fakeBucket();
		const env = { HL_MRF_PARSED: bucket };
		const init = await initOk(env);
		// Simulate the fleet processor having already run while the
		// submission was unverified.
		const key = `submissions/${init.id}/submission.json`;
		const rec = JSON.parse(new TextDecoder().decode(objects.get(key)?.body));
		rec.status = "processed";
		rec.listing = {
			status: "awaiting_verification",
			reason: "not yet verified",
			verdict_batch: "",
			published_n: null,
		};
		objects.set(key, {
			body: new TextEncoder().encode(JSON.stringify(rec)),
		});
		vi.stubGlobal(
			"fetch",
			vi.fn(async (url: string) =>
				respWithUrl(init.verify_token, 200, String(url)),
			),
		);
		const resp = await app.request(
			`/api/submissions/${init.id}/verify-site`,
			{
				method: "POST",
				headers: {
					"content-type": "application/json",
					"x-submission-token": init.token,
				},
				body: JSON.stringify({ domain: "example.org" }),
			},
			env as never,
		);
		expect(resp.status).toBe(200);
		const after = JSON.parse(new TextDecoder().decode(objects.get(key)?.body));
		expect(after.verify.status).toBe("verified");
		expect(after.listing.status).toBe("awaiting_processing");
	});
});

describe("pages", () => {
	it("serves the submit form with the honest-compliance copy", async () => {
		const { bucket } = fakeBucket();
		const resp = await app.request("/submit", {}, {
			HL_MRF_PARSED: bucket,
		} as never);
		expect(resp.status).toBe(200);
		const html = await resp.text();
		expect(html).toContain("does not, by itself, meet the CMS requirement");
		expect(html).toContain('id="sub-form"');
		expect(html).toContain("/submit.js");
	});

	it("links the flow from the home page in one click", async () => {
		const { bucket } = fakeBucket();
		const resp = await app.request("/", {}, { HL_MRF_PARSED: bucket } as never);
		expect(resp.status).toBe(200);
		const html = await resp.text();
		expect(html).toContain('href="/submit"');
		expect(html).toContain('id="for-hospitals"');
	});
});
