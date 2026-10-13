import anthropic

from app.providers.base import LLMProvider, LLMResult, ProviderError

# USD per 1M tokens (input, output). Update when vendor pricing changes.
ANTHROPIC_PRICING = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float):
        # max_retries=0: retries/backoff are owned by the gateway, not the SDK.
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=0)
        self.model = model
        self.input_price, self.output_price = ANTHROPIC_PRICING.get(model, (0.0, 0.0))

    async def complete(self, system: str, messages: list[dict[str, str]]) -> LLMResult:
        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system,
                messages=messages,
                # Extraction is a simple task: keep thinking effort low for latency/cost.
                extra_body={"output_config": {"effort": "low"}},
            )
        except (anthropic.RateLimitError, anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
            raise ProviderError(f"anthropic transient: {e}", retryable=True) from e
        except anthropic.APIStatusError as e:
            raise ProviderError(
                f"anthropic {e.status_code}: {e.message}", retryable=e.status_code >= 500
            ) from e

        if resp.stop_reason == "refusal":
            raise ProviderError("anthropic refused the request", retryable=False)

        text = "".join(b.text for b in resp.content if b.type == "text")
        return LLMResult(
            text=text,
            prompt_tokens=resp.usage.input_tokens,
            completion_tokens=resp.usage.output_tokens,
            model=resp.model,
        )
