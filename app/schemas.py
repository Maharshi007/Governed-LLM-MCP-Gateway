"""Extraction schemas (the deterministic contract) and API request/response models."""

import json
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    # strict=True: no silent coercion ("12.5" is NOT accepted for a float field)
    model_config = ConfigDict(strict=True, extra="forbid")


# ---------- Extraction schemas ----------

class LineItem(StrictModel):
    description: str
    quantity: float
    unit_price: float
    amount: float


class Invoice(StrictModel):
    invoice_number: str
    vendor_name: str
    invoice_date: date = Field(description="ISO-8601 date, YYYY-MM-DD")
    currency: str = Field(min_length=3, max_length=3, description="ISO-4217 code")
    line_items: list[LineItem]
    total_amount: float


class Sentiment(StrictModel):
    sentiment: Literal["positive", "negative", "neutral"]
    confidence: float = Field(ge=0, le=1)
    key_phrases: list[str]


class Contact(StrictModel):
    full_name: str
    email: str | None
    phone: str | None
    company: str | None


SCHEMA_REGISTRY: dict[str, type[StrictModel]] = {
    "invoice": Invoice,
    "sentiment": Sentiment,
    "contact": Contact,
}


def validate_payload(schema_cls: type[StrictModel], data: Any) -> StrictModel:
    # JSON has no date type, so validate in JSON mode (ISO strings -> date allowed)
    # while numbers/bools stay strict.
    return schema_cls.model_validate_json(json.dumps(data))


# ---------- API models ----------

class ExtractRequest(BaseModel):
    schema_name: str = Field(examples=["invoice"])
    text: str = Field(min_length=1, max_length=50_000)
    provider_hint: str | None = Field(default=None, description="Force a provider first")


class ExtractResponse(BaseModel):
    request_id: str
    schema_name: str
    data: dict[str, Any]
    provider: str
    model: str
    cache_hit: bool
    correction_rounds: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float


class TokenRequest(BaseModel):
    tenant_id: str
    roles: list[Literal["extract", "admin", "metrics"]] = ["extract"]
    token_budget: int | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
