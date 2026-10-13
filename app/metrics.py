from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "structurai_requests_total", "Extraction requests", ["tenant", "schema", "status"]
)
CACHE_HITS = Counter("structurai_cache_hits_total", "Cache hits", ["tenant"])
CACHE_MISSES = Counter("structurai_cache_misses_total", "Cache misses", ["tenant"])
PROVIDER_CALLS = Counter(
    "structurai_provider_calls_total", "Upstream LLM calls", ["provider", "outcome"]
)
FALLBACKS = Counter(
    "structurai_fallbacks_total", "Requests served by a non-primary provider", ["provider"]
)
VALIDATION_RETRIES = Counter(
    "structurai_validation_retries_total", "Self-correction rounds", ["schema"]
)
TOKENS = Counter("structurai_tokens_total", "Tokens consumed", ["tenant", "kind"])
COST_USD = Counter("structurai_cost_usd_total", "Estimated spend in USD", ["tenant", "provider"])
LATENCY = Histogram(
    "structurai_request_latency_seconds",
    "End-to-end request latency",
    ["cache"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
BREAKER_OPEN = Gauge("structurai_circuit_open", "1 if the provider circuit is open", ["provider"])
