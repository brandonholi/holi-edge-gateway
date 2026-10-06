"""
Master data the app renders in its selectors: geography, identity document
types and the opening-screen messages.

The point of these endpoints is that the app carries no hard-coded lists, so
what is tested is that whatever Odoo says arrives intact, and that saying it
once is enough.
"""
import httpx
import pytest

from app.core.breaker import CircuitBreakerOpenError
from app.main import app
from app.routers import master_data as md_router
from tests.test_auth_otp import FakeRedis


class FakeOdooMaster:
    def __init__(self):
        self.calls = []
        self.departamentos = [{"id": 15, "nombre": "Lima", "codigo": "LIM"}]
        self.provincias = [{"id": 141, "nombre": "Lima", "codigo": "1501"}]
        self.distritos = [
            {"id": 900, "nombre": "Miraflores", "codigo": "150122"},
            {"id": 901, "nombre": "San Isidro", "codigo": "150131"},
        ]
        self.tipos = [
            {"id": 5, "codigo": "1", "nombre": "DNI", "longitud": 8,
             "solo_digitos": True, "ayuda": "8 dígitos", "es_ruc": False},
            {"id": 4, "codigo": "6", "nombre": "RUC", "longitud": 11,
             "solo_digitos": True, "ayuda": "11 dígitos", "es_ruc": True},
        ]
        self.mensajes = [
            {"id": 1, "titulo": "Horario especial", "mensaje": "Atendemos hasta las 6 p. m.",
             "tipo": "aviso"},
        ]
        self.zonas = [{
            "zona_id": 1, "nombre": "Miraflores", "prioridad": 10,
            "sede": "LIM01", "tienda": "Holi Miraflores",
            "tienda_lat": -12.115, "tienda_lng": -77.035,
            "limites": {"lat_min": -12.13, "lat_max": -12.10,
                        "lng_min": -77.05, "lng_max": -77.02},
            "poligono": [
                {"lat": -12.13, "lng": -77.05}, {"lat": -12.13, "lng": -77.02},
                {"lat": -12.10, "lng": -77.02}, {"lat": -12.10, "lng": -77.05},
            ],
        }]
        self.mensaje_sin_cobertura = {
            "titulo": "Aún no llegamos a tu zona",
            "mensaje": "Estamos ampliando nuestra cobertura.",
        }
        self.raise_breaker = False

    def _guard(self, name):
        self.calls.append(name)
        if self.raise_breaker:
            raise CircuitBreakerOpenError("odoo is down")

    async def fetch_departments(self, request_id):
        self._guard("departments")
        return {"departamentos": self.departamentos}

    async def fetch_provinces(self, department_id, request_id):
        self._guard(f"provinces:{department_id}")
        return {"provincias": self.provincias}

    async def fetch_districts(self, province_id, request_id):
        self._guard(f"districts:{province_id}")
        return {"distritos": self.distritos}

    async def fetch_document_types(self, request_id):
        self._guard("document-types")
        return {"tipos_documento": self.tipos}

    async def fetch_coverage_zones(self, request_id):
        self._guard("coverage-zones")
        return {"zonas": self.zonas, "mensaje_sin_cobertura": self.mensaje_sin_cobertura}

    async def fetch_home_messages(self, sede_code, request_id):
        self._guard(f"home-messages:{sede_code}")
        return {"mensajes": self.mensajes}


@pytest.fixture
def entorno(monkeypatch):
    redis = FakeRedis()
    odoo = FakeOdooMaster()
    monkeypatch.setattr(md_router, "get_cache_redis", lambda: redis)
    monkeypatch.setattr(md_router, "odoo_client", odoo)

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    yield client, redis, odoo


# --------------------------------------------------------------------------- #
# Geography
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_departamentos_no_exigen_sede(entorno):
    """The address form is what decides the sede; requiring it first is circular."""
    client, _redis, _odoo = entorno
    res = await client.get("/v1/locations/departments")
    assert res.status_code == 200, res.text
    assert res.json()["items"][0]["nombre"] == "Lima"


@pytest.mark.asyncio
async def test_provincias_exigen_departamento(entorno):
    client, _redis, _odoo = entorno
    assert (await client.get("/v1/locations/provinces")).status_code == 422
    res = await client.get("/v1/locations/provinces", params={"departamento_id": 15})
    assert res.status_code == 200
    assert res.json()["departamento_id"] == 15


@pytest.mark.asyncio
async def test_distritos_traen_el_ubigeo(entorno):
    """
    The ubigeo is what the address record and the receipt carry, and a
    coordinate does not give it. It must survive the trip intact.
    """
    client, _redis, _odoo = entorno
    res = await client.get("/v1/locations/districts", params={"provincia_id": 141})
    assert res.status_code == 200

    distritos = res.json()["items"]
    assert [d["nombre"] for d in distritos] == ["Miraflores", "San Isidro"]
    assert distritos[0]["codigo"] == "150122"


@pytest.mark.asyncio
async def test_identificador_invalido_es_rechazado(entorno):
    client, _redis, _odoo = entorno
    assert (await client.get("/v1/locations/districts",
                             params={"provincia_id": 0})).status_code == 422
    assert (await client.get("/v1/locations/districts",
                             params={"provincia_id": "lima"})).status_code == 422


# --------------------------------------------------------------------------- #
# Document types
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_tipos_de_documento_llegan_con_sus_reglas(entorno):
    """The app validates with what the backend sends, not with its own list."""
    client, _redis, _odoo = entorno
    res = await client.get("/v1/document-types")
    assert res.status_code == 200

    tipos = res.json()["items"]
    assert [t["nombre"] for t in tipos] == ["DNI", "RUC"]

    dni, ruc = tipos
    assert (dni["longitud"], dni["es_ruc"]) == (8, False)
    assert (ruc["longitud"], ruc["es_ruc"]) == (11, True)
    assert ruc["codigo"] == "6"  # Código SUNAT, necesario para el comprobante


# --------------------------------------------------------------------------- #
# Home messages
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_mensajes_de_inicio_sin_sesion_ni_sede(entorno):
    client, _redis, _odoo = entorno
    res = await client.get("/v1/home-messages")
    assert res.status_code == 200
    cuerpo = res.json()
    assert cuerpo["sede"] is None
    assert cuerpo["items"][0]["titulo"] == "Horario especial"
    assert cuerpo["items"][0]["tipo"] == "aviso"


@pytest.mark.asyncio
async def test_mensajes_se_pueden_acotar_por_sede(entorno):
    client, _redis, odoo = entorno
    res = await client.get("/v1/home-messages", params={"sede": "lim01"})
    assert res.status_code == 200
    assert res.json()["sede"] == "LIM01"
    assert "home-messages:LIM01" in odoo.calls


@pytest.mark.asyncio
async def test_la_sede_tambien_se_lee_de_la_cabecera(entorno):
    client, _redis, odoo = entorno
    res = await client.get("/v1/home-messages", headers={"X-Holi-Sede": "LIM02"})
    assert res.status_code == 200
    assert res.json()["sede"] == "LIM02"
    assert "home-messages:LIM02" in odoo.calls


# --------------------------------------------------------------------------- #
# Caching and degradation
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_la_cache_evita_repetir_la_llamada_a_odoo(entorno):
    """A district list must not spend a semaphore slot on every app launch."""
    client, _redis, odoo = entorno

    await client.get("/v1/locations/districts", params={"provincia_id": 141})
    await client.get("/v1/locations/districts", params={"provincia_id": 141})
    await client.get("/v1/locations/districts", params={"provincia_id": 141})

    assert odoo.calls == ["districts:141"]


@pytest.mark.asyncio
async def test_cada_provincia_se_cachea_por_separado(entorno):
    client, _redis, odoo = entorno
    await client.get("/v1/locations/districts", params={"provincia_id": 141})
    await client.get("/v1/locations/districts", params={"provincia_id": 142})
    assert odoo.calls == ["districts:141", "districts:142"]


@pytest.mark.asyncio
async def test_sin_redis_sigue_respondiendo(monkeypatch):
    """Redis down makes this slower, never broken."""
    odoo = FakeOdooMaster()

    def redis_caido():
        raise RuntimeError("holi-cache unreachable")

    monkeypatch.setattr(md_router, "get_cache_redis", redis_caido)
    monkeypatch.setattr(md_router, "odoo_client", odoo)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/v1/document-types")

    assert res.status_code == 200
    assert len(res.json()["items"]) == 2


@pytest.mark.asyncio
async def test_odoo_caido_responde_503_no_lista_vacia(entorno):
    """An empty selector would look like 'we deliver nowhere'. Say it is down."""
    client, _redis, odoo = entorno
    odoo.raise_breaker = True

    res = await client.get("/v1/locations/departments")
    assert res.status_code == 503


# --------------------------------------------------------------------------- #
# Delivery coverage
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_punto_cubierto_devuelve_la_tienda(entorno):
    client, _redis, _odoo = entorno
    res = await client.get("/v1/coverage", params={"lat": -12.115, "lng": -77.035})
    assert res.status_code == 200

    cuerpo = res.json()
    assert cuerpo["cobertura"] is True
    assert cuerpo["tienda"]["sede"] == "LIM01"
    assert cuerpo["tienda"]["nombre"] == "Holi Miraflores"
    assert cuerpo["mensaje"] is None


@pytest.mark.asyncio
async def test_fuera_de_cobertura_responde_200_con_mensaje(entorno):
    """
    Not an error. A 4xx would send the app to an error screen instead of the
    message it is meant to show.
    """
    client, _redis, _odoo = entorno
    res = await client.get("/v1/coverage", params={"lat": -16.40, "lng": -71.53})
    assert res.status_code == 200

    cuerpo = res.json()
    assert cuerpo["cobertura"] is False
    assert cuerpo["tienda"] is None
    assert cuerpo["mensaje"]["titulo"] == "Aún no llegamos a tu zona"


@pytest.mark.asyncio
async def test_sin_zonas_cargadas_todo_queda_fuera(entorno):
    """Before anyone draws a polygon, nothing is covered — and it says so calmly."""
    client, _redis, odoo = entorno
    odoo.zonas = []

    res = await client.get("/v1/coverage", params={"lat": -12.115, "lng": -77.035})
    assert res.status_code == 200
    assert res.json()["cobertura"] is False


@pytest.mark.asyncio
async def test_coordenadas_invertidas_se_rechazan(entorno):
    """
    Swapping lat and lng is the classic mistake. Saying 422 tells the mobile
    team what is wrong; answering 'no coverage' would send them hunting.
    """
    client, _redis, _odoo = entorno
    res = await client.get("/v1/coverage", params={"lat": -77.035, "lng": -12.115})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_faltan_coordenadas(entorno):
    client, _redis, _odoo = entorno
    assert (await client.get("/v1/coverage")).status_code == 422
    assert (await client.get("/v1/coverage", params={"lat": -12.115})).status_code == 422


@pytest.mark.asyncio
async def test_las_zonas_se_cachean(entorno):
    """Resolving a point must not call Odoo on every app launch."""
    client, _redis, odoo = entorno
    for _ in range(3):
        await client.get("/v1/coverage", params={"lat": -12.115, "lng": -77.035})
    assert odoo.calls == ["coverage-zones"]
