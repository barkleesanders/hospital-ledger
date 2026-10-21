// Procedure page client-side filter/sort interactions.
// Filters stage in the UI and only apply when the user clicks "Update search"
// or presses Enter. Supports URL query params for shareable filtered URLs.

(() => {
	const $ = (id) => document.getElementById(id);

	// State
	const allHospitals = [];
	let stagedFilters = { state: "", payer: "", sort: "median-first" };
	let appliedFilters = { state: "", payer: "", sort: "median-first" };
	let cashMedian = 0;

	function init() {
		// Extract hospital data from SSR DOM
		const results = $("results");
		const resultsMobile = $("results-mobile");
		if (!results && !resultsMobile) return;

		// Parse initial hospital data from SSR table rows
		parseHospitalData();

		// Read filter options from data attributes
		const filterSection = document.querySelector("[data-states]");
		if (!filterSection) return;

		// Read median from somewhere (for median-first sort)
		const stats = document.querySelector("[data-cash-median]");
		if (stats) {
			cashMedian = parseFloat(stats.getAttribute("data-cash-median")) || 0;
		}

		// Read URL query params and apply them immediately
		readUrlParams();

		// Wire up filter controls
		const stateSelect = $("filter-state");
		const payerSelect = $("filter-payer");
		const sortSelect = $("sort-by");

		if (stateSelect) {
			stateSelect.addEventListener("change", () => {
				stagedFilters.state = stateSelect.value;
			});
		}

		if (payerSelect) {
			payerSelect.addEventListener("change", () => {
				stagedFilters.payer = payerSelect.value;
			});
		}

		if (sortSelect) {
			sortSelect.addEventListener("change", () => {
				stagedFilters.sort = sortSelect.value;
			});
		}

		// Add "Update search" button
		addUpdateButton();

		// Wire up Enter key on filter controls
		[stateSelect, payerSelect, sortSelect].forEach((el) => {
			if (el) {
				el.addEventListener("keydown", (e) => {
					if (e.key === "Enter") {
						e.preventDefault();
						applyFilters();
					}
				});
			}
		});
		renderAppliedFilters();
		window.addEventListener("popstate", () => {
			readUrlParams();
			renderAppliedFilters();
		});
	}

	function parseHospitalData() {
		// The SSR page renders both layouts. Match each table row to its card,
		// retaining duplicate CCNs in the same order rather than dropping cards.
		const mobileCards = new Map();
		for (const card of document.querySelectorAll(
			'#results-mobile a[href^="/hospital/"]',
		)) {
			const ccn = card.getAttribute("href").split("/").pop();
			if (!mobileCards.has(ccn)) mobileCards.set(ccn, []);
			mobileCards.get(ccn).push(card);
		}
		// Parse hospital data from SSR table rows
		const tbody = $("results");
		if (tbody) {
			const rows = tbody.querySelectorAll("tr");
			rows.forEach((row) => {
				const cells = row.querySelectorAll("td");
				if (cells.length >= 4) {
					const nameLink = cells[0].querySelector("a");
					const name = nameLink ? nameLink.textContent.trim() : "";
					const href = nameLink ? nameLink.getAttribute("href") : "";
					const ccn = href ? href.split("/").pop() : "";
					const state = cells[1].textContent.trim();
					const grossText = cells[2].textContent.trim();
					const cashText = cells[3].textContent.trim();
					const minText = cells.length > 4 ? cells[4].textContent.trim() : "—";
					const maxText = cells.length > 5 ? cells[5].textContent.trim() : "—";

					// Extract payers from the row's data
					const payersCell = cells.length > 6 ? cells[6] : null;
					const payers = [];
					if (payersCell) {
						// Payers might be in a data attribute or we need to fetch them
						const payerData = row.getAttribute("data-payers");
						if (payerData) {
							try {
								payers.push(...JSON.parse(payerData));
							} catch (_e) {
								// ignore
							}
						}
					}

					const quality = row.classList.contains("opacity-70")
						? "flagged"
						: "normal";

					allHospitals.push({
						ccn,
						name,
						state,
						gross: parseMoney(grossText),
						cash: parseMoney(cashText),
						min: parseMoney(minText),
						max: parseMoney(maxText),
						payers,
						quality,
						element: row,
						mobileElement: mobileCards.get(ccn)?.shift(),
					});
				}
			});
		}

		// Also parse mobile cards if present
		const mobileCont = $("results-mobile");
		if (mobileCont && allHospitals.length === 0) {
			const cards = mobileCont.querySelectorAll('a[href^="/hospital/"]');
			cards.forEach((card) => {
				const href = card.getAttribute("href");
				const ccn = href ? href.split("/").pop() : "";
				const nameEl = card.querySelector(".font-medium");
				const name = nameEl ? nameEl.textContent.trim() : "";
				const stateEl = card.querySelector(".text-xs.text-zinc-500");
				const state = stateEl ? stateEl.textContent.trim() : "";

				// Parse cash price
				const cashEl = card.querySelector(".text-emerald-300");
				const cashText = cashEl ? cashEl.textContent.trim() : "—";
				const cash = parseMoney(cashText);

				const quality = card.classList.contains("opacity-70")
					? "flagged"
					: "normal";

				allHospitals.push({
					ccn,
					name,
					state,
					cash,
					quality,
					element: card,
					payers: [],
				});
			});
		}
	}

	function parseMoney(text) {
		if (!text || text === "—") return null;
		const cleaned = text.replace(/[$,]/g, "");
		const num = parseFloat(cleaned);
		return Number.isNaN(num) ? null : num;
	}

	function readUrlParams() {
		const params = new URLSearchParams(location.search);
		appliedFilters.state = params.get("state") || "";
		appliedFilters.payer = params.get("payer") || "";
		appliedFilters.sort = params.get("sort") || "median-first";

		// Copy to staged
		stagedFilters = { ...appliedFilters };
		for (const [id, value] of [
			["filter-state", appliedFilters.state],
			["filter-payer", appliedFilters.payer],
			["sort-by", appliedFilters.sort],
		]) {
			const control = $(id);
			if (control) control.value = value;
		}
	}

	function addUpdateButton() {
		const filterSection = document.querySelector("[data-states]");
		if (!filterSection) return;

		const btnContainer = document.createElement("div");
		btnContainer.className = "mt-3 flex flex-wrap items-center gap-3";
		btnContainer.innerHTML = `
      <button id="apply-filters-btn" class="rounded-md bg-emerald-700 hover:bg-emerald-800 text-white px-4 py-2 text-sm font-medium transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-300">
        Update search
      </button>
      <button id="clear-filters-btn" class="rounded-md border border-zinc-500 text-zinc-100 hover:bg-zinc-800 px-4 py-2 text-sm transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-300">
        Clear filters
      </button>
      <span id="filter-status" class="text-xs text-zinc-400"></span>
    `;

		filterSection.appendChild(btnContainer);

		$("apply-filters-btn")?.addEventListener("click", applyFilters);
		$("clear-filters-btn")?.addEventListener("click", clearFilters);
	}

	function applyFilters() {
		// Copy staged to applied
		appliedFilters = { ...stagedFilters };

		// Update URL
		updateUrl();
		renderAppliedFilters();
	}

	function renderAppliedFilters() {
		// Filter and sort
		renderFilteredResults();

		// Update status
		const status = $("filter-status");
		if (status) {
			const parts = [];
			if (appliedFilters.state) parts.push(`State: ${appliedFilters.state}`);
			if (appliedFilters.payer) parts.push(`Payer: ${appliedFilters.payer}`);
			if (parts.length > 0) {
				status.textContent = `Filtered: ${parts.join(" · ")}`;
			} else {
				status.textContent = "";
			}
		}
	}

	function clearFilters() {
		stagedFilters = { state: "", payer: "", sort: "median-first" };
		appliedFilters = { state: "", payer: "", sort: "median-first" };

		// Reset selects
		const stateSelect = $("filter-state");
		const payerSelect = $("filter-payer");
		const sortSelect = $("sort-by");

		if (stateSelect) stateSelect.value = "";
		if (payerSelect) payerSelect.value = "";
		if (sortSelect) sortSelect.value = "median-first";

		// Update URL
		updateUrl();

		renderAppliedFilters();
	}

	function updateUrl() {
		const params = new URLSearchParams(location.search);
		for (const key of ["state", "payer", "sort"]) params.delete(key);
		if (appliedFilters.state) params.set("state", appliedFilters.state);
		if (appliedFilters.payer) params.set("payer", appliedFilters.payer);
		if (appliedFilters.sort && appliedFilters.sort !== "median-first") {
			params.set("sort", appliedFilters.sort);
		}

		const path = params.toString()
			? `${location.pathname}?${params.toString()}`
			: location.pathname;
		const newUrl = path + location.hash;
		if (newUrl !== location.pathname + location.search + location.hash) {
			history.pushState(null, "", newUrl);
		}
	}

	function renderFilteredResults() {
		let filtered = [...allHospitals];

		// Apply state filter
		if (appliedFilters.state) {
			filtered = filtered.filter((h) => h.state === appliedFilters.state);
		}

		// Apply payer filter
		if (appliedFilters.payer) {
			filtered = filtered.filter((h) => {
				return h.payers?.some(
					(p) =>
						(p.slug && p.slug === appliedFilters.payer) ||
						p.p?.toLowerCase().includes(appliedFilters.payer.toLowerCase()),
				);
			});
		}

		// Apply sort
		const sortFn = getSortFunction(appliedFilters.sort);
		filtered.sort(sortFn);

		// Update status text
		const status = $("status");
		if (status) {
			status.textContent = `${filtered.length.toLocaleString()} hospitals reporting`;
		}

		// Hide all, then show filtered
		allHospitals.forEach((h) => {
			if (h.element) {
				h.element.style.display = "none";
			}
			if (h.mobileElement) h.mobileElement.style.display = "none";
		});

		// Re-append in sorted order
		const tbody = $("results");
		const mobileCont = $("results-mobile");

		if (tbody) {
			filtered.forEach((h) => {
				if (h.element && h.element.tagName === "TR") {
					h.element.style.display = "";
					tbody.appendChild(h.element);
				}
			});
		}

		if (mobileCont) {
			filtered.forEach((h) => {
				const card =
					h.mobileElement ?? (h.element?.tagName === "A" ? h.element : null);
				if (card) {
					card.style.display = "";
					mobileCont.appendChild(card);
				}
			});
		}
	}

	function getSortFunction(sortKey) {
		switch (sortKey) {
			case "median-first":
				return (a, b) => {
					// Quality first (normal before flagged)
					if (a.quality !== b.quality) {
						return a.quality === "normal" ? -1 : 1;
					}
					// Then by distance from median
					const distA =
						a.cash !== null ? Math.abs(a.cash - cashMedian) : Infinity;
					const distB =
						b.cash !== null ? Math.abs(b.cash - cashMedian) : Infinity;
					return distA - distB;
				};
			case "state":
				return (a, b) =>
					a.state.localeCompare(b.state) || a.name.localeCompare(b.name);
			case "cash-asc":
				return (a, b) => {
					if (a.cash === null) return 1;
					if (b.cash === null) return -1;
					return a.cash - b.cash;
				};
			case "cash-desc":
				return (a, b) => {
					if (a.cash === null) return 1;
					if (b.cash === null) return -1;
					return b.cash - a.cash;
				};
			case "gross-asc":
				return (a, b) => {
					if (a.gross === null) return 1;
					if (b.gross === null) return -1;
					return a.gross - b.gross;
				};
			case "payer-asc":
				// Sort by lowest payer rate first
				return (a, b) => {
					const aRate = getMinPayerRate(a);
					const bRate = getMinPayerRate(b);
					if (aRate === null) return 1;
					if (bRate === null) return -1;
					return aRate - bRate;
				};
			default:
				return (_a, _b) => 0;
		}
	}

	function getMinPayerRate(hospital) {
		if (!hospital.payers || hospital.payers.length === 0) return null;
		const rates = hospital.payers
			.map((p) => p.r || p.rate || p.rate_dollar)
			.filter((r) => r !== null && r !== undefined && typeof r === "number");
		if (rates.length === 0) return null;
		return Math.min(...rates);
	}

	// Initialize on load
	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", init);
	} else {
		init();
	}
})();
