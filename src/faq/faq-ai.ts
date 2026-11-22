import type { FaqAi, FaqModel, FaqRunOutput } from "./faq-route";

/**
 * The "Ask anything" model backend. When an agentprofile token is configured
 * (the Worker secret AGENTPROFILE_TOKEN, a dedicated agent token with the
 * llm:invoke scope), questions are answered by gpt-6.2-astra through the
 * agentprofile LLM front door — OpenAI-compatible, and its streamed chunks are
 * the `choices[0].delta.content` shape the route's parser already accepts.
 * Without a token the Workers AI binding answers with llama, exactly as
 * before; and if the front door refuses a call (HTTP error, no body), that
 * call falls back to the binding rather than failing the visitor's question.
 */
export const FRONT_DOOR_MODEL: FaqModel = "gpt-6.2-astra";

export const FRONT_DOOR_URL =
	"https://profile.barkleesanders.com/llm/v1/chat/completions";

const FALLBACK_MODEL: FaqModel = "@cf/meta/llama-3.3-70b-instruct-fp8-fast";

export function agentProfileBackend(env: {
	AGENTPROFILE_TOKEN?: string;
	AI: FaqAi;
}): { ai: FaqAi; model: FaqModel } | undefined {
	const token = env.AGENTPROFILE_TOKEN;

	if (!token) return undefined;
	const fallback = env.AI;
	const ai: FaqAi = {
		async run(_model, input): Promise<FaqRunOutput> {
			try {
				const res = await fetch(FRONT_DOOR_URL, {
					method: "POST",
					headers: {
						authorization: `Bearer ${token}`,
						"content-type": "application/json",
					},
					body: JSON.stringify({
						model: FRONT_DOOR_MODEL,
						messages: input.messages,
						max_tokens: input.max_tokens,
						temperature: input.temperature,
						stream: true,
					}),
				});

				if (!res.ok || !res.body)
					throw new Error(
						`agentprofile front door: HTTP ${res.status}`,
					);

				return res.body;
			} catch (err) {
				console.error(
					JSON.stringify({
						msg: "faq.ai.fallback",
						error: err instanceof Error ? err.message : String(err),
					}),
				);

				return fallback.run(FALLBACK_MODEL, input);
			}
		},
	};

	return { ai, model: FRONT_DOOR_MODEL };
}
