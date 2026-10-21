import { defineConfig } from "vitest/config";

// The "Ask anything" suite (src/faq/*.test.ts). Its own config — the repo's
// vite.config.ts is the Worker build (hono/vite-build + dev server), not a test
// setup — and always passed explicitly: a bare `npx vitest run` can pick up a stray
// ~/vite.config.ts. vite applies the JSX settings from tsconfig.json (jsx react-jsx,
// jsxImportSource hono/jsx) so the hono/jsx pages render under node.
export default defineConfig({
	test: {
		include: ["src/faq/*.test.ts"],
		testTimeout: 15000,
	},
});
