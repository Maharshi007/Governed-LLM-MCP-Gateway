"""MCP server: exposes the StructurAI gateway as tools for AI agents
(Claude Desktop, Claude Code, any MCP client).

Agents get schema-validated extraction with all of the gateway's guarantees
(auth, budgets, caching, failover, audit) instead of calling an LLM directly.

Env:
  STRUCTURAI_URL    gateway base URL (default http://localhost:8000)
  STRUCTURAI_TOKEN  tenant JWT from POST /v1/auth/token
"""

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

GATEWAY_URL = os.environ.get("STRUCTURAI_URL", "http://localhost:8000")
TOKEN = os.environ.get("STRUCTURAI_TOKEN", "")

mcp = FastMCP("structurai")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=GATEWAY_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=180,
    )


async def _request(method: str, path: str, **kwargs) -> Any:
    async with _client() as client:
        resp = await client.request(method, path, **kwargs)
    if resp.status_code >= 400:
        # Surface gateway errors (budget, rate limit, outage) to the agent verbatim.
        raise RuntimeError(f"StructurAI {resp.status_code}: {resp.text}")
    return resp


@mcp.tool()
async def extract_structured_data(schema_name: str, text: str) -> dict:
    """Extract schema-validated JSON from unstructured text via the StructurAI gateway.

    Args:
        schema_name: one of the schemas returned by list_schemas (e.g. "invoice", "sentiment", "contact").
        text: the raw unstructured text to extract from.

    Returns the validated data plus telemetry: provider used, cache hit, tokens, cost (USD), latency.
    """
    resp = await _request("POST", "/v1/extract", json={"schema_name": schema_name, "text": text})
    return resp.json()


@mcp.tool()
async def list_schemas() -> dict:
    """List the extraction schemas the gateway supports, with their JSON Schemas."""
    return (await _request("GET", "/v1/schemas")).json()


@mcp.tool()
async def get_usage() -> dict:
    """Show this tenant's token usage today versus its daily budget."""
    return (await _request("GET", "/v1/usage")).json()


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
