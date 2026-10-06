"""
json.pe DNI lookup (ADR-014).

These tests pin the provider's response shape field by field. If json.pe ever
changes `data.apellido_paterno` or wraps the payload differently, this is what
notices — not a customer whose name arrives empty.
"""
import httpx
import pytest

from app.clients.reniec_client import ReniecClient, _cache_key
from app.core.config import settings
from tests.test_auth_otp import FakeRedis

# Verbatim from https://docs.json.pe/api-consulta/endpoint/dni.md
RESPUESTA_EXITOSA = {
    "success": True,
    "message": "exito",
    "data": {
        "numero": "27427864",
        "codigo_verificacion": "7",
        "nombres": "JOSE PEDRO",
        "apellido_paterno": "CASTILLO",
        "apellido_materno": "TERRONES",
        "nombre_completo": "CASTILLO TERRONES, JOSE PEDRO",
        "direccion": "",
        "direccion_completa": "",
        "ubigeo_reniec": "",
        "ubigeo_sunat": "",
        "ubigeo": [None, None, None],
    },
}


def _respuesta(status_code, json_body=None, text=""):
    return httpx.Response(
        status_code=status_code,
        json=json_body if json_body is not None else None,
        text=None if json_body is not None else text,
        request=httpx.Request("POST", settings.RENIEC_API_URL),
    )


def test_respuesta_exitosa_se_interpreta_en_orden_natural():
    """The provider says 'APELLIDOS, NOMBRES'; the app greets people."""
    resultado = ReniecClient()._interpret(_respuesta(200, RESPUESTA_EXITOSA))

    assert resultado.available and resultado.found
    assert resultado.nombres == "JOSE PEDRO"
    assert resultado.apellidos == "CASTILLO TERRONES"
    assert resultado.full_name == "JOSE PEDRO CASTILLO TERRONES"
    assert resultado.validation_state == "validado"


def test_documento_no_encontrado_es_pendiente_no_error():
    resultado = ReniecClient()._interpret(
        _respuesta(404, {"success": False, "message": "No se encontró DNI"})
    )
    assert resultado.available is True
    assert resultado.found is False
    assert resultado.validation_state == "pendiente"


def test_success_falso_con_200_tambien_es_no_encontrado():
    resultado = ReniecClient()._interpret(_respuesta(200, {"success": False, "message": "Bad Request"}))
    assert resultado.available is True
    assert resultado.found is False


def test_credenciales_rechazadas_no_son_culpa_del_cliente():
    """A bad token or an empty balance must not look like a missing document."""
    resultado = ReniecClient()._interpret(_respuesta(401, {"success": False}))
    assert resultado.available is False
    assert resultado.reason == "proveedor_rechazo_credenciales"
    assert resultado.validation_state == "pendiente"


def test_cuerpo_que_no_es_json_no_rompe_el_registro():
    resultado = ReniecClient()._interpret(_respuesta(200, text="<html>502 Bad Gateway</html>"))
    assert resultado.available is False
    assert resultado.reason == "proveedor_error"


def test_datos_vacios_cuentan_como_no_encontrado():
    resultado = ReniecClient()._interpret(_respuesta(200, {"success": True, "data": {}}))
    assert resultado.found is False


@pytest.mark.asyncio
async def test_la_cache_evita_gastar_un_segundo_credito():
    redis = FakeRedis()
    cliente = ReniecClient()
    cliente.url = settings.RENIEC_API_URL
    cliente.token = "token-de-prueba"

    resultado = cliente._interpret(_respuesta(200, RESPUESTA_EXITOSA))
    await cliente._write_cache(redis, "27427864", resultado)

    desde_cache = await cliente._read_cache(redis, "27427864")
    assert desde_cache is not None
    assert desde_cache.full_name == "JOSE PEDRO CASTILLO TERRONES"
    assert desde_cache.validation_state == "validado"


@pytest.mark.asyncio
async def test_la_cache_no_guarda_el_dni_en_la_clave():
    """A dump of holi-estado must not be a list of DNIs."""
    redis = FakeRedis()
    cliente = ReniecClient()
    await cliente._write_cache(redis, "27427864", cliente._interpret(_respuesta(200, RESPUESTA_EXITOSA)))

    claves = list(redis.values.keys())
    assert claves == [_cache_key("27427864")]
    assert "27427864" not in claves[0]


@pytest.mark.asyncio
async def test_sin_token_configurado_el_registro_sigue_como_pendiente():
    cliente = ReniecClient()
    cliente.token = ""
    resultado = await cliente.lookup("27427864", None)

    assert resultado.available is False
    assert resultado.reason == "proveedor_no_configurado"
    assert resultado.validation_state == "pendiente"
