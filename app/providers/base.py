from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str


class ProviderError(Exception):
    """retryable=True for 429/5xx/timeouts (worth backing off); False for 4xx/auth."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(ABC):
    name: str
    model: str
    # USD per 1M tokens
    input_price: float = 0.0
    output_price: float = 0.0

    @abstractmethod
    async def complete(self, system: str, messages: list[dict[str, str]]) -> LLMResult:
        """messages: [{"role": "user"|"assistant", "content": str}, ...]"""

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.input_price + completion_tokens * self.output_price) / 1_000_000
