import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import { afterEach, describe, expect, it } from "vitest";

const source = readFileSync(
	new URL("../public/procedure-client.js", import.meta.url),
	"utf8",
);
const windows = [];
const records = [
	{ id: "111111", state: "CA", cash: 8000, payer: "aetna" },
	{ id: "222222", state: "NY", cash: 9000, payer: "bcbs" },
	{ id: "333333", state: "CA", cash: 7000, payer: "aetna" },
];

async function page(query = "", data = records) {
	const rows = data
		.map(
			(r) => `<tr data-payers='[{"slug":"${r.payer}","rate":${r.cash}}]'>
<td><a href="/hospital/${r.id}">Fixture ${r.id}</a></td><td>${r.state}</td>
<td>$10000</td><td>$${r.cash}</td><td>—</td><td>—</td><td>1 insurer</td></tr>`,
		)
		.join("");
	const cards = data
		.map(
			(r) => `<a href="/hospital/${r.id}">
<div class="font-medium">Fixture ${r.id}</div><div class="text-xs text-zinc-500">${r.state}</div>
<div class="text-right tab-num text-emerald-300">$${r.cash}</div></a>`,
		)
		.join("");
	const dom = new JSDOM(
		`<!doctype html><section data-states='["CA","NY"]' data-cash-median="8000">
<select id="filter-state"><option value="">All states</option><option>CA</option><option>NY</option></select>
<select id="filter-payer"><option value="">All insurers</option><option>aetna</option><option>bcbs</option></select>
<select id="sort-by"><option value="median-first">Median</option><option value="cash-asc">Cash ascending</option></select>
</section><div id="status"></div><table><tbody id="results">${rows}</tbody></table>
<div id="results-mobile">${cards}</div>`,
		{
			url: `https://hospitalledger.com/procedure/27130${query}`,
			runScripts: "outside-only",
		},
	);
	windows.push(dom.window);
	await new Promise((resolve) =>
		dom.window.document.addEventListener("DOMContentLoaded", resolve, {
			once: true,
		}),
	);
	dom.window.eval(source);
	return dom.window;
}

function ids(window, container) {
	return [
		...window.document.querySelectorAll(
			container === "desktop" ? "#results tr" : "#results-mobile > a",
		),
	]
		.filter((el) => el.style.display !== "none")
		.map((el) =>
			(el.tagName === "TR" ? el.querySelector("a") : el)
				.getAttribute("href")
				.split("/")
				.at(-1),
		);
}

afterEach(() => {
	for (const window of windows.splice(0)) window.close();
});

describe.each(["desktop", "mobile"])(
	"procedure URL state in %s results",
	(container) => {
		it("applies state and sort immediately on a fresh shared URL", async () => {
			const window = await page("?state=CA&sort=cash-asc");
			expect(window.document.getElementById("filter-state").value).toBe("CA");
			expect(ids(window, container)).toEqual(["333333", "111111"]);
			expect(window.document.getElementById("status").textContent).toBe(
				"2 hospitals reporting",
			);
		});

		it("applies the insurance filter from a shared URL", async () => {
			const window = await page("?payer=bcbs");
			expect(ids(window, container)).toEqual(["222222"]);
		});

		it("keeps both records when hospital identifiers repeat", async () => {
			const window = await page("?state=CA&sort=cash-asc", [
				records[0],
				records[1],
				{ ...records[0], cash: 6000 },
			]);
			expect(ids(window, container)).toEqual(["111111", "111111"]);
			const nodes = [
				...window.document.querySelectorAll(
					container === "desktop" ? "#results tr" : "#results-mobile > a",
				),
			].filter((el) => el.style.display !== "none");
			expect(
				nodes.map(
					(el) =>
						(container === "desktop"
							? el.children[3]
							: el.querySelector(".tab-num")
						).textContent,
				),
			).toEqual(["$6000", "$8000"]);
		});

		it("stages controls until Update search, then restores clear/back/forward", async () => {
			const window = await page("?campaign=control#prices");
			const state = window.document.getElementById("filter-state");
			state.value = "CA";
			state.dispatchEvent(new window.Event("change"));
			expect(ids(window, container)).toHaveLength(3);
			window.document.getElementById("apply-filters-btn").click();
			expect(ids(window, container)).toEqual(["111111", "333333"]);
			expect(window.location.search).toBe("?campaign=control&state=CA");
			expect(window.location.hash).toBe("#prices");
			window.document.getElementById("clear-filters-btn").click();
			expect(ids(window, container)).toHaveLength(3);
			expect(window.location.search).toBe("?campaign=control");
			const back = new Promise((resolve) =>
				window.addEventListener("popstate", resolve, { once: true }),
			);
			window.history.back();
			await back;
			expect(state.value).toBe("CA");
			expect(ids(window, container)).toEqual(["111111", "333333"]);
			const forward = new Promise((resolve) =>
				window.addEventListener("popstate", resolve, { once: true }),
			);
			window.history.forward();
			await forward;
			expect(state.value).toBe("");
			expect(ids(window, container)).toHaveLength(3);
		});
	},
);
