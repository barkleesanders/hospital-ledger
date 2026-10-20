import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import app from "../index";
import { r2Passthrough, readR2Text } from "./r2";

const transient = () =>
	new Error("get: We encountered an internal error. Please try again. (10001)");

function storage(
	failures: number,
	contents: string | null,
	error = transient(),
) {
	const get = vi.fn(async (_key: string) => {
		if (failures-- > 0) throw error;
		return contents === null
			? null
			: {
					text: async () => contents,
					body: new Response(contents).body,
					httpEtag: '"fixture-etag"',
					httpMetadata: { contentType: "application/json" },
				};
	});
	return { bucket: { get } as unknown as R2Bucket, get };
}

beforeEach(() => vi.spyOn(console, "warn").mockImplementation(() => {}));
afterEach(() => vi.restoreAllMocks());

describe.each([
	["SSR text", readR2Text],
	["API stream", r2Passthrough],
] as const)("R2 %s reads", (_, read) => {
	it("recovers from two observed internal errors and preserves the object", async () => {
		const { bucket, get } = storage(2, '{"ok":true}');
		const result = await read(bucket, "aggregates/cpt-detail/27130.json");
		expect(get).toHaveBeenCalledTimes(3);
		expect(console.warn).toHaveBeenCalledWith({
			event: "r2-read-retry",
			code: 10001,
			attempt: 2,
		});
		expect(get.mock.calls.every((args) => args.length === 1)).toBe(true);
		if (result instanceof Response) {
			expect(await result.text()).toBe('{"ok":true}');
			expect(result.headers.get("etag")).toBe('"fixture-etag"');
			expect(result.headers.get("content-type")).toBe("application/json");
		} else {
			expect(result).toEqual({
				text: '{"ok":true}',
				etag: '"fixture-etag"',
				contentType: "application/json",
			});
		}
	});

	it("returns a missing object without retry", async () => {
		const { bucket, get } = storage(0, null);
		expect(await read(bucket, "missing")).toBeNull();
		expect(get).toHaveBeenCalledTimes(1);
	});

	it("stops after three attempts and propagates the original error", async () => {
		const error = transient();
		const { bucket, get } = storage(5, null, error);
		await expect(read(bucket, "unavailable")).rejects.toBe(error);
		expect(get).toHaveBeenCalledTimes(3);
	});

	it.each([
		"get: authorization failed (10001)",
		"get: We encountered an internal error. Please try again. (10002)",
		"schema error",
	])("does not retry a different failure: %s", async (message) => {
		const error = new Error(message);
		const { bucket, get } = storage(5, null, error);
		await expect(read(bucket, "protected")).rejects.toBe(error);
		expect(get).toHaveBeenCalledTimes(1);
	});
});

describe("procedure request storage recovery", () => {
	it.each(["/procedure/31006039", "/api/procedure/31006039"])(
		"returns the proper missing-record response after a transient read: %s",
		async (path) => {
			const { bucket, get } = storage(1, null);
			const response = await app.request(
				path,
				{},
				{
					HL_MRF_PARSED: bucket,
					ASSETS: {
						fetch: async () => new Response("missing", { status: 404 }),
					},
				},
			);
			expect(response.status).toBe(404);
			expect(get).toHaveBeenCalledTimes(2);
			expect(await response.text()).toContain("not indexed");
		},
	);
});
