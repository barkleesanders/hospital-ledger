/* Hospital file submission — browser driver for /submit and
 * /submit/status/:id. Uploads go in 16 MiB parts straight to R2 through
 * the Worker's multipart endpoints; this file never holds more than one
 * part in flight per lane. All server- or file-derived strings are
 * rendered with textContent — a submitted file's contents are untrusted
 * input and must never become markup.
 *
 * Outcome discipline: the submit control locks while a run is in flight
 * and every run ends by re-reading the server-side status and reporting
 * what the server says — never a local "success" the server didn't
 * confirm. */
(() => {
	const LS_KEY = "hl-submission";

	function $(id) {
		return document.getElementById(id);
	}

	function sleep(ms) {
		return new Promise((r) => {
			setTimeout(r, ms);
		});
	}

	function fmtBytes(n) {
		if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
		if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
		if (n >= 1e3) return `${(n / 1e3).toFixed(1)} KB`;
		return `${n} B`;
	}

	async function api(path, opts) {
		const resp = await fetch(path, opts);
		let data = null;
		try {
			data = await resp.json();
		} catch {
			/* non-JSON error body */
		}
		if (!resp.ok) {
			throw new Error(data?.error || `Request failed (HTTP ${resp.status})`);
		}
		return data;
	}

	function el(tag, cls, text) {
		const node = document.createElement(tag);
		if (cls) node.className = cls;
		if (text != null) node.textContent = text;
		return node;
	}

	/* ---------------- /submit form mode ---------------- */

	async function run() {
		const btn = $("sub-submit");
		const outcome = $("sub-outcome");
		const progress = $("sub-progress");
		const bar = $("sub-progress-bar");
		const ptext = $("sub-progress-text");
		outcome.textContent = "";
		outcome.style.color = "";
		const file = $("sub-file").files[0];
		try {
			if (!file)
				throw new Error("Choose your hospital's machine-readable file first.");
			if (file.size === 0)
				throw new Error("That file is empty — choose the actual price file.");
			const payload = {
				hospital_name: $("sub-hospital-name").value.trim(),
				ccn: $("sub-ccn").value.trim().toUpperCase(),
				contact_name: $("sub-contact-name").value.trim(),
				contact_email: $("sub-contact-email").value.trim(),
				hospital_website: $("sub-website").value.trim(),
				opt_in_listing: $("sub-optin").checked,
				filename: file.name,
				size_bytes: file.size,
			};
			if (
				!payload.hospital_name ||
				!payload.ccn ||
				!payload.contact_name ||
				!payload.contact_email
			) {
				throw new Error(
					"Fill in the hospital name, CCN, your name, and your email.",
				);
			}
			btn.disabled = true;
			btn.setAttribute("aria-busy", "true");
			progress.classList.remove("hidden");
			ptext.textContent = "Starting the upload…";

			const init = await api("/api/submissions/init", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify(payload),
			});
			const token = init.token;
			const partSize = init.part_size_bytes;
			const totalParts = Math.ceil(file.size / partSize);
			const parts = new Array(totalParts);
			let doneParts = 0;
			let nextPart = 1;

			const uploadPart = async (n) => {
				const start = (n - 1) * partSize;
				const blob = file.slice(start, Math.min(start + partSize, file.size));
				let lastErr = null;
				for (let attempt = 1; attempt <= 4; attempt++) {
					try {
						const resp = await fetch(`/api/submissions/${init.id}/parts/${n}`, {
							method: "POST",
							headers: { "x-submission-token": token },
							body: blob,
						});
						if (!resp.ok) {
							let d = null;
							try {
								d = await resp.json();
							} catch {}
							throw new Error(d?.error || `HTTP ${resp.status}`);
						}
						const part = await resp.json();
						parts[n - 1] = { partNumber: part.part_number, etag: part.etag };
						doneParts++;
						const pct = Math.round((doneParts / totalParts) * 100);
						bar.style.width = `${pct}%`;
						ptext.textContent = `Uploading ${file.name} — ${fmtBytes(file.size)} — part ${doneParts} of ${totalParts} (${pct}%)`;
						return;
					} catch (e) {
						lastErr = e;
						if (attempt < 4) await sleep(750 * attempt);
					}
				}
				throw new Error(`Part ${n} failed after retries: ${lastErr.message}`);
			};

			const lane = async () => {
				for (;;) {
					const n = nextPart++;
					if (n > totalParts) return;
					await uploadPart(n);
				}
			};
			await Promise.all([lane(), lane(), lane()]);

			ptext.textContent = "Finishing the upload…";
			await api(`/api/submissions/${init.id}/complete`, {
				method: "POST",
				headers: {
					"content-type": "application/json",
					"x-submission-token": token,
				},
				body: JSON.stringify({ parts }),
			});

			/* Confirm from the server, not from local state. */
			const status = await api(
				`/api/submissions/${init.id}/status?token=${encodeURIComponent(token)}`,
			);
			try {
				localStorage.setItem(LS_KEY, JSON.stringify({ id: init.id, token }));
			} catch {}
			const link = $("sub-status-link");
			const statusUrl = `${location.origin}/submit/status/${init.id}?token=${encodeURIComponent(token)}`;
			link.href = statusUrl;
			link.textContent = statusUrl;
			if (payload.opt_in_listing) {
				$("sub-verify-token").textContent = init.verify_token;
				$("sub-verify-box").classList.remove("hidden");
			}
			$("sub-started").classList.remove("hidden");
			progress.classList.add("hidden");
			outcome.style.color = "#6ee7b7";
			outcome.textContent = `Upload received (${fmtBytes(status.size_bytes_declared)}). Status: ${status.status}. Processing runs on our side — your report and normalized file will appear on your status page.`;
		} catch (e) {
			outcome.style.color = "#fca5a5";
			outcome.textContent = `Upload did not finish: ${e.message}`;
			ptext.textContent = "";
		} finally {
			btn.disabled = false;
			btn.removeAttribute("aria-busy");
		}
	}

	/* ---------------- /submit/status/:id mode ---------------- */

	function tokenFor(id) {
		const q = new URLSearchParams(location.search).get("token");
		if (q) return q;
		try {
			const saved = JSON.parse(localStorage.getItem(LS_KEY) || "null");
			if (saved && saved.id === id) return saved.token;
		} catch {}
		return null;
	}

	async function initStatus(root) {
		const id = root.getAttribute("data-submission-id");
		const token = tokenFor(id);
		const errBox = $("status-error");
		if (!token) {
			$("status-loading").classList.add("hidden");
			errBox.classList.remove("hidden");
			errBox.textContent =
				"This status page needs the token from your status link. Open the exact link you were given when you submitted — it carries the token, and it can't be recovered if lost.";
			return;
		}
		if (!location.search.includes("token=")) {
			history.replaceState(
				null,
				"",
				`${location.pathname}?token=${encodeURIComponent(token)}`,
			);
		}
		const terminal = {
			processed: true,
			process_failed: true,
			listed: true,
			size_mismatch: true,
		};
		for (;;) {
			let rec;
			try {
				rec = await api(
					`/api/submissions/${id}/status?token=${encodeURIComponent(token)}`,
				);
			} catch (e) {
				$("status-loading").classList.add("hidden");
				errBox.classList.remove("hidden");
				errBox.textContent = `Could not load this submission: ${e.message}`;
				return;
			}
			renderStatus(rec, token);
			if (terminal[rec.status]) return;
			await sleep(6000);
		}
	}

	function renderStatus(rec, token) {
		$("status-loading").classList.add("hidden");
		$("status-body").classList.remove("hidden");
		const phase = $("status-phase");
		const detail = $("status-detail");
		const p = rec.processing || {};
		const phaseText = {
			uploading: "Your file is still uploading.",
			uploaded: "Your file is uploaded and waiting for processing.",
			size_mismatch:
				"The uploaded file's size did not match the file you selected, so it will not be processed. Please submit again.",
			processing: "Your file is being processed now.",
			processed: "Processing is complete.",
			process_failed:
				"Processing failed on our side. The failure is recorded; if it persists, submit again or post the file on your hospital's website and our harvester will find it.",
			listed:
				"Processing is complete, and your hospital is listed from this submission.",
		};
		phase.textContent = phaseText[rec.status] || rec.status;
		const bits = [];
		bits.push(`${rec.hospital_name_claimed} · CCN ${rec.ccn_claimed}`);
		bits.push(`${rec.filename} (${fmtBytes(rec.size_bytes_declared)})`);
		if (p.items_parsed)
			bits.push(
				`${p.items_parsed.toLocaleString("en-US")} charge entries parsed`,
			);
		if (p.sha256) bits.push(`file SHA-256 ${p.sha256.slice(0, 16)}…`);
		detail.textContent = bits.join(" · ");

		/* Downloads */
		const dl = $("status-downloads");
		const hasNormalized = Boolean(p.normalized_key);
		const hasReport = Boolean(p.report_key);
		if (hasNormalized || hasReport) {
			dl.classList.remove("hidden");
			if (hasNormalized) {
				const a = $("dl-normalized");
				a.classList.remove("hidden");
				a.href = `/api/submissions/${rec.id}/normalized?token=${encodeURIComponent(token)}`;
			}
			if (hasReport) {
				const r = $("dl-report");
				r.classList.remove("hidden");
				r.href = `/api/submissions/${rec.id}/report?token=${encodeURIComponent(token)}`;
			}
		}

		/* Findings */
		const findings = p.findings || [];
		if (findings.length > 0) {
			$("status-findings").classList.remove("hidden");
			const errs = findings.filter((f) => f.severity === "error").length;
			const warns = findings.filter((f) => f.severity === "warning").length;
			$("findings-summary").textContent =
				`${errs} error${errs === 1 ? "" : "s"}, ${warns} warning${warns === 1 ? "" : "s"}. ` +
				"Errors are things the CMS template requires that your file is missing or gets wrong; warnings are gaps that weaken the file.";
			const list = $("findings-list");
			list.textContent = "";
			findings.forEach((f) => {
				const li = el(
					"li",
					"rounded-lg border border-zinc-800 bg-zinc-900/40 p-3",
				);
				const badge = el(
					"span",
					`mr-2 rounded px-1.5 py-0.5 text-xs font-semibold ${
						f.severity === "error"
							? "bg-red-900/60 text-red-200"
							: f.severity === "warning"
								? "bg-amber-900/60 text-amber-200"
								: "bg-zinc-800 text-zinc-300"
					}`,
					f.severity.toUpperCase(),
				);
				li.appendChild(badge);
				li.appendChild(el("span", "text-zinc-200", f.message));
				if (f.count != null) {
					li.appendChild(
						el(
							"div",
							"mt-1 text-xs text-zinc-500",
							`Affects ${f.count.toLocaleString("en-US")} item(s).`,
						),
					);
				}
				if (f.examples?.length) {
					li.appendChild(
						el(
							"div",
							"mt-1 text-xs text-zinc-500",
							`Examples: ${f.examples.join(" · ")}`,
						),
					);
				}
				list.appendChild(li);
			});
		}

		/* Verification */
		const verifyBox = $("status-verify");
		if (rec.opt_in_listing && rec.verify && rec.verify.status !== "verified") {
			verifyBox.classList.remove("hidden");
			$("status-verify-token").textContent = rec.verify_token;
			const dom = $("verify-domain");
			if (!dom.value && rec.hospital_website) {
				try {
					dom.value = new URL(
						rec.hospital_website.includes("://")
							? rec.hospital_website
							: `https://${rec.hospital_website}`,
					).hostname;
				} catch {}
			}
			const btn = $("verify-check");
			if (!btn.dataset.bound) {
				btn.dataset.bound = "1";
				btn.addEventListener("click", async () => {
					const out = $("verify-outcome");
					btn.disabled = true;
					out.textContent = "Checking your website…";
					try {
						const res = await api(`/api/submissions/${rec.id}/verify-site`, {
							method: "POST",
							headers: {
								"content-type": "application/json",
								"x-submission-token": token,
							},
							body: JSON.stringify({ domain: $("verify-domain").value.trim() }),
						});
						out.style.color =
							res.verify.status === "verified" ? "#6ee7b7" : "#fca5a5";
						out.textContent =
							res.verify.status === "verified"
								? `Verified — ${res.verify.detail}. Your listing request now goes through the standard identity and completeness gates.`
								: `Not verified yet: ${res.verify.detail}`;
					} catch (e) {
						out.style.color = "#fca5a5";
						out.textContent = `Check failed: ${e.message}`;
					} finally {
						btn.disabled = false;
					}
				});
			}
		} else if (rec.verify && rec.verify.status === "verified") {
			verifyBox.classList.add("hidden");
		}

		/* Listing outcome */
		if (rec.listing && rec.listing.status !== "awaiting_processing") {
			$("status-listing").classList.remove("hidden");
			const text = {
				not_opted_in:
					"You didn't opt in to listing, so this submission was processed for the report and the normalized file only. Your prices reach the Ledger the usual way: post the file on your hospital's website and our harvester will pick it up.",
				awaiting_verification:
					"Your listing is waiting on website verification (above). Once verified, it goes through the standard identity and completeness gates.",
				refused: `Your submission could not be listed: ${rec.listing.reason || "a gate did not pass"} It was still processed — the report and normalized file above are yours to use.`,
				publish_deferred: `Your submission passed the gates and is queued for the next publishing run: ${rec.listing.reason || ""}`,
				dry_run_ok:
					"Your submission passed every gate and a full dry-run publish. It will be listed in the next publishing run.",
				published: `Your hospital is listed in the Ledger from this submission${
					rec.listing.published_n
						? ` (${rec.listing.published_n.toLocaleString("en-US")} priced items)`
						: ""
				}.`,
			};
			$("listing-text").textContent =
				text[rec.listing.status] || rec.listing.status;
		}
	}

	/* ---------------- wiring (after every declaration) ---------------- */

	const form = $("sub-form");
	if (form) {
		let running = false;
		form.addEventListener("submit", (ev) => {
			ev.preventDefault();
			if (running) return;
			running = true;
			run().then(
				() => {
					running = false;
				},
				() => {
					running = false;
				},
			);
		});
	}

	const root = $("status-root");
	if (root) {
		initStatus(root);
	}
})();
