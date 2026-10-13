from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Governed LLM & MCP Gateway"
    environment: str = "dev"

    # Auth
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 60
    admin_api_key: str = "dev-admin-key"  # used to mint tenant tokens

    # Infra. Empty redis_url -> in-process fakeredis; sqlite fallback for the audit DB.
    redis_url: str = ""
    database_url: str = "sqlite+aiosqlite:///./gateway.db"

    # Providers (tried in order; providers without credentials are skipped)
    provider_chain: str = "openai,anthropic,ollama,mock"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"
    ollama_base_url: str = ""  # e.g. http://localhost:11434; empty disables Ollama
    ollama_model: str = "llama3.2:3b"
    ollama_timeout_seconds: float = 120.0  # local models can be slow on first load
    enable_mock_provider: bool = True
    provider_timeout_seconds: float = 30.0

    # Resilience
    retry_attempts: int = 3
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 4.0
    breaker_failure_threshold: int = 3
    breaker_reset_seconds: int = 30
    max_correction_attempts: int = 2

    # Cache / limits
    cache_ttl_seconds: int = 3600
    idempotency_ttl_seconds: int = 86400
    rate_limit_per_minute: int = 60
    default_token_budget: int = 1_000_000  # tokens per tenant per day

    @property
    def providers(self) -> list[str]:
        return [p.strip() for p in self.provider_chain.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
