"""Locally hosted model via Ollama (no API key, no per-token cost)."""

import httpx

from app.providers.base import LLMProvider, LLMResult, ProviderError


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: float):
        self.client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self.model = model
        self.input_price = self.output_price = 0.0  # self-hosted

    async def complete(self, system: str, messages: list[dict[str, str]]) -> LLMResult:
        try:
            resp = await self.client.post("/api/chat", json={
                "model": self.model,
                "messages": [{"role": "system", "content": system}, *messages],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0},
            })
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise ProviderError(f"ollama unreachable: {e!r}", retryable=True) from e

        if resp.status_code != 200:
            # 404 = model not pulled: retrying won't help.
            raise ProviderError(f"ollama {resp.status_code}: {resp.text[:200]}",
                                retryable=resp.status_code >= 500)

        body = resp.json()
        return LLMResult(
            text=body.get("message", {}).get("content", ""),
            prompt_tokens=body.get("prompt_eval_count", 0),
            completion_tokens=body.get("eval_count", 0),
            model=body.get("model", self.model),
        )
