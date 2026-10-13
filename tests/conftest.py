import os
import tempfile

# Configure before any app import (settings are cached).
_tmp = tempfile.mkdtemp()
os.environ.update({
    "REDIS_URL": "",
    "DATABASE_URL": f"sqlite+aiosqlite:///{_tmp}/test.db",
    "PROVIDER_CHAIN": "mock",
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "RETRY_BASE_SECONDS": "0.01",
    "RETRY_MAX_SECONDS": "0.02",
    "RATE_LIMIT_PER_MINUTE": "1000",
    "JWT_SECRET": "test-secret",
    "ADMIN_API_KEY": "test-admin",
})

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402

from app import db, store  # noqa: E402
from app.gateway import Gateway  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.mock_provider import MockProvider  # noqa: E402


@pytest_asyncio.fixture
async def make_client():
    """Factory: build a client around a gateway with the given providers."""
    await store.init_redis()
    await db.init_db()
    await store.get_redis().flushall()

    clients = []

    async def _make(providers=None):
        app.state.gateway = Gateway(providers or [MockProvider()])
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        return c

    yield _make
    for c in clients:
        await c.aclose()
    await db.close_db()


async def token_for(client, tenant="tenant-a", roles=("extract",), budget=None) -> dict:
    r = await client.post(
        "/v1/auth/token",
        json={"tenant_id": tenant, "roles": list(roles), "token_budget": budget},
        headers={"X-Admin-Key": "test-admin"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
