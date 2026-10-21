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


@pytest.mark.asyncio
async def test_categoria_padre_incluye_sus_subcategorias(monkeypatch):
    """Products sit in leaf categories; a parent picked on the home screen must not open empty."""
    import json as _json
    redis = FakeRedis()
    for sku in ("A", "B", "C"):
        redis.hashes[f"prod:{sku}"] = {"product_id": "1", "sku": sku, "name": sku}
        redis.hashes[f"price:1:{SEDE}:{sku}"] = {"list_price": "5", "final_price": "5"}
    redis.sets[f"cat:1:{SEDE}"] = {"A", "B", "C"}
    redis.sets[f"cat:1:{SEDE}:11"] = {"A"}      # child of 10
    redis.sets[f"cat:1:{SEDE}:12"] = {"B"}      # grandchild of 10, through 11
    redis.sets[f"cat:1:{SEDE}:20"] = {"C"}      # another branch
    redis.hashes[f"tree:1:{SEDE}"] = {
        "10": _json.dumps({"id": 10, "name": "Bebidas", "parent_id": None}),
        "11": _json.dumps({"id": 11, "name": "Gaseosas", "parent_id": 10}),
        "12": _json.dumps({"id": 12, "name": "Colas", "parent_id": 11}),
        "20": _json.dumps({"id": 20, "name": "Limpieza", "parent_id": None}),
    }
    monkeypatch.setattr(catalog_router, "get_cache_redis", lambda: redis)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    res = await client.get("/v1/catalog?categoria_id=10", headers=HEADERS)
    assert [p["sku"] for p in res.json()["items"]] == ["A", "B"]
    assert res.json()["paginacion"]["total"] == 2
    hoja = await client.get("/v1/catalog?categoria_id=12", headers=HEADERS)
    assert [p["sku"] for p in hoja.json()["items"]] == ["B"]
