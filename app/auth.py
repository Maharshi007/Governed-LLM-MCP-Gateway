"""JWT bearer auth + role-based access control."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

_bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    tenant_id: str
    roles: list[str]
    token_budget: int


def issue_token(tenant_id: str, roles: list[str], token_budget: int | None) -> tuple[str, int]:
    s = get_settings()
    expires_in = s.jwt_expiry_minutes * 60
    now = datetime.now(timezone.utc)
    claims = {
        "sub": tenant_id,
        "roles": roles,
        "budget": token_budget or s.default_token_budget,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
        "iss": s.app_name,
    }
    return jwt.encode(claims, s.jwt_secret, algorithm=s.jwt_algorithm), expires_in


async def get_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    s = get_settings()
    try:
        claims = jwt.decode(
            creds.credentials, s.jwt_secret, algorithms=[s.jwt_algorithm],
            issuer=s.app_name, options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    return Principal(
        tenant_id=claims["sub"],
        roles=claims.get("roles", []),
        token_budget=int(claims.get("budget", s.default_token_budget)),
    )


def require_role(role: str):
    async def checker(principal: Principal = Depends(get_principal)) -> Principal:
        if role not in principal.roles and "admin" not in principal.roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role '{role}' required")
        return principal

    return checker


async def require_admin_key(x_admin_key: str = Header(...)) -> None:
    if not secrets.compare_digest(x_admin_key, get_settings().admin_api_key):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid admin key")
