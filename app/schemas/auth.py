from typing import Optional
from pydantic import BaseModel


class LoginIn(BaseModel):
    email: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 900  # 15 min in seconds
    refresh_token: Optional[str] = None
