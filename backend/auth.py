from __future__ import annotations

import os

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient


_jwks_clients: dict[str, PyJWKClient] = {}


def current_user_id(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    token = authorization.removeprefix("Bearer ").strip()
    jwt_key = os.getenv("CLERK_JWT_KEY", "").replace("\\n", "\n").strip()
    jwks_url = os.getenv("CLERK_JWKS_URL", "").strip()

    if not jwt_key and not token:
        return "local"
    if not token:
        raise HTTPException(401, "Authentication required")
    if not jwt_key and not jwks_url:
        raise HTTPException(503, "Backend authentication is not configured")

    try:
        if jwt_key:
            claims = jwt.decode(token, jwt_key, algorithms=["RS256"], options={"verify_aud": False})
        else:
            client = _jwks_clients.setdefault(jwks_url, PyJWKClient(jwks_url))
            signing_key = client.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, signing_key.key, algorithms=["RS256"], options={"verify_aud": False})
    except (jwt.PyJWTError, OSError) as exc:
        raise HTTPException(401, "Invalid authentication token") from exc
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise HTTPException(401, "Authentication token has no user")
    return subject
