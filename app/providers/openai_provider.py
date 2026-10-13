import openai

from app.providers.base import LLMProvider, LLMResult, ProviderError

# USD per 1M tokens (input, output). Update when vendor pricing changes.
OPENAI_PRICING = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout: float):
        # max_retries=0: retries/backoff are owned by the gateway, not the SDK.
        self.client = openai.AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=0)
        self.model = model
        self.input_price, self.output_price = OPENAI_PRICING.get(model, (0.0, 0.0))

    async def complete(self, system: str, messages: list[dict[str, str]]) -> LLMResult:
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, *messages],
                response_format={"type": "json_object"},
                temperature=0,
            )
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError) as e:
            raise ProviderError(f"openai transient: {e}", retryable=True) from e
        except openai.APIStatusError as e:
            raise ProviderError(f"openai {e.status_code}: {e.message}", retryable=e.status_code >= 500) from e

        usage = resp.usage
        return LLMResult(
            text=resp.choices[0].message.content or "",
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            model=resp.model,
        )
