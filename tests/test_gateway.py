import pytest
import sqlalchemy

from app import db
from app.providers.mock_provider import MockProvider
from tests.conftest import token_for

INVOICE_TEXT = "Invoice INV-1001 from Acme Corp dated Jan 15 2026. 2 widgets @ $50, 1 gadget @ $25.50."


def extract_body(text=INVOICE_TEXT, schema="invoice", **kw):
    return {"schema_name": schema, "text": text, **kw}


@pytest.mark.asyncio
async def test_requires_auth(make_client):
    c = await make_client()
    r = await c.post("/v1/extract", json=extract_body())
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_rbac_rejects_missing_role(make_client):
    c = await make_client()
    h = await token_for(c, roles=("metrics",))
    r = await c.post("/v1/extract", json=extract_body(), headers=h)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_extract_then_cache_hit(make_client):
    c = await make_client()
    h = await token_for(c)

    r1 = await c.post("/v1/extract", json=extract_body(), headers=h)
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["data"]["total_amount"] == 125.5
    assert b1["data"]["invoice_date"] == "2026-01-15"
    assert r1.headers["X-Cache-Hit"] == "false"
    assert float(r1.headers["X-Token-Cost"]) > 0

    r2 = await c.post("/v1/extract", json=extract_body(), headers=h)
    assert r2.status_code == 200
    assert r2.headers["X-Cache-Hit"] == "true"
    assert r2.json()["cost_usd"] == 0.0
    assert r2.json()["data"] == b1["data"]


@pytest.mark.asyncio
async def test_cache_is_tenant_scoped(make_client):
    c = await make_client()
    await c.post("/v1/extract", json=extract_body(), headers=await token_for(c, tenant="a"))
    r = await c.post("/v1/extract", json=extract_body(), headers=await token_for(c, tenant="b"))
    assert r.headers["X-Cache-Hit"] == "false"


@pytest.mark.asyncio
@pytest.mark.parametrize("chaos", ["[[bad_types]]", "[[garbage]]"])
async def test_self_correction(make_client, chaos):
    c = await make_client()
    r = await c.post("/v1/extract", json=extract_body(text=f"{chaos} {INVOICE_TEXT}"), headers=await token_for(c))
    assert r.status_code == 200, r.text
    assert r.json()["correction_rounds"] == 1
    assert isinstance(r.json()["data"]["total_amount"], float)


@pytest.mark.asyncio
async def test_fallback_and_circuit_breaker(make_client):
    primary = MockProvider("primary", fail=True)
    secondary = MockProvider("secondary")
    c = await make_client([primary, secondary])
    h = await token_for(c)

    # Each request: primary retried 3x then fails over. Threshold=3 trips the breaker.
    for i in range(3):
        r = await c.post("/v1/extract", json=extract_body(text=f"{INVOICE_TEXT} #{i}"), headers=h)
        assert r.status_code == 200
        assert r.json()["provider"] == "secondary"
    calls_when_tripped = primary.calls

    r = await c.post("/v1/extract", json=extract_body(text=f"{INVOICE_TEXT} #after"), headers=h)
    assert r.json()["provider"] == "secondary"
    assert primary.calls == calls_when_tripped  # circuit open: primary not called at all

    ready = (await c.get("/ready")).json()
    assert ready["circuits"]["primary"] == "open"
    assert ready["ready"] is True  # secondary is still healthy


@pytest.mark.asyncio
async def test_all_providers_down_returns_503(make_client):
    c = await make_client([MockProvider("a", fail=True), MockProvider("b", fail=True)])
    r = await c.post("/v1/extract", json=extract_body(), headers=await token_for(c))
    assert r.status_code == 503
    assert set(r.json()["detail"]["providers"]) == {"a", "b"}


@pytest.mark.asyncio
async def test_idempotency_replay(make_client):
    provider = MockProvider()
    c = await make_client([provider])
    h = {**await token_for(c), "Idempotency-Key": "abc-123"}
    r1 = await c.post("/v1/extract", json=extract_body(), headers=h)
    r2 = await c.post("/v1/extract", json=extract_body(), headers=h)
    assert r2.headers.get("Idempotent-Replayed") == "true"
    assert r1.json()["request_id"] == r2.json()["request_id"]
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_budget_rejects_request_that_would_exceed_it(make_client):
    c = await make_client()
    h = await token_for(c, budget=100)  # smaller than one request's reservation
    r = await c.post("/v1/extract", json=extract_body(), headers=h)
    assert r.status_code == 402
    assert (await c.get("/v1/usage", headers=h)).json()["tokens_used_today"] == 0  # reservation refunded


@pytest.mark.asyncio
async def test_budget_reservation_reconciled_to_actual_usage(make_client):
    c = await make_client()
    h = await token_for(c, budget=10_000)
    body = (await c.post("/v1/extract", json=extract_body(), headers=h)).json()
    used = (await c.get("/v1/usage", headers=h)).json()["tokens_used_today"]
    assert used == body["prompt_tokens"] + body["completion_tokens"]


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_overshoot_budget(make_client):
    import asyncio
    from app import store

    class SlowProvider(MockProvider):
        async def complete(self, system, messages):
            await asyncio.sleep(0.2)  # keep all three requests in flight together
            return await super().complete(system, messages)

    c = await make_client([SlowProvider()])
    one = store.estimate_tokens(INVOICE_TEXT + " #0")
    h = await token_for(c, budget=int(one * 2.5))  # room for 2 reservations, not 3
    rs = await asyncio.gather(*[
        c.post("/v1/extract", json=extract_body(text=f"{INVOICE_TEXT} #{i}"), headers=h) for i in range(3)
    ])
    assert sorted(r.status_code for r in rs) == [200, 200, 402]


@pytest.mark.asyncio
async def test_failed_upstream_refunds_reservation(make_client):
    c = await make_client([MockProvider("down", fail=True)])
    h = await token_for(c)
    assert (await c.post("/v1/extract", json=extract_body(), headers=h)).status_code == 503
    assert (await c.get("/v1/usage", headers=h)).json()["tokens_used_today"] == 0


@pytest.mark.asyncio
async def test_rate_limit(make_client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 2)
    c = await make_client()
    h = await token_for(c)
    codes = [(await c.post("/v1/extract", json=extract_body(), headers=h)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


@pytest.mark.asyncio
async def test_unknown_schema(make_client):
    c = await make_client()
    r = await c.post("/v1/extract", json=extract_body(schema="nope"), headers=await token_for(c))
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_audit_log_written_and_immutable(make_client):
    c = await make_client()
    h = await token_for(c, tenant="auditor")
    await c.post("/v1/extract", json=extract_body(), headers=h)
    rows = (await c.get("/v1/audit", headers=h)).json()
    assert rows and rows[0]["status"] == "success" and rows[0]["tenant_id"] == "auditor"

    async with db.get_sessionmaker()() as s:
        with pytest.raises(sqlalchemy.exc.DBAPIError, match="append-only"):
            await s.execute(sqlalchemy.text("UPDATE audit_log SET cost_usd = 0"))
            await s.commit()


@pytest.mark.asyncio
async def test_metrics_exposed(make_client):
    c = await make_client()
    await c.post("/v1/extract", json=extract_body(), headers=await token_for(c))
    text = (await c.get("/metrics")).text
    assert "structurai_requests_total" in text
    assert "structurai_cost_usd_total" in text
