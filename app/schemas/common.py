from typing import Optional, Dict, Any
from pydantic import BaseModel, Field


class ProblemDetails(BaseModel):
    """RFC 7807 Problem Details for HTTP APIs (application/problem+json)."""
    type: str = Field(default="about:blank")
    title: str
    status: int
    detail: str
    instance: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None
