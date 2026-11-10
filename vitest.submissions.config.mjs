import { defineConfig } from "vitest/config";

// The hospital file-submission API (src/routes/api/submissions.ts) and
// its pages. Its own config for the same reason as
// vitest.fetch-proxy.config.mjs: vite.config.ts is the Worker build,
// not a test setup. The multipart tests move one real 16 MiB part, so
// the timeout is sized for that, not for a hang.
export default defineConfig({
	test: {
		include: ["src/routes/api/submissions.test.ts"],
		testTimeout: 30000,
	},
});
