import time
import uuid
import jwt
from typing import Optional, Dict, Any
from .config import settings

# The Odoo venv of this workspace ships an unrelated package also imported as
# `jwt` (jwt==1.4.0). Running the Edge there fails mid sign-up, after Twilio
# already approved the code; fail at startup instead.
if not hasattr(jwt, "encode"):
    raise ImportError(
        "El módulo 'jwt' cargado no es PyJWT. Levanta el Edge con el entorno del gateway: "
        "holi-edge-gateway\\.venv\\Scripts\\python.exe -m uvicorn app.main:app"
    )


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


def create_verification_token(subject: str, purpose: str) -> str:
    """
    Proof that a one-time code was approved by the OTP provider.

    It is the only thing that authorises the write into Odoo, so it is short-lived
    and single-use: the 'jti' is burned in holi-estado the first time it is spent.
    'purpose' keeps a code approved for a registration from being spent on a
    password reset, and vice versa.
    """
    now = int(time.time())
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + (settings.VERIFICATION_TOKEN_EXPIRE_MINUTES * 60),
        "type": "verification",
        "purpose": purpose,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def verify_verification_token(token: str, expected_purpose: str) -> Dict[str, Any]:
    """Verifies signature, expiry and purpose. Burning the 'jti' is the caller's job."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise SecurityError("Verification token has expired")
    except jwt.InvalidTokenError as e:
        raise SecurityError(f"Invalid verification token: {e}")

    if payload.get("type") != "verification":
        raise SecurityError("Invalid token type")
    if payload.get("purpose") != expected_purpose:
        raise SecurityError("Verification token was issued for a different purpose")
    if not payload.get("jti"):
        raise SecurityError("Verification token has no identifier")
    return payload
