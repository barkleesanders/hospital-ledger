/**
 * Hospital file submission API.
 *
 *   POST /api/submissions/init            create a submission + R2 multipart upload
 *   POST /api/submissions/:id/parts/:n    upload one 16 MiB part (token-gated)
 *   POST /api/submissions/:id/complete    finish the multipart upload (token-gated)
 *   POST /api/submissions/:id/abort       abandon an unfinished upload (token-gated)
 *   GET  /api/submissions/:id/status      submission state (token-gated)
 *   GET  /api/submissions/:id/report      validation report JSON (token-gated)
 *   GET  /api/submissions/:id/normalized  normalized CMS-template file (token-gated)
 *   POST /api/submissions/:id/verify-site check the verification token on the
 *                                         hospital's own website (token-gated)
 *
 * Uploads go straight into R2 through the HL_MRF_PARSED binding's
 * multipart API (submissions/ prefix; the hl-mrf-raw bucket is not
 * reachable by the fleet processor's credentials — see
 * src/lib/submissions.ts). Real MRFs run past 1 GB, and a request-body
 * upload through the Worker would fail exactly the hospitals this flow
 * exists for. The Worker never buffers more than one 16 MiB part.
 *
 * Processing is deliberately NOT done here. The fleet processor
 * (scripts/process_submissions.py, running where the parser and the
 * publish path live) claims uploaded submissions, runs the identity and
 * completeness gates, and writes the report + normalized file back to
 * the same R2 prefix. This route only moves bytes and records state.
 *
 * Nothing here can list a hospital: listing happens only in the fleet
 * processor, after the site-token check below AND the fleet's own
 * domain-binding + in-file identity gates pass.
 */

import type { Context } from "hono";
import { z } from "zod";
import { r2Passthrough, readR2Json } from "../../lib/r2";
import {
	ACCEPTED_EXTENSIONS,
	digestEqual,
	extensionOf,
	MAX_FILE_BYTES,
	newToken,
	PART_SIZE_BYTES,
	publicView,
	QUARANTINED_CCN,
	rawKey,
	recordKey,
	reportKey,
	type SubmissionRecord,
	sanitizeFilename,
	sha256Hex,
	submissionRecord,
	VERIFY_FILE_PATHS,
	verifyTokenFor,
} from "../../lib/submissions";

export type SubmissionsEnv = {
	Bindings: {
		HL_MRF_PARSED?: R2Bucket;
		SUBMIT_RATE_LIMITER?: RateLimit;
		SUBMIT_RATE_LIMITER_GLOBAL?: RateLimit;
	};
};

const NO_STORE = { "cache-control": "private, no-store" } as const;

function json(data: unknown, status = 200): Response {
	return new Response(JSON.stringify(data), {
		status,
		headers: { "content-type": "application/json", ...NO_STORE },
	});
}

function fail(status: number, error: string): Response {
	return json({ error }, status);
}

async function rateLimited(c: Context<SubmissionsEnv>): Promise<boolean> {
	const ip = c.req.header("cf-connecting-ip") ?? "unknown";
	try {
		const [perIp, global] = await Promise.all([
			c.env.SUBMIT_RATE_LIMITER?.limit({ key: ip }),
			c.env.SUBMIT_RATE_LIMITER_GLOBAL?.limit({ key: "global" }),
		]);
		return perIp?.success === false || global?.success === false;
	} catch {
		return false; // the limiter failing must not take submissions down
	}
}

async function loadRecord(
	c: Context<SubmissionsEnv>,
	id: string,
): Promise<SubmissionRecord | null> {
	if (!z.string().uuid().safeParse(id).success) return null;
	const raw = await readR2Json<unknown>(c.env.HL_MRF_PARSED, recordKey(id));
	if (!raw) return null;
	const parsed = submissionRecord.safeParse(raw);
	return parsed.success ? parsed.data : null;
}

async function saveRecord(
	c: Context<SubmissionsEnv>,
	rec: SubmissionRecord,
): Promise<void> {
	rec.updated_at = new Date().toISOString();
	await c.env.HL_MRF_PARSED?.put(recordKey(rec.id), JSON.stringify(rec), {
		httpMetadata: { contentType: "application/json" },
	});
}

/** The raw API token travels in a header for writes, or the `token`
 * query parameter for the two browser downloads and the status poll. */
async function authorized(
	c: Context<SubmissionsEnv>,
	rec: SubmissionRecord,
): Promise<boolean> {
	const presented =
		c.req.header("x-submission-token") ?? c.req.query("token") ?? "";
	if (!presented) return false;
	const digest = await sha256Hex(presented);
	return digestEqual(digest, rec.token_sha256);
}

const initBody = z.object({
	hospital_name: z.string().trim().min(1).max(200),
	ccn: z
		.string()
		.trim()
		.toUpperCase()
		.regex(
			/^(\d{6}|\d{5}[A-Z])$/,
			"CCN must be 6 digits (or 5 digits + a letter)",
		),
	contact_name: z.string().trim().min(1).max(200),
	contact_email: z.string().trim().email().max(320),
	hospital_website: z.string().trim().max(500).default(""),
	opt_in_listing: z.boolean().default(false),
	filename: z.string().trim().min(1).max(400),
	size_bytes: z.number().int().positive(),
});

export async function initSubmission(
	c: Context<SubmissionsEnv>,
): Promise<Response> {
	if (!c.env.HL_MRF_PARSED) return fail(503, "submission storage unavailable");
	if (await rateLimited(c)) {
		return fail(429, "Too many submission attempts — wait a minute and retry.");
	}
	let body: z.infer<typeof initBody>;
	try {
		body = initBody.parse(await c.req.json());
	} catch (e) {
		return fail(
			400,
			e instanceof z.ZodError ? e.issues[0].message : "invalid request body",
		);
	}
	if (body.ccn === QUARANTINED_CCN) {
		return fail(
			422,
			"That CCN is not eligible for listing; contact us before submitting.",
		);
	}
	const filename = sanitizeFilename(body.filename);
	const ext = extensionOf(filename);
	if (
		!ACCEPTED_EXTENSIONS.includes(ext as (typeof ACCEPTED_EXTENSIONS)[number])
	) {
		return fail(
			422,
			`Unsupported file type "${ext || "(none)"}". We can process: ${ACCEPTED_EXTENSIONS.join(", ")}.`,
		);
	}
	if (body.size_bytes > MAX_FILE_BYTES) {
		return fail(
			422,
			"That file is larger than this flow can carry. Post it on your hospital's website instead and our harvester will pick it up.",
		);
	}
	const id = crypto.randomUUID();
	const token = newToken();
	const verifySecret = newToken().slice(0, 32);
	const rec: SubmissionRecord = {
		id,
		created_at: new Date().toISOString(),
		updated_at: new Date().toISOString(),
		status: "uploading",
		hospital_name_claimed: body.hospital_name,
		ccn_claimed: body.ccn,
		contact_name: body.contact_name,
		contact_email: body.contact_email,
		hospital_website: body.hospital_website,
		opt_in_listing: body.opt_in_listing,
		filename,
		size_bytes_declared: body.size_bytes,
		raw_key: rawKey(id, filename),
		upload_id: null,
		token_sha256: await sha256Hex(token),
		verify_token: verifyTokenFor(id, verifySecret),
		verify: {
			status: "not_requested",
			domain: "",
			checked_at: null,
			detail: "",
		},
		processing: null,
		listing: body.opt_in_listing
			? {
					status: "awaiting_processing",
					reason: "",
					verdict_batch: "",
					published_n: null,
				}
			: {
					status: "not_opted_in",
					reason: "",
					verdict_batch: "",
					published_n: null,
				},
	};
	try {
		const upload = await c.env.HL_MRF_PARSED.createMultipartUpload(
			rec.raw_key,
			{
				httpMetadata: { contentType: "application/octet-stream" },
			},
		);
		rec.upload_id = upload.uploadId;
	} catch (e) {
		console.error({
			event: "submit-init-multipart-failed",
			id,
			error: String(e),
		});
		return fail(502, "Could not start the upload. Please retry.");
	}
	await saveRecord(c, rec);
	console.log({
		event: "submit-init",
		id,
		ccn: rec.ccn_claimed,
		size_bytes: rec.size_bytes_declared,
		opt_in: rec.opt_in_listing,
	});
	return json({
		id,
		token,
		part_size_bytes: PART_SIZE_BYTES,
		verify_token: rec.verify_token,
		status_path: `/submit/status/${id}`,
	});
}

async function withRecord(
	c: Context<SubmissionsEnv>,
	fn: (rec: SubmissionRecord) => Promise<Response>,
): Promise<Response> {
	const rec = await loadRecord(c, c.req.param("id") ?? "");
	if (!rec) return fail(404, "submission not found");
	if (!(await authorized(c, rec)))
		return fail(401, "missing or wrong submission token");
	return fn(rec);
}

export async function uploadPart(
	c: Context<SubmissionsEnv>,
): Promise<Response> {
	return withRecord(c, async (rec) => {
		if (rec.status !== "uploading" || !rec.upload_id || !c.env.HL_MRF_PARSED) {
			return fail(409, "this submission is not accepting parts");
		}
		const n = Number(c.req.param("n"));
		const expectedParts = Math.ceil(rec.size_bytes_declared / PART_SIZE_BYTES);
		if (!Number.isInteger(n) || n < 1 || n > expectedParts) {
			return fail(400, `part number must be between 1 and ${expectedParts}`);
		}
		const declared = Number(c.req.header("content-length") ?? "0");
		if (declared > PART_SIZE_BYTES) {
			return fail(413, `part exceeds the ${PART_SIZE_BYTES}-byte part size`);
		}
		const body = await c.req.arrayBuffer();
		if (body.byteLength === 0 || body.byteLength > PART_SIZE_BYTES) {
			return fail(413, "part is empty or exceeds the part size");
		}
		const upload = c.env.HL_MRF_PARSED.resumeMultipartUpload(
			rec.raw_key,
			rec.upload_id,
		);
		try {
			const part = await upload.uploadPart(n, body);
			return json({ part_number: part.partNumber, etag: part.etag });
		} catch (e) {
			console.error({
				event: "submit-part-failed",
				id: rec.id,
				n,
				error: String(e),
			});
			return fail(502, "part upload failed; retry this part");
		}
	});
}

const completeBody = z.object({
	parts: z
		.array(
			z.object({
				partNumber: z.number().int().positive(),
				etag: z.string().min(1),
			}),
		)
		.min(1)
		.max(10_000),
});

export async function completeSubmission(
	c: Context<SubmissionsEnv>,
): Promise<Response> {
	return withRecord(c, async (rec) => {
		if (rec.status !== "uploading" || !rec.upload_id || !c.env.HL_MRF_PARSED) {
			return fail(409, "this submission is not awaiting completion");
		}
		let body: z.infer<typeof completeBody>;
		try {
			body = completeBody.parse(await c.req.json());
		} catch {
			return fail(400, "invalid parts list");
		}
		const expectedParts = Math.ceil(rec.size_bytes_declared / PART_SIZE_BYTES);
		const numbers = body.parts.map((p) => p.partNumber).sort((a, b) => a - b);
		const contiguous =
			numbers.length === expectedParts && numbers.every((n, i) => n === i + 1);
		if (!contiguous) {
			return fail(
				400,
				`parts must be exactly 1..${expectedParts} with no gaps`,
			);
		}
		const upload = c.env.HL_MRF_PARSED.resumeMultipartUpload(
			rec.raw_key,
			rec.upload_id,
		);
		try {
			await upload.complete(body.parts);
		} catch (e) {
			console.error({
				event: "submit-complete-failed",
				id: rec.id,
				error: String(e),
			});
			return fail(502, "could not complete the upload; retry completion");
		}
		const head = await c.env.HL_MRF_PARSED.head(rec.raw_key);
		if (!head) return fail(502, "uploaded object not found after completion");
		if (head.size !== rec.size_bytes_declared) {
			rec.status = "size_mismatch";
			await saveRecord(c, rec);
			console.warn({
				event: "submit-size-mismatch",
				id: rec.id,
				declared: rec.size_bytes_declared,
				actual: head.size,
			});
			return fail(
				422,
				`uploaded size (${head.size}) does not match the declared size (${rec.size_bytes_declared}); the file will not be processed`,
			);
		}
		rec.status = "uploaded";
		await saveRecord(c, rec);
		console.log({
			event: "submit-complete",
			id: rec.id,
			size_bytes: head.size,
		});
		return json({ id: rec.id, status: rec.status, size_bytes: head.size });
	});
}

export async function abortSubmission(
	c: Context<SubmissionsEnv>,
): Promise<Response> {
	return withRecord(c, async (rec) => {
		if (rec.status === "uploading" && rec.upload_id && c.env.HL_MRF_PARSED) {
			try {
				await c.env.HL_MRF_PARSED.resumeMultipartUpload(
					rec.raw_key,
					rec.upload_id,
				).abort();
			} catch {
				// An already-finished upload cannot be aborted; state below stands.
			}
			rec.upload_id = null;
			await saveRecord(c, rec);
		}
		return json({ id: rec.id, status: rec.status });
	});
}

export async function submissionStatus(
	c: Context<SubmissionsEnv>,
): Promise<Response> {
	return withRecord(c, async (rec) => json(publicView(rec)));
}

export async function submissionReport(
	c: Context<SubmissionsEnv>,
): Promise<Response> {
	return withRecord(c, async (rec) => {
		const resp = await r2Passthrough(c.env.HL_MRF_PARSED, reportKey(rec.id), {
			cacheControl: "private, no-store",
		});
		if (!resp)
			return fail(404, "no report yet — the file has not been processed");
		const headers = new Headers(resp.headers);
		headers.set(
			"content-disposition",
			`attachment; filename="hospital-ledger-validation-report-${rec.id}.json"`,
		);
		return new Response(resp.body, { status: resp.status, headers });
	});
}

export async function submissionNormalized(
	c: Context<SubmissionsEnv>,
): Promise<Response> {
	return withRecord(c, async (rec) => {
		const key = rec.processing?.normalized_key;
		if (!key)
			return fail(
				404,
				"no normalized file yet — the file has not been processed",
			);
		const resp = await r2Passthrough(c.env.HL_MRF_PARSED, key, {
			cacheControl: "private, no-store",
		});
		if (!resp) return fail(404, "normalized file missing");
		const headers = new Headers(resp.headers);
		headers.set("content-type", "application/json");
		headers.set(
			"content-disposition",
			`attachment; filename="${rec.processing?.normalized_filename || "standardcharges.json"}"`,
		);
		return new Response(resp.body, { status: resp.status, headers });
	});
}

const verifyBody = z.object({ domain: z.string().trim().max(253).optional() });

function hostnameOf(value: string): string | null {
	const v = value.trim().toLowerCase();
	if (!v) return null;
	try {
		const host = v.includes("://") ? new URL(v).hostname : v.split("/")[0];
		if (
			!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/.test(
				host,
			)
		) {
			return null;
		}
		// A hospital website is a DNS name. An IP literal is never that,
		// and the verification fetch must not become a probe of arbitrary
		// addresses.
		if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host)) return null;
		return host;
	} catch {
		return null;
	}
}

/** Read a response body with a hard byte cap (verification files are one
 * line; anything past 64 KiB is not a token file). */
async function readCapped(resp: Response, cap: number): Promise<string> {
	if (!resp.body) return "";
	const reader = resp.body.getReader();
	const chunks: Uint8Array[] = [];
	let total = 0;
	for (;;) {
		const { done, value } = await reader.read();
		if (done) break;
		total += value.byteLength;
		if (total > cap) {
			await reader.cancel();
			break;
		}
		chunks.push(value);
	}
	const merged = new Uint8Array(total > cap ? cap : total);
	let off = 0;
	for (const ch of chunks) {
		const take = Math.min(ch.byteLength, merged.byteLength - off);
		if (take <= 0) break;
		merged.set(ch.subarray(0, take), off);
		off += take;
	}
	return new TextDecoder().decode(merged);
}

export async function verifySite(
	c: Context<SubmissionsEnv>,
): Promise<Response> {
	if (await rateLimited(c)) {
		return fail(
			429,
			"Too many verification attempts — wait a minute and retry.",
		);
	}
	return withRecord(c, async (rec) => {
		let domain: string | null = null;
		try {
			const body = verifyBody.parse(await c.req.json().catch(() => ({})));
			// An explicitly supplied domain is authoritative: if it is
			// not a valid DNS hostname the request is wrong, and silently
			// falling back to the form's website would hide that. Only an
			// absent domain falls back to the website on the record.
			const rawDomain = (body.domain ?? "").trim();
			domain = rawDomain
				? hostnameOf(rawDomain)
				: hostnameOf(rec.hospital_website);
		} catch {
			return fail(400, "invalid body");
		}
		if (!domain) {
			return fail(
				422,
				"no valid hospital domain to check — provide the hospital's website domain",
			);
		}
		let matchedPath: string | null = null;
		let lastDetail = "";
		for (const path of VERIFY_FILE_PATHS) {
			try {
				const resp: Response = await fetch(`https://${domain}${path}`, {
					redirect: "follow",
					headers: { "user-agent": "HospitalLedger-Verification/1.0" },
				});
				const finalHost: string = new URL(resp.url).hostname;
				if (finalHost !== domain) {
					lastDetail = `${path} redirected to a different host (${finalHost})`;
					continue;
				}
				if (!resp.ok) {
					lastDetail = `${path} answered HTTP ${resp.status}`;
					continue;
				}
				const text = await readCapped(resp, 64 * 1024);
				if (text.includes(rec.verify_token)) {
					matchedPath = path;
					break;
				}
				lastDetail = `${path} did not contain this submission's verification token`;
			} catch (e) {
				lastDetail = `${path} could not be fetched (${String(e).slice(0, 120)})`;
			}
		}
		rec.verify = {
			status: matchedPath ? "verified" : "failed",
			domain,
			checked_at: new Date().toISOString(),
			detail: matchedPath
				? `token found at https://${domain}${matchedPath}`
				: lastDetail,
		};
		if (matchedPath && rec.listing?.status === "awaiting_verification") {
			// Processing may already have run while the site was
			// unverified. Re-arm the listing so the fleet processor
			// picks it up on its next pass instead of stranding it.
			rec.listing.status = "awaiting_processing";
		}
		if (!rec.opt_in_listing && matchedPath) {
			// Verification without the opt-in is recorded but lists nothing.
			rec.verify.detail += " (submission did not opt in to listing)";
		}
		await saveRecord(c, rec);
		console.log({
			event: "submit-verify",
			id: rec.id,
			domain,
			verified: Boolean(matchedPath),
		});
		return json({ id: rec.id, verify: rec.verify });
	});
}
