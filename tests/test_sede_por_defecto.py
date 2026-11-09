"""
The default store: what the app sees before the customer picks an address.

Odoo marks one store as the default and publishes it to holi-cache. A request
without X-Holi-Sede falls back to it, and the response says which store
answered so the app can show "prices from Holi Miraflores".
"""
import json

import httpx
import pytest

from app.core import sede as sede_module
from app.main import app
from app.routers import catalog as catalog_router
from tests.test_auth_otp import FakeRedis

CLAVE = "content:default_store"


class RedisCaido:
    async def get(self, key):
        raise ConnectionError("holi-cache is down")


@pytest.fixture
def entorno(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(sede_module, "get_cache_redis", lambda: redis)
    monkeypatch.setattr(catalog_router, "get_cache_redis", lambda: redis)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, redis


def _marcar(redis, sede="LIM01", nombre="Holi Miraflores"):
    redis.values[CLAVE] = json.dumps({"sede": sede, "nombre": nombre})


@pytest.mark.asyncio
async def test_sin_cabecera_usa_la_sede_por_defecto(entorno):
    client, redis = entorno
    _marcar(redis)

    res = await client.get("/v1/catalog")
    assert res.status_code == 200
    assert res.json()["sede"] == "LIM01"
    assert res.headers["X-Holi-Sede"] == "LIM01"


@pytest.mark.asyncio
async def test_la_cabecera_manda_sobre_la_sede_por_defecto(entorno):
    client, redis = entorno
    _marcar(redis)

    res = await client.get("/v1/catalog", headers={"X-Holi-Sede": "aqp01"})
    assert res.status_code == 200
    assert res.json()["sede"] == "AQP01"
    assert res.headers["X-Holi-Sede"] == "AQP01"


@pytest.mark.asyncio
@pytest.mark.parametrize("publicado", [None, "null", "{}", "no es json"])
async def test_sin_cabecera_ni_sede_por_defecto_es_422(entorno, publicado):
    client, redis = entorno
    if publicado is not None:
        redis.values[CLAVE] = publicado

    res = await client.get("/v1/catalog")
    assert res.status_code == 422
    assert res.json()["type"].endswith("/parametros-invalidos")
    assert "X-Holi-Sede" in res.json()["detail"]


@pytest.mark.asyncio
async def test_sin_cabecera_y_redis_caido_es_503(monkeypatch):
    monkeypatch.setattr(sede_module, "get_cache_redis", lambda: RedisCaido())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/v1/catalog")
    assert res.status_code == 503


@pytest.mark.asyncio
async def test_con_cabecera_no_lee_redis(monkeypatch):
    """A request that names its store must not depend on the default being readable."""
    monkeypatch.setattr(sede_module, "get_cache_redis", lambda: RedisCaido())
    monkeypatch.setattr(catalog_router, "get_cache_redis", lambda: FakeRedis())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/v1/catalog", headers={"X-Holi-Sede": "LIM01"})
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_ruta_de_la_sede_por_defecto(entorno):
    client, redis = entorno
    _marcar(redis, "LIM02", "Holi Larco")

    res = await client.get("/v1/stores/default")
    assert res.status_code == 200
    assert res.json() == {"sede": "LIM02", "nombre": "Holi Larco"}


@pytest.mark.asyncio
async def test_ruta_sin_sede_por_defecto_es_404(entorno):
    client, redis = entorno
    redis.values[CLAVE] = "null"

    res = await client.get("/v1/stores/default")
    assert res.status_code == 404
    assert res.json()["type"].endswith("/no-encontrado")
