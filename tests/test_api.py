import pytest
import httpx
from app.main import app
from app.core.security import create_access_token, verify_access_token


@pytest.mark.asyncio
async def test_health_endpoint():
    """Verify health check endpoint returns 200."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert "status" in data


@pytest.mark.asyncio
async def test_catalog_missing_sede_header():
    """Verify missing X-Holi-Sede returns RFC-7807 400 Problem."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/v1/catalog")
        assert res.status_code == 400
        assert res.headers.get("Content-Type") == "application/problem+json"
        data = res.json()
        assert data["status"] == 400
        assert "X-Holi-Sede" in data["detail"]


def test_jwt_token_local_verification():
    """Verify short-lived 15-min access token generation and local verification."""
    token = create_access_token(partner_id=42, extra_claims={"name": "Brandon"})
    payload = verify_access_token(token)
    assert payload["sub"] == "42"
    assert payload["name"] == "Brandon"
    assert payload["type"] == "access"
