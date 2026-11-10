/**
 * Hospital file submissions — shared record schema + helpers.
 *
 * A submission is one hospital uploading its machine-readable file (MRF)
 * directly to Hospital Ledger instead of (or before) posting it where the
 * harvester can find it. Records live as JSON in the HL_MRF_PARSED bucket
 * under `submissions/<id>/` — the webmcp-beacon precedent (small JSON
 * records written through the R2 binding, no extra database), and the
 * bucket the fleet's Mini-side S3 credentials can actually reach: the
 * hl-mrf-raw bucket answers those credentials with 403, verified
 * 2026-11-10, so the processor could not read uploads placed there.
 *
 * Trust model:
 * - The API token is returned to the submitter exactly once, at init.
 *   Only its SHA-256 is stored, so a leaked record cannot be replayed.
 * - The site-verification token is the opposite: it is meant to be
 *   published on the hospital's own website, so it is stored in the
 *   record and shown to the submitter. It proves control of a website,
 *   nothing more; the fleet binds that website to the claimed CCN
 *   before anything is listed.
 * - Processing (parse, identity gate, validation, optional publish)
 *   happens on the fleet side — see scripts/process_submissions.py.
 *   This module only defines the record both sides share.
 */

import { z } from "zod";

/** R2 multipart part size. R2 requires every part except the last to be
 * at least 5 MiB; 16 MiB keeps a 1 GiB MRF to 64 parts. */
export const PART_SIZE_BYTES = 16 * 1024 * 1024;

/** R2 multipart uploads allow at most 10,000 parts. The largest file this
 * flow can carry is therefore 10,000 × 16 MiB ≈ 160 GiB — a bound set by
 * R2 itself, not by us. The largest raw MRF the fleet has ever handled
 * is ~2.4 GB, so the headroom is ~65×. */
export const MAX_PARTS = 10_000;
export const MAX_FILE_BYTES = PART_SIZE_BYTES * MAX_PARTS;

/** Extensions the fleet parser can detect and parse (it also sniffs
 * content, but a file with no plausible extension is not an MRF). */
export const ACCEPTED_EXTENSIONS = [
	".json",
	".csv",
	".tsv",
	".txt",
	".zip",
	".xlsx",
	".xls",
	".xml",
] as const;

/** CCNs are 6 digits, or 5 digits + a letter for distinct-part units
 * (the publish guard's shape rule, publish_mini.py key_refusal). */
export const CCN_PATTERN = /^(\d{6}|\d{5}[A-Z])$/;

/** Quarantined fleet-wide (publish doctrine): never accepted, anywhere. */
export const QUARANTINED_CCN = "150042";

export const submissionStatus = z.enum([
	"uploading",
	"uploaded",
	"size_mismatch",
	"processing",
	"processed",
	"process_failed",
	"listed",
]);
export type SubmissionStatus = z.infer<typeof submissionStatus>;

export const verifyStatus = z.enum([
	"not_requested",
	"pending",
	"verified",
	"failed",
]);
export type VerifyStatus = z.infer<typeof verifyStatus>;

export const listingStatus = z.enum([
	"not_opted_in",
	"awaiting_processing",
	"awaiting_verification",
	"refused",
	"publish_deferred",
	"dry_run_ok",
	"published",
]);
export type ListingStatus = z.infer<typeof listingStatus>;

const finding = z.object({
	severity: z.enum(["error", "warning", "info"]),
	rule: z.string(),
	message: z.string(),
	count: z.number().int().nonnegative().optional(),
	examples: z.array(z.string()).max(5).optional(),
});

export const submissionRecord = z.object({
	id: z.string().uuid(),
	created_at: z.string(),
	updated_at: z.string(),
	status: submissionStatus,
	// Submitter-supplied (claims — never trusted until the fleet gates pass).
	hospital_name_claimed: z.string().min(1).max(200),
	ccn_claimed: z.string().regex(CCN_PATTERN),
	contact_name: z.string().min(1).max(200),
	contact_email: z.string().email().max(320),
	hospital_website: z.string().max(500).default(""),
	opt_in_listing: z.boolean(),
	filename: z.string().min(1).max(200),
	size_bytes_declared: z.number().int().positive(),
	// Upload state (Worker-owned until status leaves "uploading").
	raw_key: z.string(),
	upload_id: z.string().nullable(),
	token_sha256: z.string().regex(/^[0-9a-f]{64}$/),
	verify_token: z.string().min(16),
	verify: z.object({
		status: verifyStatus,
		domain: z.string().default(""),
		checked_at: z.string().nullable().default(null),
		detail: z.string().default(""),
	}),
	// Processing state (fleet-owned once claimed).
	processing: z
		.object({
			claimed_at: z.string().nullable().default(null),
			processor_version: z.string().default(""),
			sha256: z.string().default(""),
			size_bytes_actual: z.number().int().nonnegative().default(0),
			format_detected: z.string().default(""),
			source_entry_count: z.number().int().nullable().default(null),
			items_parsed: z.number().int().nonnegative().default(0),
			priced_items: z.number().int().nonnegative().default(0),
			identity_verdict: z
				.enum(["VERIFIED", "MISMATCH", "UNVERIFIABLE", ""])
				.default(""),
			identity_detail: z.string().default(""),
			findings: z.array(finding).default([]),
			report_key: z.string().default(""),
			normalized_key: z.string().default(""),
			normalized_filename: z.string().default(""),
		})
		.nullable()
		.default(null),
	listing: z
		.object({
			status: listingStatus,
			reason: z.string().default(""),
			verdict_batch: z.string().default(""),
			published_n: z.number().int().nullable().default(null),
		})
		.nullable()
		.default(null),
});
export type SubmissionRecord = z.infer<typeof submissionRecord>;

/** What the status endpoint may show the token holder: everything about
 * the submission except the credential hash and the multipart internals.
 * The contact email is the submitter's own, shown back to them only. */
export function publicView(rec: SubmissionRecord) {
	const { token_sha256: _hash, upload_id: _up, ...rest } = rec;
	return rest;
}

export function recordKey(id: string): string {
	return `submissions/${id}/submission.json`;
}

export function reportKey(id: string): string {
	return `submissions/${id}/report.json`;
}

export function rawKey(id: string, filename: string): string {
	return `submissions/${id}/raw/${filename}`;
}

/** Keep only a basename made of safe characters; fall back to a neutral
 * name. The extension survives so content sniffing and humans both win. */
export function sanitizeFilename(name: string): string {
	const base = name.split(/[\\/]/).pop() ?? "";
	const cleaned = base.replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 120);
	return cleaned.length > 0 ? cleaned : "standardcharges";
}

export function extensionOf(filename: string): string {
	const m = /\.[A-Za-z0-9]+$/.exec(filename);
	return m ? m[0].toLowerCase() : "";
}

export async function sha256Hex(text: string): Promise<string> {
	const digest = await crypto.subtle.digest(
		"SHA-256",
		new TextEncoder().encode(text),
	);
	return [...new Uint8Array(digest)]
		.map((b) => b.toString(16).padStart(2, "0"))
		.join("");
}

/** Constant-time comparison of two hex digests. */
export function digestEqual(a: string, b: string): boolean {
	if (a.length !== b.length) return false;
	let diff = 0;
	for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
	return diff === 0;
}

export function newToken(): string {
	const bytes = new Uint8Array(32);
	crypto.getRandomValues(bytes);
	return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** The file a hospital places on its own site to prove control. */
export const VERIFY_FILE_PATHS = [
	"/.well-known/hospital-ledger-verification.txt",
	"/hospital-ledger-verification.txt",
] as const;

export function verifyTokenFor(id: string, secret: string): string {
	return `hospital-ledger-verification: ${id}:${secret}`;
}
