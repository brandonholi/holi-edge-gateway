"""
Enforces the API conventions of CLAUDE.md on every route, current and future.

These tests read the OpenAPI document the app generates, so a new endpoint is
checked the moment it is registered: nobody has to remember to add it here.
"""
import re

import httpx
import pytest
from fastapi import FastAPI

from app.clients.odoo_client import OdooClientError
from app.clients.otp_provider import OtpProviderError
from app.core.errors import CATALOGO, TIPO_BASE, ApiError, instalar_manejadores
from app.main import app

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

# English words that mean an internal name leaked into the public contract
# (ADR-012). `items` and `sku` are part of the contract on purpose.
PALABRAS_INTERNAS = {
    "name", "price", "prices", "qty", "quantity", "line", "lines", "amount", "state", "partner",
    "product", "products", "category", "categories", "availability", "available", "image", "currency",
    "discount", "slot", "slots", "order", "featured", "selling", "message", "messages", "limit",
    "offset", "page", "count", "notes", "coupon", "delivery", "address", "parent", "barcode", "uom",
}

# RFC 7807 members keep their standard names.
ESQUEMAS_EXENTOS = {"Problema"}

# Arrays inside a resource that are not "the list" the endpoint returns. A new
# one is a deliberate decision: add it here in the same change.
COLECCIONES_DE_RECURSO = {"lineas", "canales_disponibles", "skus_destacados", "skus_mas_vendidos", "errores"}


@pytest.fixture(scope="module")
def openapi():
    return app.openapi()


def _rutas_publicas(openapi):
    for ruta, metodos in openapi["paths"].items():
        if ruta.startswith("/v1/"):
            for metodo, operacion in metodos.items():
                yield ruta, metodo, operacion


def _esquema(openapi, ref_o_esquema):
    ref = ref_o_esquema.get("$ref")
    if not ref:
        return None, ref_o_esquema
    nombre = ref.rsplit("/", 1)[-1]
    return nombre, openapi["components"]["schemas"][nombre]


def test_toda_ruta_declara_un_objeto_como_respuesta(openapi):
    for ruta, metodo, operacion in _rutas_publicas(openapi):
        contenido = operacion["responses"].get("200", {}).get("content", {}).get("application/json", {})
        esquema = contenido.get("schema")
        assert esquema, f"{metodo.upper()} {ruta} no declara response_model"
        nombre, cuerpo = _esquema(openapi, esquema)
        assert cuerpo.get("type") != "array", (
            f"{metodo.upper()} {ruta} responde un arreglo suelto; usa Lista[T] / ListaPaginada[T] (items)"
        )
        assert nombre, f"{metodo.upper()} {ruta} debe responder un modelo Pydantic con nombre"


def test_toda_ruta_documenta_errores_problem_json(openapi):
    for ruta, metodo, operacion in _rutas_publicas(openapi):
        for codigo in ("4XX", "5XX"):
            assert codigo in operacion["responses"], f"{metodo.upper()} {ruta} no documenta {codigo}"
        if "422" in operacion["responses"]:
            esquema = operacion["responses"]["422"]["content"]["application/json"]["schema"]
            assert esquema["$ref"].endswith("/Problema"), f"{metodo.upper()} {ruta}: 422 no usa Problema"


def test_campos_en_snake_case_y_en_espanol(openapi):
    errores = []
    for nombre, esquema in openapi["components"]["schemas"].items():
        if nombre in ESQUEMAS_EXENTOS or nombre in ("HTTPValidationError", "ValidationError"):
            continue
        for campo in esquema.get("properties", {}):
            if not SNAKE_CASE.match(campo):
                errores.append(f"{nombre}.{campo}: no es snake_case")
            internas = PALABRAS_INTERNAS.intersection(campo.split("_"))
            if internas:
                errores.append(f"{nombre}.{campo}: nombre interno en inglés ({', '.join(sorted(internas))})")
    assert not errores, "\n".join(errores)


def test_parametros_de_consulta_en_espanol(openapi):
    errores = []
    for ruta, metodo, operacion in _rutas_publicas(openapi):
        for parametro in operacion.get("parameters", []):
            if parametro["in"] not in ("query", "path"):
                continue
            nombre = parametro["name"]
            if not SNAKE_CASE.match(nombre) or PALABRAS_INTERNAS.intersection(nombre.split("_")):
                errores.append(f"{metodo.upper()} {ruta}: parámetro '{nombre}'")
    assert not errores, "\n".join(errores)


def test_las_listas_se_llaman_items(openapi):
    errores = []
    for ruta, metodo, operacion in _rutas_publicas(openapi):
        esquema = operacion["responses"].get("200", {}).get("content", {}).get("application/json", {}).get("schema")
        if not esquema:
            continue
        nombre, cuerpo = _esquema(openapi, esquema)
        for campo, definicion in cuerpo.get("properties", {}).items():
            if definicion.get("type") == "array" and campo != "items" and campo not in COLECCIONES_DE_RECURSO:
                errores.append(f"{metodo.upper()} {ruta}: {nombre}.{campo} es una lista y no se llama 'items'")
    assert not errores, "\n".join(errores)


def test_la_paginacion_usa_pagina_y_por_pagina(openapi):
    for ruta, metodo, operacion in _rutas_publicas(openapi):
        esquema = operacion["responses"].get("200", {}).get("content", {}).get("application/json", {}).get("schema")
        if not esquema:
            continue
        _, cuerpo = _esquema(openapi, esquema)
        if "paginacion" in cuerpo.get("properties", {}):
            nombres = {p["name"] for p in operacion.get("parameters", [])}
            assert {"pagina", "por_pagina"} <= nombres, f"{metodo.upper()} {ruta} pagina sin pagina/por_pagina"


# --------------------------------------------------------------------------- #
# The problem document itself
# --------------------------------------------------------------------------- #

def _app_de_prueba() -> FastAPI:
    prueba = FastAPI()

    @prueba.middleware("http")
    async def _request_id(request, call_next):
        request.state.request_id = request.headers.get("X-Request-Id", "req-prueba")
        return await call_next(request)

    instalar_manejadores(prueba)

    @prueba.get("/api-error")
    async def _api_error():
        raise ApiError("stock-insuficiente", "1 línea sin stock", lineas=[{"sku": "A", "disponible": 0}])

    @prueba.get("/odoo")
    async def _odoo():
        raise OdooClientError(409, "Out of stock", "Line A has no stock",
                              {"codigo": "stock-insuficiente", "lineas": [{"sku": "A"}]})

    @prueba.get("/twilio-cuenta")
    async def _twilio_cuenta():
        # Twilio 21608: the account lacks an approved compliance profile.
        raise OtpProviderError(403, "OTP Provider Error", "To send messages to unverified numbers...", "proveedor_error")

    @prueba.get("/odoo-firma")
    async def _odoo_firma():
        raise OdooClientError(401, "Invalid Signature", "HMAC verification failed.")

    @prueba.get("/odoo-ip")
    async def _odoo_ip():
        raise OdooClientError(403, "Forbidden IP", "Your IP address is not authorized.")

    @prueba.get("/odoo-telefono")
    async def _odoo_telefono():
        raise OdooClientError(403, "Phone Not Verified", "An account cannot be created without a verified phone.",
                              {"codigo": "telefono-no-verificado"})

    @prueba.get("/odoo-desconocido")
    async def _odoo_desconocido():
        raise OdooClientError(502, "Odoo Error", "Traceback in internal endpoint")

    @prueba.get("/inesperado")
    async def _inesperado():
        raise RuntimeError("boom")

    @prueba.get("/validacion")
    async def _validacion(pagina: int):
        return {}

    return prueba


async def _get(ruta: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app_de_prueba(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(ruta, headers={"X-Request-Id": "req-123"})


def _es_problema(res: httpx.Response, codigo: str) -> dict:
    assert res.headers["content-type"].startswith("application/problem+json")
    cuerpo = res.json()
    assert cuerpo["type"] == TIPO_BASE + codigo
    assert cuerpo["status"] == res.status_code == CATALOGO[codigo][0]
    assert cuerpo["title"] == CATALOGO[codigo][1]
    assert cuerpo["request_id"] == "req-123"
    assert cuerpo["instance"]
    return cuerpo


async def test_api_error_lleva_los_campos_extra():
    cuerpo = _es_problema(await _get("/api-error"), "stock-insuficiente")
    assert cuerpo["detail"] == "1 línea sin stock"
    assert cuerpo["lineas"] == [{"sku": "A", "disponible": 0}]


async def test_error_de_odoo_con_codigo_conocido_se_conserva():
    cuerpo = _es_problema(await _get("/odoo"), "stock-insuficiente")
    assert cuerpo["lineas"] == [{"sku": "A"}]


async def test_twilio_rechaza_la_cuenta_no_es_un_error_del_cliente():
    _es_problema(await _get("/twilio-cuenta"), "servicio-no-disponible")


async def test_odoo_rechaza_la_firma_no_es_un_error_del_cliente():
    _es_problema(await _get("/odoo-firma"), "servicio-no-disponible")


async def test_odoo_rechaza_la_ip_no_es_un_error_del_cliente():
    _es_problema(await _get("/odoo-ip"), "servicio-no-disponible")


async def test_un_403_de_negocio_con_codigo_llega_tal_cual():
    cuerpo = _es_problema(await _get("/odoo-telefono"), "telefono-no-verificado")
    assert "phone" not in cuerpo["detail"]


async def test_error_de_odoo_desconocido_no_filtra_el_texto_interno():
    cuerpo = _es_problema(await _get("/odoo-desconocido"), "servicio-no-disponible")
    assert "Traceback" not in cuerpo["detail"]


async def test_error_inesperado_es_problem_json():
    _es_problema(await _get("/inesperado"), "error-interno")


async def test_validacion_indica_el_campo():
    cuerpo = _es_problema(await _get("/validacion?pagina=uno"), "parametros-invalidos")
    assert cuerpo["errores"][0]["campo"] == "pagina"


async def test_ruta_inexistente_es_problem_json():
    _es_problema(await _get("/no-existe"), "no-encontrado")


def test_codigo_fuera_del_catalogo_falla_al_lanzarlo():
    with pytest.raises(KeyError):
        ApiError("codigo-inventado")


def test_catalogo_bien_formado():
    for codigo, (status, titulo, detalle) in CATALOGO.items():
        assert re.fullmatch(r"[a-z]+(-[a-z]+)*", codigo), codigo
        assert 400 <= status < 600 and titulo and detalle
