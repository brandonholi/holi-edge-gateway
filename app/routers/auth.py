import uuid
from typing import Optional
from fastapi import APIRouter, HTTPException, Header
from ..clients.redis_client import get_state_redis
from ..core.security import create_access_token, verify_access_token
from ..schemas.auth import LoginIn, TokenResponse, RefreshIn

router = APIRouter(prefix="/v1/auth", tags=["Auth"])


@router.post("/login", response_model=TokenResponse)
async def login(credentials: LoginIn):
    """
    Authenticate user.
    Returns 15-min JWT access token (verified locally) and stores 30-day refresh token in holi-estado.
    """
    # In production, this validates against Odoo or auth provider
    partner_id = 1  # Standard public/resolved partner for demonstration

    access_token = create_access_token(partner_id)
    refresh_token = str(uuid.uuid4())

    state_redis = get_state_redis()
    # Store refresh token in holi-estado (30 days)
    await state_redis.set(f"rt:{refresh_token}", str(partner_id), ex=2592000)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=900,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(payload: RefreshIn):
    """Rotate refresh token and issue new 15-min access token."""
    state_redis = get_state_redis()
    old_rt_key = f"rt:{payload.refresh_token}"
    partner_id = await state_redis.get(old_rt_key)

    if not partner_id:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token.")

    # Invalidate old refresh token (rotative)
    await state_redis.delete(old_rt_key)

    new_refresh_token = str(uuid.uuid4())
    await state_redis.set(f"rt:{new_refresh_token}", partner_id, ex=2592000)
    new_access_token = create_access_token(int(partner_id))

    return TokenResponse(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        expires_in=900,
    )


@router.post("/logout")
async def logout(payload: RefreshIn):
    """Revoke refresh token immediately."""
    state_redis = get_state_redis()
    await state_redis.delete(f"rt:{payload.refresh_token}")
    return {"message": "Logged out successfully"}
