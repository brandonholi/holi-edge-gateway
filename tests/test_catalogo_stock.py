"""Without a stock band in Redis the Edge answers agotado, never disponible (ADR-011)."""
import logging

import httpx
import pytest

from app.main import app
from app.routers import catalog as catalog_router
from app.routers import search as search_router
from tests.test_auth_otp import FakeRedis

SEDE = "LIM01"
SKU = "SKU1"
HEADERS = {"X-Holi-Sede": SEDE}


class FakeMeili:
    async def search_skus(self, q, sede, limit, offset):
        return [SKU], 1


@pytest.fixture
def entorno(monkeypatch):
    redis = FakeRedis()
    redis.hashes[f"prod:{SKU}"] = {"product_id": "1", "sku": SKU, "name": "Arroz"}
    redis.hashes[f"price:1:{SEDE}:{SKU}"] = {"list_price": "5", "final_price": "5"}
    redis.sets[f"cat:1:{SEDE}"] = {SKU}
    monkeypatch.setattr(catalog_router, "get_cache_redis", lambda: redis)
    monkeypatch.setattr(search_router, "get_cache_redis", lambda: redis)
    monkeypatch.setattr(search_router, "meili", FakeMeili())
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_listado_sin_banda_es_agotado(entorno):
    res = await entorno.get("/v1/catalog", headers=HEADERS)
    assert res.json()["items"][0]["disponibilidad"] == "agotado"


@pytest.mark.asyncio
async def test_detalle_sin_banda_es_agotado(entorno, caplog):
    with caplog.at_level(logging.WARNING):
        res = await entorno.get(f"/v1/catalog/{SKU}", headers=HEADERS)
    assert res.json()["disponibilidad"] == "agotado"
    assert f"Missing stock band for {SKU} on {SEDE}" in caplog.text


@pytest.mark.asyncio
async def test_disponibilidad_sin_banda_es_agotado(entorno):
    res = await entorno.get(f"/v1/availability/{SKU}", headers=HEADERS)
    assert res.json()["disponibilidad"] == "agotado"


@pytest.mark.asyncio
async def test_busqueda_sin_banda_es_agotado(entorno):
    res = await entorno.get("/v1/search?q=arroz", headers=HEADERS)
    assert res.json()["items"][0]["disponibilidad"] == "agotado"
