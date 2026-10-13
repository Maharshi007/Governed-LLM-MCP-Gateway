import json

import httpx
import pytest

from app.main import app
from app.providers.base import ProviderError
from app.providers.ollama_provider import OllamaProvider
from mcp_server import server as mcp_server
from tests.conftest import token_for


@pytest.mark.asyncio
async def test_mcp_tools_registered():
    names = {t.name for t in await mcp_server.mcp.list_tools()}
    assert names == {"extract_structured_data", "list_schemas", "get_usage"}


@pytest.mark.asyncio
async def test_mcp_extract_calls_gateway(make_client, monkeypatch):
    c = await make_client()
    headers = await token_for(c, tenant="mcp-agent")

    def in_process_client():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)

    monkeypatch.setattr(mcp_server, "_client", in_process_client)
    result = await mcp_server.extract_structured_data("invoice", "Invoice INV-1001 from Acme Corp")
    assert result["data"]["total_amount"] == 125.5
    assert result["provider"] == "mock"

    usage = await mcp_server.get_usage()
    assert usage["tenant_id"] == "mcp-agent" and usage["tokens_used_today"] > 0


@pytest.mark.asyncio
async def test_mcp_surfaces_gateway_errors(make_client, monkeypatch):
    await make_client()
    monkeypatch.setattr(
        mcp_server, "_client",
        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"),
    )
    with pytest.raises(RuntimeError, match="401"):
        await mcp_server.list_schemas()


def _ollama_with(handler) -> OllamaProvider:
    p = OllamaProvider("http://ollama", "llama3.2:3b", timeout=5)
    p.client = httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler))
    return p


@pytest.mark.asyncio
async def test_ollama_parses_response_and_usage():
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["format"] == "json" and body["stream"] is False
        assert body["messages"][0]["role"] == "system"
        return httpx.Response(200, json={
            "model": "llama3.2:3b", "message": {"role": "assistant", "content": '{"a": 1}'},
            "prompt_eval_count": 42, "eval_count": 7,
        })

    result = await _ollama_with(handler).complete("sys", [{"role": "user", "content": "hi"}])
    assert result.text == '{"a": 1}'
    assert (result.prompt_tokens, result.completion_tokens) == (42, 7)


@pytest.mark.asyncio
async def test_ollama_missing_model_is_not_retryable():
    p = _ollama_with(lambda req: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ProviderError) as exc:
        await p.complete("sys", [{"role": "user", "content": "hi"}])
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_ollama_down_is_retryable():
    def handler(req):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ProviderError) as exc:
        await _ollama_with(handler).complete("sys", [{"role": "user", "content": "hi"}])
    assert exc.value.retryable is True
