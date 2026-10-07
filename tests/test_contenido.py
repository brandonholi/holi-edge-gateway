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
    redis.values["col:6"] = json.dumps({
        "id": 6, "nombre": "Mundo",
        "categorias": [{"id": 10, "nombre": "Bebidas", "descendientes": [10]},
                       {"id": 20, "nombre": "Fuera", "descendientes": [20]}],
        "skus": [],
    })
    redis.hashes[f"tree:3:{SEDE}"] = {
        "10": json.dumps({"id": 10, "name": "Bebidas"}),
        "11": json.dumps({"id": 11, "name": "Gaseosas", "parent_id": 10}),
    }
    img = {"md": "b/1-aaaaaaaa-md.webp", "lg": "b/1-aaaaaaaa-lg.webp"}

    def slide(tipo, id_):
        return {"imagen": img, "texto_alternativo": "x", "etiqueta": None, "titulo": None,
                "destino": {"tipo": tipo, "id": id_}}

    redis.values["content:banners"] = json.dumps([
        {"id": 1, "tipo": "carrusel", "ubicacion": "principal", "categoria_id": None, "tiendas": [],
         "inicio": "2026-10-01T00:00:00Z", "fin": "2026-10-31T00:00:00Z",
         "imagenes": [slide("producto", "A"), slide("producto", "Z")]},
        {"id": 2, "tipo": "estatico", "ubicacion": "principal", "categoria_id": None, "tiendas": [],
         "inicio": None, "fin": "2026-10-05T00:00:00Z", "imagenes": [slide("producto", "A")]},
        {"id": 3, "tipo": "estatico", "ubicacion": "principal", "categoria_id": None, "tiendas": ["ARE01"],
         "inicio": None, "fin": None, "imagenes": [slide("producto", "A")]},
        {"id": 4, "tipo": "estatico", "ubicacion": "categoria", "categoria_id": 10, "tiendas": [],
         "inicio": None, "fin": None, "imagenes": [slide("categoria", "10")]},
        {"id": 5, "tipo": "estatico", "ubicacion": "principal", "categoria_id": None, "tiendas": [],
         "inicio": None, "fin": None, "imagenes": [slide("mundo", "4")]},
    ])
    redis.values["content:mundos"] = json.dumps([
        {"id": 3, "nombre": "Verano", "subtitulo": None, "imagen": {"md": "m/3-aaaaaaaa-md.webp", "lg": None},
         "coleccion_id": 6, "tiendas": [], "inicio": None, "fin": None},
        {"id": 4, "nombre": "Futuro", "subtitulo": None, "imagen": None,
         "coleccion_id": 6, "tiendas": [], "inicio": "2026-11-01T00:00:00Z", "fin": None},
    ])
    monkeypatch.setattr(content_router, "ahora",
                        lambda: datetime(2026, 10, 6, 12, tzinfo=timezone.utc))
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


@pytest.mark.asyncio
async def test_categoria_fuera_del_catalogo_de_la_sede_se_excluye(cliente):
    redis = catalog_router.get_cache_redis()
    redis.sets[f"cat:3:{SEDE}:10"] = {"A", "X"}  # X is not in cat:3:LIM01
    res = await cliente.get("/v1/collections/5/products", headers=HEADERS)
    assert skus(res) == ["A", "B", "D"]
    res = await cliente.get("/v1/collections/5/products?categoria_id=10", headers=HEADERS)
    assert skus(res) == ["A", "B"]


@pytest.mark.asyncio
@pytest.mark.parametrize("valor", ["[1]", '{"id":5,"categorias":[{"nombre":"x"}]}', '{"id":5,"categorias":5}'])
async def test_coleccion_malformada_es_404(cliente, valor):
    catalog_router.get_cache_redis().values["col:5"] = valor
    res = await cliente.get("/v1/collections/5/products", headers=HEADERS)
    assert res.status_code == 404


def test_fecha_invalida_o_ingenua_no_es_visible():
    t = datetime(2026, 10, 10, tzinfo=timezone.utc)
    assert not visible_en_sede({"inicio": "basura"}, SEDE, t)
    assert not visible_en_sede({"fin": "2026-10-11T00:00:00"}, SEDE, t)


@pytest.mark.asyncio
async def test_banners_principal_filtra_tienda_vigencia_y_destino(cliente):
    res = await cliente.get("/v1/banners?ubicacion=principal", headers=HEADERS)
    assert res.status_code == 200
    items = res.json()["items"]
    assert [b["id"] for b in items] == [1]
    assert [i["destino"] for i in items[0]["imagenes"]] == [{"tipo": "producto", "id": "A"}]


@pytest.mark.asyncio
async def test_banner_que_queda_vacio_no_sale(cliente):
    res = await cliente.get("/v1/banners?ubicacion=principal", headers=HEADERS)
    assert 5 not in [b["id"] for b in res.json()["items"]]


@pytest.mark.asyncio
async def test_banners_por_categoria(cliente):
    res = await cliente.get("/v1/banners?ubicacion=categoria&categoria_id=10", headers=HEADERS)
    items = res.json()["items"]
    assert [b["id"] for b in items] == [4]
    assert items[0]["imagenes"][0]["destino"] == {"tipo": "categoria", "id": "10"}


@pytest.mark.asyncio
async def test_banners_categoria_sin_id_es_422(cliente):
    res = await cliente.get("/v1/banners?ubicacion=categoria", headers=HEADERS)
    assert res.status_code == 422
    assert res.json()["errores"][0]["campo"] == "categoria_id"


@pytest.mark.asyncio
async def test_banners_destinos_mundo_y_coleccion(cliente):
    redis = catalog_router.get_cache_redis()
    banners = json.loads(redis.values["content:banners"])
    banners[4]["imagenes"] = [
        {**banners[4]["imagenes"][0], "destino": {"tipo": "mundo", "id": "3"}},
        {**banners[4]["imagenes"][0], "destino": {"tipo": "coleccion", "id": "5"}},
        {**banners[4]["imagenes"][0], "destino": {"tipo": "coleccion", "id": "99"}},
    ]
    redis.values["content:banners"] = json.dumps(banners)
    res = await cliente.get("/v1/banners?ubicacion=principal", headers=HEADERS)
    quinto = [b for b in res.json()["items"] if b["id"] == 5][0]
    assert [i["destino"]["id"] for i in quinto["imagenes"]] == ["3", "5"]


@pytest.mark.asyncio
@pytest.mark.parametrize("clave", ["content:banners", "content:mundos"])
async def test_contenido_malformado_es_lista_vacia(cliente, clave):
    catalog_router.get_cache_redis().values[clave] = '{"x": 1}'
    ruta = "/v1/banners?ubicacion=principal" if "banners" in clave else "/v1/worlds"
    res = await cliente.get(ruta, headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["items"] == []


@pytest.mark.asyncio
async def test_banner_malformado_se_omite(cliente):
    redis = catalog_router.get_cache_redis()
    banners = json.loads(redis.values["content:banners"])
    redis.values["content:banners"] = json.dumps(["x", {"id": 9}] + banners)
    res = await cliente.get("/v1/banners?ubicacion=principal", headers=HEADERS)
    assert [b["id"] for b in res.json()["items"]] == [1]


@pytest.mark.asyncio
async def test_mundos_lista_solo_visibles(cliente):
    res = await cliente.get("/v1/worlds", headers=HEADERS)
    assert [m["id"] for m in res.json()["items"]] == [3]


@pytest.mark.asyncio
async def test_mundo_detalle_chips_del_arbol(cliente):
    res = await cliente.get("/v1/worlds/3", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["categorias"] == [{"id": 10, "nombre": "Bebidas"}]
    assert res.json()["coleccion_id"] == 6


@pytest.mark.asyncio
async def test_mundo_no_visible_es_404(cliente):
    res = await cliente.get("/v1/worlds/4", headers=HEADERS)
    assert res.status_code == 404
    assert (await cliente.get("/v1/worlds/99", headers=HEADERS)).status_code == 404
