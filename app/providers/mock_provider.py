"""Deterministic offline provider for demos and tests.

Chaos controls (embed in the request text):
  [[fail]]        -> always raise a retryable error (simulates an outage / 503)
  [[bad_types]]   -> first response has wrong types; corrected after feedback
  [[garbage]]     -> first response is not JSON; corrected after feedback
"""

import json
import re

from app.providers.base import LLMProvider, LLMResult, ProviderError

_SAMPLES = {
    "invoice": {
        "invoice_number": "INV-1001",
        "vendor_name": "Acme Corp",
        "invoice_date": "2026-01-15",
        "currency": "USD",
        "line_items": [
            {"description": "Widget", "quantity": 2.0, "unit_price": 50.0, "amount": 100.0},
            {"description": "Gadget", "quantity": 1.0, "unit_price": 25.5, "amount": 25.5},
        ],
        "total_amount": 125.5,
    },
    "sentiment": {"sentiment": "positive", "confidence": 0.92, "key_phrases": ["great service"]},
    "contact": {"full_name": "Jane Doe", "email": "jane@example.com", "phone": None, "company": "Acme"},
}


class MockProvider(LLMProvider):
    def __init__(self, name: str = "mock", fail: bool = False):
        self.name = name
        self.model = f"{name}-model"
        self.fail = fail
        self.input_price, self.output_price = 0.10, 0.50
        self.calls = 0

    async def complete(self, system: str, messages: list[dict[str, str]]) -> LLMResult:
        self.calls += 1
        first_user = messages[0]["content"]
        if self.fail or "[[fail]]" in first_user:
            raise ProviderError(f"{self.name}: simulated 503", retryable=True)

        schema = re.search(r"SCHEMA_NAME: (\w+)", system)
        data = dict(_SAMPLES.get(schema.group(1) if schema else "", {}))
        is_correction = len(messages) > 1

        if "[[garbage]]" in first_user and not is_correction:
            text = "Sure! Here is the invoice data you asked for: total is 125.5"
        elif "[[bad_types]]" in first_user and not is_correction:
            data = {**data, "total_amount": "125.50 USD", "invoice_date": "15th Jan"}
            text = json.dumps(data)
        else:
            text = json.dumps(data)

        prompt_tokens = (len(system) + sum(len(m["content"]) for m in messages)) // 4
        return LLMResult(text=text, prompt_tokens=prompt_tokens, completion_tokens=len(text) // 4, model=self.model)
