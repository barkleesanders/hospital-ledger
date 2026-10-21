/**
 * Which URLs the "Ask anything" island turns into clickable links.
 *
 * The prompt carries text the site did not write — hospital names, plan-name
 * aliases and procedure descriptions come straight out of each hospital's
 * machine-readable file — so a crafted description could steer an answer toward
 * an attacker's URL, and every later visitor asking about that record would get
 * it as a link. Only this site's own https URLs become links; any other URL the
 * model emits is still shown, as plain text. (Same allowlist shape as
 * improvecortland/src/faq_links.ts and improvebayarea/src/faq_links.ts.)
 */
export const FAQ_LINK_HOST = "hospitalledger.com";

export function isSiteLink(href: string): boolean {
	let host: string;

	try {
		const url = new URL(href);

		if (url.protocol !== "https:") return false;
		host = url.hostname.toLowerCase();
	} catch {
		return false;
	}

	return host === FAQ_LINK_HOST || host.endsWith(`.${FAQ_LINK_HOST}`);
}
