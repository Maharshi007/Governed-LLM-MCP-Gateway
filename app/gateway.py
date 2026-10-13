"""Core pipeline: provider routing -> retry/backoff -> circuit breaking -> fallback
-> schema validation -> self-correction."""

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.circuit_breaker import CircuitBreaker
from app.config import get_settings
from app.logging_conf import logger
from app.metrics import FALLBACKS, PROVIDER_CALLS, VALIDATION_RETRIES
from app.providers.base import LLMProvider, LLMResult, ProviderError
from app.schemas import StrictModel, validate_payload


class AllProvidersFailed(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__(f"all providers failed: {errors}")
        self.errors = errors


class ExtractionFailed(Exception):
    """Provider answered, but never produced schema-valid output."""

    def __init__(self, message: str, usage: "Usage"):
        super().__init__(message)
        self.usage = usage


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, provider: LLMProvider, result: LLMResult) -> None:
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        self.cost_usd += provider.cost(result.prompt_tokens, result.completion_tokens)


@dataclass
class ExtractionResult:
    data: dict[str, Any]
    provider: str
    model: str
    correction_rounds: int
    usage: Usage = field(default_factory=Usage)


def build_system_prompt(schema_name: str, schema_cls: type[StrictModel]) -> str:
    return (
        "You are a data extraction engine. Extract information from the user's text and "
        "respond with ONLY a single JSON object that conforms exactly to this JSON Schema. "
        "No prose, no markdown fences. Use null for optional fields that are absent. "
        "Numbers must be JSON numbers, dates must be ISO-8601 (YYYY-MM-DD).\n"
        f"SCHEMA_NAME: {schema_name}\n"
        f"JSON_SCHEMA: {json.dumps(schema_cls.model_json_schema())}"
    )


def _parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):  # tolerate markdown fences
        text = text.strip("`")
        text = text[text.find("\n") + 1 :] if "\n" in text else text
    return json.loads(text)


class Gateway:
    def __init__(self, providers: list[LLMProvider]):
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = providers
        self.settings = get_settings()

    def _ordered(self, hint: str | None) -> list[LLMProvider]:
        if not hint:
            return self.providers
        return sorted(self.providers, key=lambda p: p.name != hint)

    async def _call_with_retry(self, provider: LLMProvider, system: str, messages: list[dict]) -> LLMResult:
        s = self.settings
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(s.retry_attempts),
            wait=wait_exponential_jitter(initial=s.retry_base_seconds, max=s.retry_max_seconds),
            retry=retry_if_exception(lambda e: isinstance(e, ProviderError) and e.retryable),
            reraise=True,
        ):
            with attempt:
                n = attempt.retry_state.attempt_number
                if n > 1:
                    logger.warning("retrying provider", extra={"provider": provider.name, "attempt": n})
                return await provider.complete(system, messages)
        raise AssertionError("unreachable")

    async def _complete(
        self, system: str, messages: list[dict], hint: str | None, errors: dict[str, str]
    ) -> tuple[LLMProvider, LLMResult]:
        """Try providers in order, skipping ones whose circuit is open."""
        for idx, provider in enumerate(self._ordered(hint)):
            breaker = CircuitBreaker(provider.name)
            if not await breaker.allow_request():
                errors[provider.name] = "circuit open"
                PROVIDER_CALLS.labels(provider.name, "short_circuited").inc()
                continue
            try:
                result = await self._call_with_retry(provider, system, messages)
            except ProviderError as e:
                await breaker.record_failure()
                errors[provider.name] = str(e)
                PROVIDER_CALLS.labels(provider.name, "error").inc()
                logger.warning("provider failed, falling back", extra={"provider": provider.name, "error": str(e)})
                continue
            await breaker.record_success()
            PROVIDER_CALLS.labels(provider.name, "success").inc()
            if idx > 0:
                FALLBACKS.labels(provider.name).inc()
            return provider, result
        raise AllProvidersFailed(errors)

    async def extract(
        self, schema_name: str, schema_cls: type[StrictModel], text: str, hint: str | None = None
    ) -> ExtractionResult:
        system = build_system_prompt(schema_name, schema_cls)
        messages: list[dict[str, str]] = [{"role": "user", "content": text}]
        usage = Usage()
        errors: dict[str, str] = {}
        last_error = ""

        for round_ in range(self.settings.max_correction_attempts + 1):
            provider, result = await self._complete(system, messages, hint, errors)
            usage.add(provider, result)
            try:
                validated = validate_payload(schema_cls, _parse_json(result.text))
                return ExtractionResult(
                    data=validated.model_dump(mode="json"),
                    provider=provider.name,
                    model=result.model,
                    correction_rounds=round_,
                    usage=usage,
                )
            except (json.JSONDecodeError, ValidationError) as e:
                last_error = str(e)
                VALIDATION_RETRIES.labels(schema_name).inc()
                logger.info("schema validation failed; requesting self-correction",
                            extra={"provider": provider.name, "round": round_, "error": last_error[:500]})
                # Feed the exact validation error back so the model can fix its output.
                messages += [
                    {"role": "assistant", "content": result.text},
                    {"role": "user", "content": (
                        "Your previous response failed validation with these errors:\n"
                        f"{last_error}\n"
                        "Return ONLY the corrected JSON object that satisfies the schema."
                    )},
                ]
                # Stick with the provider that has the conversation context.
                hint = provider.name

        raise ExtractionFailed(f"output failed schema validation after retries: {last_error}", usage)
