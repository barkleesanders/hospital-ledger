// Bundles src/faq/faq-island.tsx → public/faq-island.js (the "Ask anything" island).
//
// Run: npm run build:faq-island   (also part of `npm run predeploy`, LAST, so the
// committed bundle is the one the gates saw).
// The output is COMMITTED: a deploy serves whatever public/ holds, so the build
// must have run before the commit. src/faq/faq-section.test.ts fails when the
// committed bundle differs from a fresh build of the source.
import { build } from "esbuild";

await build({
	entryPoints: ["src/faq/faq-island.tsx"],
	bundle: true,
	format: "esm",
	minify: true,
	target: "es2022",
	jsx: "automatic",
	jsxImportSource: "hono/jsx/dom",
	outfile: "public/faq-island.js",
	logLevel: "info",
});
