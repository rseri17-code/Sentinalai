"""AG UI Authentication & RBAC Middleware.

JWT-based authentication with role-based access control.

Roles (in order of ascending privilege):
  viewer   → read-only access to all data
  operator → viewer + start investigations + trigger replay
  approver → operator + approve/reject/pause control actions
  admin    → all + delete, config, purge

JWT claim: custom:agui_role (string)
Issuer: Cognito user pool (configurable) or test JWT

Enforcement:
  L1: JWT signature validation (via Cognito JWKS or HS256 test secret)
  L2: Role check per route (via Depends(require_role(...)))
  L3: Request context carries actor_id + actor_role for audit logging

Security notes:
  - JWT_SECRET is for dev/test only; prod uses Cognito JWKS
  - Token expiry is validated
  - Audience claim is validated against AGUI_AUDIENCE env var
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

ROLE_HIERARCHY = {"viewer": 0, "operator": 1, "approver": 2, "admin": 3}

AUTH_REQUIRED = os.getenv("AGUI_AUTH_REQUIRED", "true").lower() == "true"
JWT_SECRET = os.getenv("AGUI_JWT_SECRET", "dev-secret-change-in-production")
AGUI_AUDIENCE = os.getenv("AGUI_AUDIENCE", "agui")
COGNITO_JWKS_URL = os.getenv("AGUI_COGNITO_JWKS_URL", "")


@dataclass
class ActorContext:
    actor_id: str
    actor_role: str
    email: Optional[str] = None
    token: Optional[str] = None


security = HTTPBearer(auto_error=False)


def _decode_jwt(token: str) -> dict:
    """Decode and validate JWT. Returns claims dict."""
    if COGNITO_JWKS_URL:
        return _decode_jwt_cognito(token)
    return _decode_jwt_local(token)


def _decode_jwt_local(token: str) -> dict:
    """HS256 local JWT for dev/test."""
    try:
        import jwt  # PyJWT
        claims = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            audience=AGUI_AUDIENCE,
            options={"verify_exp": True},
        )
        return claims
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        )


def _decode_jwt_cognito(token: str) -> dict:
    """RS256 Cognito JWT via JWKS."""
    try:
        import jwt
        from jwt import PyJWKClient
        jwks_client = PyJWKClient(COGNITO_JWKS_URL)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=AGUI_AUDIENCE,
            options={"verify_exp": True},
        )
        return claims
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Cognito token validation failed: {e}",
        )


def _get_actor_from_claims(claims: dict) -> ActorContext:
    actor_id = claims.get("sub", claims.get("username", "anonymous"))
    role = claims.get("custom:agui_role", claims.get("role", "viewer"))
    if role not in ROLE_HIERARCHY:
        role = "viewer"
    return ActorContext(
        actor_id=actor_id,
        actor_role=role,
        email=claims.get("email"),
    )


async def get_actor(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> ActorContext:
    """FastAPI dependency: extract and validate actor from JWT."""
    if not AUTH_REQUIRED:
        # Dev mode: anonymous admin access
        return ActorContext(actor_id="dev-user", actor_role="admin")

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required",
        )

    claims = _decode_jwt(credentials.credentials)
    actor = _get_actor_from_claims(claims)
    actor.token = credentials.credentials
    return actor


def require_role(minimum_role: str):
    """FastAPI dependency factory: require a minimum role."""
    required_level = ROLE_HIERARCHY.get(minimum_role, 0)

    async def check_role(actor: ActorContext = Depends(get_actor)) -> ActorContext:
        actor_level = ROLE_HIERARCHY.get(actor.actor_role, -1)
        if actor_level < required_level:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Insufficient role. Required: {minimum_role}, "
                    f"Actual: {actor.actor_role}"
                ),
            )
        return actor

    return check_role


def make_test_token(
    actor_id: str = "test-user",
    role: str = "admin",
    expires_in: int = 3600,
) -> str:
    """Generate a test JWT token (dev/test only)."""
    try:
        import jwt
        payload = {
            "sub": actor_id,
            "custom:agui_role": role,
            "aud": AGUI_AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + expires_in,
        }
        return jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    except ImportError:
        # Return a base64 stub if PyJWT not installed
        import base64, json
        payload = {"sub": actor_id, "custom:agui_role": role}
        return base64.b64encode(json.dumps(payload).encode()).decode()
