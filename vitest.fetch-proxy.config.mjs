import { defineConfig } from "vitest/config";

// The /api/fetch edge-fetch route (src/routes/api/fetch-proxy.ts). Its own
// config for the same reason as vitest.faq.config.mjs: vite.config.ts is the
// Worker build, not a test setup.
export default defineConfig({
	test: {
		include: ["src/routes/api/fetch-proxy.test.ts"],
		testTimeout: 15000,
	},
});
