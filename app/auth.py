"""JWT access/refresh tokens and role checks."""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import bcrypt
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import db
from app.errors import fail


bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    id: UUID
    role: str


def password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def password_matches(password: str, digest: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), digest.encode("ascii"))


def encode_token(user_id: UUID, role: str, token_type: str, lifetime: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": str(user_id), "role": role, "type": token_type,
         "iat": now, "exp": now + lifetime, "jti": str(uuid4())},
        os.environ["JWT_SECRET"],
        algorithm="HS256",
    )


def token_pair(user_id: UUID, role: str) -> dict:
    return {
        "access_token": encode_token(user_id, role, "access", timedelta(minutes=20)),
        "refresh_token": encode_token(user_id, role, "refresh", timedelta(days=14)),
        "token_type": "bearer",
    }


def decode_token(token: str, expected_type: str) -> dict:
    invalid = "REFRESH_TOKEN_INVALID" if expected_type == "refresh" else "TOKEN_INVALID"
    try:
        claims = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=["HS256"], options={"require": ["sub", "exp", "iat", "type", "role"]})
        if claims.get("type") != expected_type or claims.get("role") not in {"USER", "SELLER", "ADMIN"}:
            fail(invalid, "Wrong token type or role")
        UUID(claims["sub"])
        return claims
    except jwt.ExpiredSignatureError:
        fail("REFRESH_TOKEN_INVALID" if expected_type == "refresh" else "TOKEN_EXPIRED", "Token expired")
    except (jwt.InvalidTokenError, ValueError, KeyError):
        fail(invalid, "Invalid token")


def current_principal(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if not credentials or credentials.scheme.lower() != "bearer":
        fail("TOKEN_INVALID", "Bearer access token required")
    claims = decode_token(credentials.credentials, "access")
    principal = Principal(UUID(claims["sub"]), claims["role"])
    request.state.user_id = str(principal.id)
    return principal


def require_role(principal: Principal, *roles: str) -> None:
    if principal.role not in roles:
        fail("ACCESS_DENIED", "Role is not allowed for this operation")


def bootstrap_admin() -> None:
    email = os.getenv("BOOTSTRAP_ADMIN_EMAIL")
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD")
    if not email or not password:
        return
    with db.connection() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email = %s", (email.lower(),)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, 'ADMIN')",
                (email.lower(), password_hash(password)),
            )
