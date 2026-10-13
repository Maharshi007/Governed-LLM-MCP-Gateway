import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select

from app import db, store
from app.auth import Principal, issue_token, require_admin_key, require_role
from app.circuit_breaker import CircuitBreaker
from app.config import get_settings
from app.gateway import AllProvidersFailed, ExtractionFailed, Gateway
from app.logging_conf import configure_logging, logger
from app.metrics import CACHE_HITS, CACHE_MISSES, COST_USD, LATENCY, REQUESTS, TOKENS
from app.providers.base import LLMProvider
from app.schemas import SCHEMA_REGISTRY, ExtractRequest, ExtractResponse, TokenRequest, TokenResponse


def build_providers() -> list[LLMProvider]:
    s = get_settings()
    providers: list[LLMProvider] = []
    for name in s.providers:
        if name == "openai" and s.openai_api_key:
            from app.providers.openai_provider import OpenAIProvider
            providers.append(OpenAIProvider(s.openai_api_key, s.openai_model, s.provider_timeout_seconds))
        elif name == "anthropic" and s.anthropic_api_key:
            from app.providers.anthropic_provider import AnthropicProvider
            providers.append(AnthropicProvider(s.anthropic_api_key, s.anthropic_model, s.provider_timeout_seconds))
        elif name == "ollama" and s.ollama_base_url:
            from app.providers.ollama_provider import OllamaProvider
            providers.append(OllamaProvider(s.ollama_base_url, s.ollama_model, s.ollama_timeout_seconds))
        elif name == "mock" and s.enable_mock_provider:
            from app.providers.mock_provider import MockProvider
            providers.append(MockProvider())
    return providers


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await store.init_redis()
    await db.init_db()
    if not getattr(app.state, "gateway", None):
        app.state.gateway = Gateway(build_providers())
    logger.info("gateway started", extra={"providers": [p.name for p in app.state.gateway.providers]})
    yield
    await db.close_db()
    await store.close_redis()


app = FastAPI(
    title="StructurAI",
    description="AI gateway: routing, fallback, caching, and strict schema validation for LLM extraction.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info("request", extra={
        "request_id": request_id, "method": request.method, "path": request.url.path,
        "status": response.status_code, "latency_ms": round((time.perf_counter() - start) * 1000, 2),
    })
    return response


def _telemetry_headers(response: Response, body: ExtractResponse, remaining_rate: int | None = None) -> None:
    response.headers["X-Cache-Hit"] = str(body.cache_hit).lower()
    response.headers["X-Token-Cost"] = f"{body.cost_usd:.6f}"
    response.headers["X-Provider"] = body.provider
    response.headers["X-Prompt-Tokens"] = str(body.prompt_tokens)
    response.headers["X-Completion-Tokens"] = str(body.completion_tokens)
    response.headers["X-Correction-Rounds"] = str(body.correction_rounds)
    if remaining_rate is not None:
        response.headers["X-RateLimit-Remaining"] = str(remaining_rate)


@app.post("/v1/extract", response_model=ExtractResponse)
async def extract(
    req: ExtractRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_role("extract")),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    s = get_settings()
    start = time.perf_counter()
    request_id = request.state.request_id
    tenant = principal.tenant_id

    # 1. Authorization done by dependency. Now rate limit + budget.
    allowed, remaining = await store.check_rate_limit(tenant, s.rate_limit_per_minute)
    if not allowed:
        REQUESTS.labels(tenant, req.schema_name, "rate_limited").inc()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded",
                            headers={"Retry-After": "60"})
    if await store.tokens_used_today(tenant) >= principal.token_budget:
        REQUESTS.labels(tenant, req.schema_name, "budget_exceeded").inc()
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "daily token budget exhausted")  # fast path

    schema_cls = SCHEMA_REGISTRY.get(req.schema_name)
    if schema_cls is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"unknown schema '{req.schema_name}'; available: {sorted(SCHEMA_REGISTRY)}")

    # 2. Idempotency + cache
    if idempotency_key:
        prior = await store.idempotency_get(tenant, idempotency_key)
        if prior:
            body = ExtractResponse(**prior)
            _telemetry_headers(response, body, remaining)
            response.headers["Idempotent-Replayed"] = "true"
            return body
        if not await store.idempotency_lock(tenant, idempotency_key):
            raise HTTPException(status.HTTP_409_CONFLICT, "request with this Idempotency-Key is in progress")

    try:
        key = store.cache_key(tenant, req.schema_name, req.text)
        cached = await store.cache_get(key)
        if cached:
            CACHE_HITS.labels(tenant).inc()
            latency = (time.perf_counter() - start) * 1000
            body = ExtractResponse(
                **{**cached, "request_id": request_id, "cache_hit": True, "cost_usd": 0.0,
                   "prompt_tokens": 0, "completion_tokens": 0, "correction_rounds": 0,
                   "latency_ms": round(latency, 2)}
            )
            LATENCY.labels("hit").observe(latency / 1000)
            REQUESTS.labels(tenant, req.schema_name, "success").inc()
            await _finish(tenant, idempotency_key, body, request_id)
            _telemetry_headers(response, body, remaining)
            return body
        CACHE_MISSES.labels(tenant).inc()

        # Reserve estimated tokens atomically; reconciled to actual usage below.
        reserved = store.estimate_tokens(req.text)
        if not await store.reserve_tokens(tenant, reserved, principal.token_budget):
            REQUESTS.labels(tenant, req.schema_name, "budget_exceeded").inc()
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED,
                                "request would exceed the daily token budget")
        actual_tokens = 0

        # 3 + 4. Routing, resilience, validation, self-correction
        try:
            result = await request.app.state.gateway.extract(
                req.schema_name, schema_cls, req.text, req.provider_hint
            )
            actual_tokens = result.usage.prompt_tokens + result.usage.completion_tokens
        except AllProvidersFailed as e:
            REQUESTS.labels(tenant, req.schema_name, "upstream_unavailable").inc()
            await db.write_audit(request_id=request_id, tenant_id=tenant, schema_name=req.schema_name,
                                 status="upstream_unavailable", error=str(e.errors),
                                 latency_ms=(time.perf_counter() - start) * 1000)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                {"message": "all LLM providers unavailable", "providers": e.errors})
        except ExtractionFailed as e:
            # Tokens were really spent, so they count against the budget.
            actual_tokens = e.usage.prompt_tokens + e.usage.completion_tokens
            REQUESTS.labels(tenant, req.schema_name, "validation_failed").inc()
            await db.write_audit(request_id=request_id, tenant_id=tenant, schema_name=req.schema_name,
                                 status="validation_failed", error=str(e)[:2000],
                                 prompt_tokens=e.usage.prompt_tokens,
                                 completion_tokens=e.usage.completion_tokens, cost_usd=e.usage.cost_usd,
                                 latency_ms=(time.perf_counter() - start) * 1000)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "LLM output could not be validated against the schema")
        finally:
            await store.adjust_tokens(tenant, actual_tokens - reserved)

        # 5. Telemetry
        latency = (time.perf_counter() - start) * 1000
        u = result.usage
        body = ExtractResponse(
            request_id=request_id, schema_name=req.schema_name, data=result.data,
            provider=result.provider, model=result.model, cache_hit=False,
            correction_rounds=result.correction_rounds, prompt_tokens=u.prompt_tokens,
            completion_tokens=u.completion_tokens, cost_usd=round(u.cost_usd, 8),
            latency_ms=round(latency, 2),
        )
        await store.cache_set(key, body.model_dump())
        TOKENS.labels(tenant, "prompt").inc(u.prompt_tokens)
        TOKENS.labels(tenant, "completion").inc(u.completion_tokens)
        COST_USD.labels(tenant, result.provider).inc(u.cost_usd)
        LATENCY.labels("miss").observe(latency / 1000)
        REQUESTS.labels(tenant, req.schema_name, "success").inc()
        await _finish(tenant, idempotency_key, body, request_id)

        # 6. Response
        _telemetry_headers(response, body, remaining)
        return body
    finally:
        if idempotency_key:
            await store.idempotency_unlock(tenant, idempotency_key)


async def _finish(tenant: str, idempotency_key: str | None, body: ExtractResponse, request_id: str) -> None:
    if idempotency_key:
        await store.idempotency_set(tenant, idempotency_key, body.model_dump())
    await db.write_audit(
        request_id=request_id, tenant_id=tenant, schema_name=body.schema_name, status="success",
        provider=body.provider, model=body.model, cache_hit=int(body.cache_hit),
        correction_rounds=body.correction_rounds, prompt_tokens=body.prompt_tokens,
        completion_tokens=body.completion_tokens, cost_usd=body.cost_usd, latency_ms=body.latency_ms,
    )


@app.post("/v1/auth/token", response_model=TokenResponse, dependencies=[Depends(require_admin_key)])
async def create_token(req: TokenRequest):
    token, expires_in = issue_token(req.tenant_id, list(req.roles), req.token_budget)
    return TokenResponse(access_token=token, expires_in=expires_in)


@app.get("/v1/schemas")
async def list_schemas(principal: Principal = Depends(require_role("extract"))):
    return {name: cls.model_json_schema() for name, cls in SCHEMA_REGISTRY.items()}


@app.get("/v1/usage")
async def usage(principal: Principal = Depends(require_role("extract"))):
    used = await store.tokens_used_today(principal.tenant_id)
    return {"tenant_id": principal.tenant_id, "tokens_used_today": used,
            "token_budget": principal.token_budget, "remaining": max(0, principal.token_budget - used)}


@app.get("/v1/audit")
async def audit(limit: int = 50, principal: Principal = Depends(require_role("extract"))):
    async with db.get_sessionmaker()() as session:
        q = (select(db.AuditLog).where(db.AuditLog.tenant_id == principal.tenant_id)
             .order_by(db.AuditLog.id.desc()).limit(min(limit, 500)))
        rows = (await session.execute(q)).scalars().all()
    return [{c.name: getattr(r, c.name) for c in db.AuditLog.__table__.columns} for r in rows]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request):
    checks = {}
    try:
        await store.get_redis().ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"
    breakers = {p.name: (await CircuitBreaker(p.name).state()).value for p in request.app.state.gateway.providers}
    ok = checks["redis"] == "ok" and any(v != "open" for v in breakers.values())
    return JSONResponse({"ready": ok, "checks": checks, "circuits": breakers}, status_code=200 if ok else 503)


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
