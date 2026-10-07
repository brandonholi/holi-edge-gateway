"""Home content served from Redis: products of a collection (spec section 2)."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.main import app
from app.routers import catalog as catalog_router
from app.routers import content as content_router
from app.routers.content import visible_en_sede
from tests.test_auth_otp import FakeRedis

SEDE = "LIM01"
HEADERS = {"X-Holi-Sede": SEDE}


@pytest.fixture
def cliente(monkeypatch):
    redis = FakeRedis()
    redis.values[f"v:cat:{SEDE}"] = "3"
    redis.sets[f"cat:3:{SEDE}"] = {"A", "B", "C", "D"}
    redis.sets[f"cat:3:{SEDE}:10"] = {"A"}
    redis.sets[f"cat:3:{SEDE}:11"] = {"B"}
    redis.sets[f"cat:3:{SEDE}:20"] = {"C"}
    for sku in "ABCD":
        redis.hashes[f"prod:{sku}"] = {"product_id": "1", "sku": sku, "name": sku}
        redis.hashes[f"price:1:{SEDE}:{sku}"] = {"list_price": "5", "final_price": "5"}
    redis.values["col:5"] = json.dumps({
        "id": 5, "nombre": "Bebidas",
        "categorias": [{"id": 10, "nombre": "Bebidas", "descendientes": [10, 11]}],
        "skus": ["D", "Z"],
    })
    monkeypatch.setattr(catalog_router, "get_cache_redis", lambda: redis)
    monkeypatch.setattr(content_router, "get_cache_redis", lambda: redis)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def skus(res):
    return [i["sku"] for i in res.json()["items"]]


@pytest.mark.asyncio
async def test_coleccion_une_categorias_y_skus(cliente):
    res = await cliente.get("/v1/collections/5/products", headers=HEADERS)
    assert res.status_code == 200
    assert skus(res) == ["A", "B", "D"]
    assert res.json()["paginacion"]["total"] == 3
    assert res.json()["version_catalogo"] == 3


@pytest.mark.asyncio
async def test_coleccion_filtra_por_chip(cliente):
    res = await cliente.get("/v1/collections/5/products?categoria_id=10", headers=HEADERS)
    assert skus(res) == ["A", "B"]


@pytest.mark.asyncio
async def test_chip_ajeno_es_422(cliente):
    res = await cliente.get("/v1/collections/5/products?categoria_id=20", headers=HEADERS)
    assert res.status_code == 422
    assert res.json()["errores"][0]["campo"] == "categoria_id"


@pytest.mark.asyncio
async def test_coleccion_inexistente_es_404(cliente):
    res = await cliente.get("/v1/collections/99/products", headers=HEADERS)
    assert res.status_code == 404
    assert res.json()["type"].endswith("/no-encontrado")


@pytest.mark.asyncio
async def test_coleccion_pagina(cliente):
    res = await cliente.get("/v1/collections/5/products?por_pagina=2&pagina=2", headers=HEADERS)
    assert skus(res) == ["D"]


@pytest.mark.asyncio
async def test_coleccion_sin_sede_es_422(cliente):
    res = await cliente.get("/v1/collections/5/products")
    assert res.status_code == 422


def test_visible_en_sede():
    t = datetime(2026, 10, 10, tzinfo=timezone.utc)
    assert visible_en_sede({}, SEDE, t)
    assert visible_en_sede({"tiendas": [SEDE]}, SEDE, t)
    assert not visible_en_sede({"tiendas": ["OTRA"]}, SEDE, t)
    assert not visible_en_sede({"inicio": "2026-10-11T00:00:00Z"}, SEDE, t)
    assert not visible_en_sede({"fin": "2026-10-09T23:59:59Z"}, SEDE, t)
    assert visible_en_sede({"inicio": "2026-10-10T00:00:00Z", "fin": "2026-10-10T00:00:00Z"}, SEDE, t)
    assert visible_en_sede({"inicio": None, "fin": None}, SEDE, t)
