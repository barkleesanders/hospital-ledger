import { defineConfig } from "vitest/config";

// R2-first site data (src/lib/site-data.ts + the /data/*.json routes). Its own
// config for the same reason as vitest.faq.config.mjs: vite.config.ts is the
// Worker build, not a test setup.
export default defineConfig({
	test: {
		include: ["src/lib/site-data.test.ts", "src/lib/r2.test.ts", "tests/procedure-client.test.js", "tests/hospital-id-clients.test.js"],
		testTimeout: 15000,
	},
});
