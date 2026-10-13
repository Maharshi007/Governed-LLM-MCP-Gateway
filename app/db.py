"""Append-only audit log (PostgreSQL in prod, SQLite fallback for local dev)."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings
from app.logging_conf import logger


class Base(DeclarativeBase):
    pass


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    schema_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cache_hit: Mapped[int] = mapped_column(Integer, default=0)
    correction_rounds: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# DB-level immutability: reject UPDATE/DELETE even if app code (or a human) tries.
_PG_IMMUTABLE = [
    """CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
       BEGIN RAISE EXCEPTION 'audit_log is append-only'; END;
       $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS audit_log_no_mutation ON audit_log",
    """CREATE TRIGGER audit_log_no_mutation BEFORE UPDATE OR DELETE ON audit_log
       FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()""",
]
_SQLITE_IMMUTABLE = [
    """CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
       BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;""",
    """CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
       BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;""",
]

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None


async def init_db() -> None:
    global _engine, _sessionmaker
    url = get_settings().database_url
    _engine = create_async_engine(url, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        stmts = _PG_IMMUTABLE if url.startswith("postgresql") else _SQLITE_IMMUTABLE
        for stmt in stmts:
            await conn.execute(text(stmt))
    logger.info("audit db ready", extra={"dialect": _engine.dialect.name})


async def close_db() -> None:
    if _engine is not None:
        await _engine.dispose()


async def write_audit(**fields) -> None:
    if _sessionmaker is None:
        return
    try:
        async with _sessionmaker() as session:
            session.add(AuditLog(**fields))
            await session.commit()
    except Exception:
        # Audit failure must not fail the client request; it is logged and alertable.
        logger.exception("audit write failed", extra={"request_id": fields.get("request_id")})


def get_sessionmaker() -> async_sessionmaker:
    if _sessionmaker is None:
        raise RuntimeError("db not initialised")
    return _sessionmaker
