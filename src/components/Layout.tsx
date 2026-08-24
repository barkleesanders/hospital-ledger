/**
 * Shared <html><head><body> shell. Wraps every SSR page with the same
 * fonts, Tailwind CDN, security/perf meta, and footer.
 *
 * Tailwind is loaded via CDN (matches the legacy site/index.html). PostCSS
 * pipeline is intentionally out of scope this round.
 */

import type { FC, PropsWithChildren } from "hono/jsx";

export type LayoutProps = PropsWithChildren<{
	title: string;
	description: string;
	// Full canonical URL for this page (e.g. "https://hospitalledger.com/procedure/27130").
	// Optional for backwards compat — pages that omit it skip the canonical/og:url tags.
	url?: string;
	// Override the default OG image (defaults to https://hospitalledger.com/og.png).
	ogImage?: string;
	ogTitle?: string;
	bodyClass?: string;
	// Optional inline `<script>` body to ship below the rendered content.
	// Keep these tiny — heavy logic should live in /public/*.js.
	inlineScript?: string;
	// Optional <script src="..."> to defer below the body. Pass an array
	// when multiple ordered scripts are needed (e.g. cpt-names.js then
	// home-client.js, where the latter reads window.CPT_NAMES).
	scriptSrc?: string | string[];
}>;

const DEFAULT_OG_IMAGE = "https://hospitalledger.com/og.png";

export const Layout: FC<LayoutProps> = ({
	title,
	description,
	url,
	ogImage,
	ogTitle,
	bodyClass,
	inlineScript,
	scriptSrc,
	children,
}) => {
	const resolvedOgImage = ogImage ?? DEFAULT_OG_IMAGE;
	const resolvedOgTitle = ogTitle ?? title;
	return (
		<html lang="en" class="bg-zinc-950 text-zinc-100">
			<head>
				<meta charset="utf-8" />
				<meta name="viewport" content="width=device-width,initial-scale=1" />
				<title>{title}</title>
				<meta name="description" content={description} />
				{url ? <link rel="canonical" href={url} /> : null}

				{/* Open Graph */}
				<meta property="og:title" content={resolvedOgTitle} />
				<meta property="og:description" content={description} />
				<meta property="og:type" content="website" />
				<meta property="og:site_name" content="Hospital Ledger" />
				{url ? <meta property="og:url" content={url} /> : null}
				<meta property="og:image" content={resolvedOgImage} />
				<meta property="og:image:width" content="1200" />
				<meta property="og:image:height" content="630" />
				<meta
					property="og:image:alt"
					content="Hospital Ledger — what every U.S. hospital actually charges"
				/>

				{/* Twitter / X */}
				<meta name="twitter:card" content="summary_large_image" />
				<meta name="twitter:title" content={resolvedOgTitle} />
				<meta name="twitter:description" content={description} />
				<meta name="twitter:image" content={resolvedOgImage} />

				<link rel="icon" type="image/svg+xml" href="/favicon.svg" />
				<link rel="alternate icon" href="/favicon.ico" />
				<link rel="apple-touch-icon" href="/apple-touch-icon.png" />
				<link rel="preconnect" href="https://fonts.googleapis.com" />
				<link
					rel="preconnect"
					href="https://fonts.gstatic.com"
					crossorigin="anonymous"
				/>
				<link
					href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500&display=swap"
					rel="stylesheet"
				/>
				<script src="https://cdn.tailwindcss.com" />
				<style
					dangerouslySetInnerHTML={{
						__html: `
  body { font-family: 'Inter', ui-sans-serif, -apple-system, system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
  .serif { font-family: 'Instrument Serif', Georgia, ui-serif, serif; font-weight: 400; letter-spacing: -0.01em; }
  .label-eyebrow { font-family: 'Inter', sans-serif; font-size: 10px; letter-spacing: 0.15em; text-transform: uppercase; color: #a1a1aa; }
  .mono, .tab-num { font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace; font-variant-numeric: tabular-nums; }
  .num-step { font-family: 'JetBrains Mono', ui-monospace, monospace; font-weight: 500; color: #34d399; }
  .editorial-rule { border-top: 1px solid #27272a; }
  .fade-in { animation: fade .4s ease-out; }
  @keyframes fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
  details[open] summary svg { transform: rotate(180deg); }
  summary { list-style: none; }
  summary::-webkit-details-marker { display: none; }
  .lift { transition: transform .15s ease, border-color .15s ease, background .15s ease; }
  .lift:hover { transform: translateY(-2px); border-color: rgba(52,211,153,0.35); background: rgba(24,24,27,0.85); }
  input:focus, select:focus, button:focus-visible { outline: 2px solid #34d399; outline-offset: 1px; }
  .section-rule { display: flex; align-items: baseline; gap: 1rem; margin-bottom: 1.5rem; }
  .section-rule .num { font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; color: #71717a; letter-spacing: 0.1em; }
  .section-rule .label { font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.15em; color: #a1a1aa; }
  .section-rule .line { flex: 1; height: 1px; background: #27272a; }
  .carousel-track::-webkit-scrollbar { display: none; }
  .carousel-track { scrollbar-width: none; -ms-overflow-style: none; }
  @media print { body { background: white; color: black; } .no-print { display: none; } }
        `,
					}}
				/>

				{/* Traks Analytics (self-hosted, cookieless) */}
				<script defer src="https://traks-collect.barkleesanders.workers.dev/t.js" data-site="pb_live_yx6qihmu9eceff3n9wcubpmr" />
			</head>
			<body class={bodyClass ?? "min-h-screen"}>
				{children}
				<footer class="border-t border-zinc-800 mt-10 sm:mt-12 py-6 text-center text-xs text-zinc-500">
					<a href="/" class="hover:text-zinc-300">
						Hospital Ledger
					</a>{" "}
					· © 2026 · CC0 data · No tracking
				</footer>
				{Array.isArray(scriptSrc) ? (
					scriptSrc.map((src) => <script src={src} defer />)
				) : scriptSrc ? (
					<script src={scriptSrc} defer />
				) : null}
				{inlineScript ? (
					<script dangerouslySetInnerHTML={{ __html: inlineScript }} />
				) : null}
			</body>
		</html>
	);
};

export const PageHeader: FC<{ eyebrow?: string }> = ({ eyebrow }) => (
	<header class="border-b border-zinc-800">
		<div class="mx-auto max-w-6xl px-4 sm:px-6 pt-6 sm:pt-8 pb-4">
			<div class="flex items-center justify-between">
				<a href="/" class="text-sm text-zinc-400 hover:text-zinc-200">
					← Hospital Ledger
				</a>
				{eyebrow ? (
					<div class="label-eyebrow text-emerald-300">{eyebrow}</div>
				) : null}
			</div>
		</div>
	</header>
);
