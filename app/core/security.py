import time
import jwt
from typing import Optional, Dict, Any
from .config import settings


class SecurityError(Exception):
    pass


def create_access_token(partner_id: int, extra_claims: Optional[Dict[str, Any]] = None) -> str:
    """Creates a short-lived 15-minute JWT access token."""
    now = int(time.time())
    payload = {
        "sub": str(partner_id),
        "iat": now,
        "exp": now + (settings.JWT_ACCESS_EXPIRE_MINUTES * 60),
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_access_token(token: str) -> Dict[str, Any]:
    """Locally verifies JWT signature and expiration without making network hops."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise SecurityError("Invalid token type")
        return payload
    except jwt.ExpiredSignatureError:
        raise SecurityError("Access token has expired")
    except jwt.InvalidTokenError as e:
        raise SecurityError(f"Invalid token: {e}")
