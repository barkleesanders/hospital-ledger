import { Hono } from "hono";
import { afterEach, describe, expect, it, vi } from "vitest";
import { wideEvent } from "./wide-event";

function buildApp() {
	const app = new Hono();
	app.use("*", wideEvent);
	app.get("/hello", (c) => c.text("hi"));
	app.get("/boom", () => {
		throw new Error("kaboom");
	});
	return app;
}

afterEach(() => {
	vi.restoreAllMocks();
});

describe("wideEvent", () => {
	it("emits one structured http_request event per request", async () => {
		const spy = vi.spyOn(console, "log").mockImplementation(() => {});
		const res = await buildApp().request("/hello?token=<redacted>", {
			headers: { "cf-ray": "ray-123", "user-agent": "vitest" },
		});
		expect(res.status).toBe(200);
		expect(spy).toHaveBeenCalledTimes(1);
		const event = JSON.parse(spy.mock.calls[0][0] as string);
		expect(event.event).toBe("http_request");
		expect(event.method).toBe("GET");
		// Pathname only: the query string (and its token) never reaches the log.
		expect(event.path).toBe("/hello");
		expect(event.status).toBe(200);
		expect(event.outcome).toBe("ok");
		expect(event.request_id).toBe("ray-123");
		expect(typeof event.duration_ms).toBe("number");
	});

	it("marks a thrown handler as outcome=error and still emits", async () => {
		const spy = vi.spyOn(console, "log").mockImplementation(() => {});
		const res = await buildApp().request("/boom");
		expect(res.status).toBe(500);
		expect(spy).toHaveBeenCalledTimes(1);
		const event = JSON.parse(spy.mock.calls[0][0] as string);
		// Hono converts a thrown handler error into a 500 response inside the
		// pipeline, so the middleware observes it as a 500, not a throw.
		expect(event.status).toBe(500);
		expect(event.outcome).toBe("error");
	});
});
