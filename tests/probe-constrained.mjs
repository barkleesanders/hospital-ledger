// Constrained-browser probe for hospitalledger.com (2026-09-18, /ship Phase 1.45i,
// Pattern 43) — port of improvecortland/tests/probe-constrained.mjs. Drives the home
// page and a hospital page in a headless Chromium with NO WebGL and NO geolocation
// (a datacentre VM agent), intercepts every POST /api/* (nothing is ever asked of the
// model), taps each primary-action control and records what the page did. A tap that
// neither POSTs nor changes something visible is the silent class this probe exists for.
// Needs Playwright (not a dependency of this repo): from a checkout that has it,
//   ln -s /path/with/node_modules tests/node_modules
//   ./node_modules/.bin/wrangler dev -c ./wrangler.jsonc --port 8801   # the candidate
//   ASSERT=1 node tests/probe-constrained.mjs probe
// Env: PROBE_URL=<origin> — default http://127.0.0.1:8801 (the local candidate; prod
//      would show the last deploy, not this one); HOSPITAL_CCN=<6 digits> to drive
//      /hospital/<ccn> (default 251315, which must be loaded into the local R2 store);
//      ASSERT=1 to exit non-zero on the first silent tap or any native dialog. This is
//      what `npm run probe:constrained` runs.
// Output: probe-<run>.json + .png beside the script.
import fs from "node:fs";
import { chromium } from "playwright";

const RUN = process.argv[3] || "1";

const OUT = new URL(`./probe-${RUN}`, import.meta.url).pathname;

const ORIGIN = new URL(process.env.PROBE_URL || "http://127.0.0.1:8801").origin;

// A dead origin would surface as "island did not mount" — say what is actually wrong.
const alive = await fetch(`${ORIGIN}/`, { method: "HEAD" }).then(
	(r) => r.ok,
	() => false,
);

if (!alive) {
	console.error(
		`PROBE UNMEASURED: ${ORIGIN} is not serving / (start wrangler dev on :8801 or set PROBE_URL)`,
	);
	process.exit(2);
}

const HOSPITAL_CCN = process.env.HOSPITAL_CCN ?? "251315";

const browser = await chromium.launch({
	headless: true,
	args: [
		"--disable-gpu",
		"--disable-webgl",
		"--disable-webgl2",
		"--disable-3d-apis",
		"--use-gl=disabled",
		"--disable-software-rasterizer",
	],
});

// No geolocation grant, no GPS: what a VM agent has.
const ctx = await browser.newContext({
	viewport: { width: 412, height: 915 },
	userAgent:
		"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36 hospitalledger-probe",
});

const log = { dialogs: [], console: [], posts: [], popups: [], choosers: [] };

const page = await ctx.newPage();

page.on("dialog", async (d) => {
	log.dialogs.push({ type: d.type(), message: d.message(), t: Date.now() });
	await d.dismiss();
});

page.on("console", (m) => {
	if (["error", "warning"].includes(m.type()))
		log.console.push({ type: m.type(), text: m.text().slice(0, 300) });
});

page.on("pageerror", (e) =>
	log.console.push({ type: "pageerror", text: String(e).slice(0, 300) }),
);

page.on("popup", (p) => log.popups.push(p.url()));

page.on("filechooser", (fc) => {
	log.choosers.push({ t: Date.now(), multiple: fc.isMultiple() });
});

await ctx.route("**/*", async (route) => {
	const req = route.request();

	if (req.method() === "POST" && req.url().startsWith(`${ORIGIN}/api/`)) {
		const data = req.postData() || "";

		log.posts.push({ url: req.url(), t: Date.now(), bodyLen: data.length });

		return route.fulfill({
			status: 503,
			contentType: "application/json",
			body: JSON.stringify({ ok: false, error: "PROBE_INTERCEPT" }),
		});
	}

	return route.continue();
});

const text = (sel) =>
	page.evaluate((s) => {
		const e = document.querySelector(s);

		return e && e.offsetParent !== null
			? (e.textContent || "").trim().slice(0, 200)
			: null;
	}, sel);

const steps = [];

/** Only the taps (the loaded/report-loaded markers live in `steps` too). */
const taps = [];

/** Run one tap and record what changed while it ran. */
async function tap(label, act, visible) {
	const before = {
		dialogs: log.dialogs.length,
		posts: log.posts.length,
		popups: log.popups.length,
		choosers: log.choosers.length,
	};

	let failed = null;

	await act().catch((e) => {
		failed = e.message.split("\n")[0];
	});
	await page.waitForTimeout(2500);

	const s = {
		label,
		failed,
		visible: await visible(),
		delta: {
			dialogs: log.dialogs.slice(before.dialogs),
			posts: log.posts.slice(before.posts),
			popups: log.popups.slice(before.popups),
			choosers: log.choosers.slice(before.choosers),
		},
	};

	steps.push(s);
	taps.push(s);

	return s;
}

/** The "Ask anything" row: type, send, and read what the island shows. */
async function askRow(prefix, question, submitVia) {
	await page.waitForSelector("#faq-ask .faq-ask-input", { timeout: 15000 });
	await page.fill("#faq-ask .faq-ask-input", "");
	await page.type("#faq-ask .faq-ask-input", question);

	return tap(
		`${prefix}-send-${submitVia}`,
		async () => {
			if (submitVia === "enter")
				await page.press("#faq-ask .faq-ask-input", "Enter");
			else await page.click("#faq-ask .faq-ask-btn", { timeout: 5000 });
		},
		async () => ({
			error: await text("#faq-ask .faq-error"),
			shimmer: await text("#faq-ask .faq-shimmer"),
			answer: await text("#faq-ask .faq-answer"),
		}),
	);
}

await page.goto(`${ORIGIN}/`, { waitUntil: "load", timeout: 60000 });

steps.push({
	label: "loaded",
	url: page.url(),
	webgl: await page.evaluate(() => {
		try {
			const c = document.createElement("canvas");

			return !!(c.getContext("webgl") || c.getContext("webgl2"));
		} catch {
			return false;
		}
	}),
	islandMounted: await page.evaluate(
		() => !!document.querySelector("#faq-ask .faq-ask-input"),
	),
});

// The hero search: a code resolves to a navigation, a name to a picker/message.
await page.fill("#by-proc-input", "MRI knee");

await tap(
	"home-compare-search",
	() => page.click("#by-proc-form button[type=submit]", { timeout: 5000 }),
	async () => ({
		msg: await text("#by-proc-msg"),
		navigated: page.url() !== `${ORIGIN}/` ? page.url() : null,
	}),
);

if (page.url() !== `${ORIGIN}/`) {
	await page.goto(`${ORIGIN}/`, { waitUntil: "load", timeout: 60000 });
}

await askRow(
	"home",
	"Why does the homepage say fewer hospitals than CMS required?",
	"click",
);

await askRow("home", "What does the compliance grade measure?", "enter");

if (HOSPITAL_CCN) {
	await page.goto(`${ORIGIN}/hospital/${encodeURIComponent(HOSPITAL_CCN)}`, {
		waitUntil: "load",
		timeout: 60000,
	});
	steps.push({
		label: "hospital-loaded",
		url: page.url(),
		islandMounted: await page.evaluate(
			() => !!document.querySelector("#faq-ask .faq-ask-input"),
		),
	});
	await askRow("hospital", "What grade did this hospital get?", "click");
}

await page.screenshot({ path: `${OUT}.png`, fullPage: true });

fs.writeFileSync(`${OUT}.json`, JSON.stringify({ log, steps }, null, 2));

const summary = taps.map((s) => ({
	tap: s.label,
	posts: s.delta.posts.map((p) => new URL(p.url).pathname),
	choosers: s.delta.choosers.length,
	dialogs: s.delta.dialogs.length,
	visible: s.visible,
	failed: s.failed,
}));

console.log(
	JSON.stringify(
		{
			origin: ORIGIN,
			run: RUN,
			webgl: steps[0].webgl,
			island: steps[0].islandMounted,
			taps: summary,
		},
		null,
		1,
	),
);

await browser.close();

// ASSERT mode: every tap must produce a POST /api/* (intercepted here) OR a
// visible outcome — a native file chooser, error/status copy, an answer.
// Never none of them. Zero native dialogs, zero popups.
if (process.env.ASSERT === "1") {
	const failures = [];

	for (const s of taps) {
		const posted = s.delta.posts.length > 0;
		// Reporters return true, non-empty copy, or null/'' — anything truthy is on screen.
		const shown =
			s.delta.choosers.length > 0 || Object.values(s.visible).some(Boolean);

		if (!posted && !shown)
			failures.push(`${s.label}: no POST /api/*, nothing visible changed`);
	}

	if (log.dialogs.length)
		failures.push(
			`native dialogs opened: ${log.dialogs.map((d) => d.type).join(",")}`,
		);

	if (log.popups.length)
		failures.push(`popups opened: ${log.popups.join(",")}`);

	if (!steps[0].islandMounted) failures.push("faq island did not mount on /");

	if (failures.length) {
		console.error(`SILENT-OUTCOME FAIL (${RUN}):\n  ${failures.join("\n  ")}`);
		process.exit(1);
	}

	console.log(
		`SILENT-OUTCOME OK (${RUN}): ${summary.length} taps, 0 silent, 0 dialogs`,
	);
}
