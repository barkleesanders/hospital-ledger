/**
 * Shared SEO building blocks for the SSR templates: page-title fitting,
 * breadcrumb trails, and JSON-LD node builders.
 *
 * Audit basis (2026-11-20 SEO completeness sweep, items #12/#15/#17/#30):
 * title templates ran 65–95 chars against the 50–60 target, no page emitted
 * breadcrumbs or BreadcrumbList schema, payer/index/about/submit pages had
 * no JSON-LD at all, and only the apex Dataset node carried dateModified.
 * These helpers keep the fixes in one place so templates cannot drift
 * apart again.
 */

export const SITE_ORIGIN = "https://hospitalledger.com";

/** Titles past ~60 chars are truncated in search results; fit inside it. */
export const TITLE_MAX = 60;

/** Truncate at a word boundary with an ellipsis, to at most `max` chars. */
export function truncateWords(text: string, max: number): string {
	if (text.length <= max) return text;
	const cut = text.slice(0, max - 1);
	const lastSpace = cut.lastIndexOf(" ");
	return `${(lastSpace > 0 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`;
}

/** Fit a complete title inside TITLE_MAX, truncating the lead if needed. */
export function fitTitle(title: string, max: number = TITLE_MAX): string {
	return truncateWords(title, max);
}

/**
 * Fit `lead + suffix` inside TITLE_MAX by truncating the lead only, so a
 * meaningful suffix (e.g. a procedure's ` (45378)` code) always survives.
 */
export function fitTitleWithSuffix(
	lead: string,
	suffix: string,
	max: number = TITLE_MAX,
): string {
	return `${truncateWords(lead, max - suffix.length)}${suffix}`;
}

/** One breadcrumb step: visible label + site-relative path. */
export type Crumb = { name: string; path: string };

/** schema.org BreadcrumbList node for a crumb trail (last = current page). */
export function breadcrumbListNode(crumbs: Crumb[]) {
	return {
		"@type": "BreadcrumbList",
		itemListElement: crumbs.map((c, i) => ({
			"@type": "ListItem",
			position: i + 1,
			name: c.name,
			item: `${SITE_ORIGIN}${c.path}`,
		})),
	};
}

/** schema.org WebPage node; dateModified is the pipeline refresh stamp. */
export function webPageNode(opts: {
	name: string;
	url: string;
	description?: string;
	dateModified?: string;
}) {
	return {
		"@type": "WebPage",
		name: opts.name,
		url: opts.url,
		...(opts.description ? { description: opts.description } : {}),
		...(opts.dateModified ? { dateModified: opts.dateModified } : {}),
	};
}

/**
 * schema.org ItemList node. `items` may be a capped sample of the list the
 * page renders; `numberOfItems` always states the full count.
 */
export function itemListNode(opts: {
	name: string;
	items: { name: string; url: string }[];
	numberOfItems?: number;
}) {
	return {
		"@type": "ItemList",
		name: opts.name,
		numberOfItems: opts.numberOfItems ?? opts.items.length,
		itemListElement: opts.items.map((it, i) => ({
			"@type": "ListItem",
			position: i + 1,
			name: it.name,
			url: it.url,
		})),
	};
}

/**
 * Combine nodes into one @graph JSON-LD block. Child nodes carry no
 * @context of their own; the graph root carries it once. The Layout
 * serializes whatever it is given, so a page passes this as its jsonLd.
 */
export function graphJsonLd(...nodes: Record<string, unknown>[]) {
	return { "@context": "https://schema.org", "@graph": nodes };
}
