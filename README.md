# StructurAI

An AI gateway that sits between client apps and LLM providers. It turns unstructured text into **schema-validated JSON**, with caching, multi-provider failover, per-tenant budgets and a full audit trail.

```
client ──► FastAPI ──► JWT + RBAC ──► rate limit / token budget ──► idempotency + cache (Redis)
                                                                          │ miss
                                                                          ▼
                     ┌────────── provider chain (in priority order) ───────────┐
                     │  circuit breaker ─► Tenacity retry (exp. backoff+jitter) │
                     │  openai ──fail──► anthropic ──fail──► ollama (local)     │
                     └──────────────────────────────────────────────────────────┘
                                                                          │
                                                                          ▼
                     Pydantic strict validation ──fail──► send error back to LLM (self-correct, ≤2 rounds)
                                                                          │ ok
                                                                          ▼
                  audit log (Postgres, append-only trigger) + Prometheus metrics + X-* headers ──► client
```

## Run locally (no Docker)

```powershell
.\.venv\Scripts\Activate.ps1
copy .env.example .env          # add OPENAI_API_KEY / ANTHROPIC_API_KEY if you have them
uvicorn app.main:app --reload
```
With `REDIS_URL` empty, Redis is replaced by in-process `fakeredis`, and the audit log goes to SQLite. Docs are at http://localhost:8000/docs.

## Local LLM with Ollama (free, no API key)

```powershell
winget install Ollama.Ollama
ollama pull llama3.2:3b      # ~2 GB; fits a 4 GB GPU
```
`.env` already points `OLLAMA_BASE_URL` at `http://localhost:11434`. Ollama costs $0 per token, and requests run on your own machine.

## Run with Docker (Redis + Postgres + Prometheus)

```bash
docker compose up --build
```
API: `:8000`, Prometheus: `:9090`.

## Try it

```bash
# 1. Mint a tenant token (admin-only)
curl -X POST localhost:8000/v1/auth/token -H "X-Admin-Key: dev-admin-key" \
  -H "Content-Type: application/json" -d '{"tenant_id":"acme","roles":["extract"]}'

# 2. Extract
curl -i -X POST localhost:8000/v1/extract -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" -H "Idempotency-Key: order-42" \
  -d '{"schema_name":"invoice","text":"Invoice INV-1001 from Acme Corp ..."}'
```
Response headers: `X-Cache-Hit`, `X-Token-Cost`, `X-Provider`, `X-Prompt-Tokens`, `X-Completion-Tokens`, `X-Correction-Rounds`, `X-RateLimit-Remaining`, `X-Request-ID`.

**Chaos testing with the mock provider.** Put one of these markers in `text`:
- `[[fail]]`: the provider returns a simulated 503, which triggers retries, then fallback, then the circuit breaker.
- `[[bad_types]]`: the first answer has wrong types (`"125.50 USD"`), which triggers self-correction.
- `[[garbage]]`: the first answer is prose instead of JSON, which also triggers self-correction.

## MCP server (use the gateway from AI agents)

`mcp_server/server.py` exposes three tools: `extract_structured_data`, `list_schemas` and `get_usage`. Any MCP client (Claude Desktop, Claude Code) can then extract data through the gateway, with budgets, caching, failover and audit applied.

```bash
# Claude Code
claude mcp add structurai -e STRUCTURAI_URL=http://localhost:8000 -e STRUCTURAI_TOKEN=<tenant-jwt>   -- <path-to>/.venv/Scripts/python.exe <path-to>/mcp_server/server.py
```
Claude Desktop uses the same command, args and env in `claude_desktop_config.json` under `mcpServers.structurai`.

## CI/CD and deployment

- **GitHub Actions** (`.github/workflows/ci.yml`) runs on every push and PR in two jobs:
  1. The unit and integration test suite.
  2. A `docker compose` run against real Redis and Postgres. It smoke-tests extraction, the cache, `/ready` and `/metrics`, and checks that Postgres rejects `DELETE` on the audit log.
- **Render** (`render.yaml`): New → Blueprint → select this repo. It creates the web service and a Render Key Value (Redis) instance. Fill in `DATABASE_URL` and any provider keys when prompted.
- **Supabase** hosts the audit log. Use the *session pooler* connection string (port 5432) in the form `postgresql+asyncpg://postgres.<ref>:<password>@<host>:5432/postgres?ssl=require`. The append-only trigger is created automatically on startup.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/auth/token` | Issue tenant JWT (requires `X-Admin-Key`) |
| POST | `/v1/extract` | Structured extraction |
| GET | `/v1/schemas` | Registered schemas (JSON Schema) |
| GET | `/v1/usage` | Tenant's token usage vs. daily budget |
| GET | `/v1/audit` | Tenant's audit records |
| GET | `/health`, `/ready` | Liveness; readiness (Redis + circuit states) |
| GET | `/metrics` | Prometheus |

## Tests

```bash
pytest -q
```
There are 20 tests. They cover auth/RBAC, cache hit/miss, tenant cache isolation, self-correction, fallback, circuit-breaker tripping, total outage (503), idempotent replay, token budget (402), rate limit (429), audit immutability, metrics, the MCP tools and the Ollama provider.

## Project tracking

The backlog is in Jira: epics and stories are in `docs/jira/structurai_backlog.csv`, with import steps in `docs/jira/README.md`.

## Design decisions

- **Retries live in the gateway, not the SDK.** Both SDKs are set to `max_retries=0`, so retry and backoff policy has one owner and is visible in metrics. Only transient errors (429, 5xx, timeouts) are retried. 4xx errors fail over immediately.
- **Circuit state is kept in Redis.** Every replica sees the same breaker state. In half-open, a single probe is allowed across the whole fleet (`SET NX`).
- **Strict Pydantic.** Validation uses `strict=True` and `extra="forbid"`, so `"125.5"` is rejected for a float field instead of being silently coerced.
- **Self-correction stays on the same provider**, because that provider holds the conversation context.
- **Cache keys are scoped per tenant** (`sha256(schema + text)` under each tenant's prefix), so one tenant can never read another tenant's data.
- **Cache hits cost 0** and do not count against the budget. Failed validations do count, because those tokens were actually spent.
- **The audit log is append-only at the database level.** A trigger rejects UPDATE and DELETE, and an audit-write failure is logged but does not fail the client request.

## Known limitations

- Auth is JWT bearer tokens minted by an admin endpoint, not a full OAuth 2.0 authorization server or IdP integration.
- Caching is exact-match only. There is no semantic caching.
- Pricing tables are hardcoded in the provider modules and must be updated by hand.
- Budgets are checked before a call and charged after it, so one in-flight request can overshoot the budget slightly.
- Ollama isn't available on Render. The cloud deployment uses Claude, OpenAI or the mock provider.
