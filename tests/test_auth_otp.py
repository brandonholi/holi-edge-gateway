"""
Sign-up handshake, session and recovery (RF-001, RF-051, RF-062).

Twilio and Odoo are replaced by doubles and holi-estado by an in-memory stand-in,
so the suite exercises the real routing, guards and token handling without
sending a single message or touching the ERP.
"""
import time

import httpx
import pytest

from app.clients import otp_provider as otp_provider_module
from app.clients.otp_provider import CheckResult, OtpProvider, SendResult, set_otp_provider
from app.clients.reniec_client import ReniecResult, set_reniec_client
from app.core.config import settings
from app.main import app
from app.routers import auth as auth_router
from app.routers import master_data as md_router


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #

class FakeRedis:
    """Enough of holi-estado to run the handshake: strings, hashes, sets, TTLs."""

    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.sets = {}
        self.expiries = {}

    # -- internals --
    def _alive(self, key):
        expiry = self.expiries.get(key)
        if expiry is not None and expiry <= time.time():
            self.values.pop(key, None)
            self.hashes.pop(key, None)
            self.sets.pop(key, None)
            self.expiries.pop(key, None)
            return False
        return True

    # -- strings --
    async def get(self, key):
        return self.values.get(key) if self._alive(key) else None

    async def set(self, key, value, ex=None, nx=False):
        self._alive(key)
        if nx and key in self.values:
            return None
        self.values[key] = str(value)
        if ex:
            self.expiries[key] = time.time() + ex
        return True

    async def incr(self, key):
        self._alive(key)
        current = int(self.values.get(key, 0)) + 1
        self.values[key] = str(current)
        return current

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            self.hashes.pop(key, None)
            self.sets.pop(key, None)
            self.expiries.pop(key, None)
        return len(keys)

    async def expire(self, key, seconds, **kwargs):
        self.expiries[key] = time.time() + seconds
        return True

    async def ttl(self, key):
        if not self._alive(key):
            return -2
        expiry = self.expiries.get(key)
        if expiry is None:
            return -1 if (key in self.values or key in self.hashes) else -2
        return max(int(expiry - time.time()), 1)

    # -- hashes --
    async def hset(self, key, field=None, value=None, mapping=None):
        self._alive(key)
        bucket = self.hashes.setdefault(key, {})
        if mapping:
            bucket.update({k: str(v) for k, v in mapping.items()})
        if field is not None:
            bucket[field] = str(value)
        return 1

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {})) if self._alive(key) else {}

    # -- sets --
    async def sadd(self, key, *members):
        self._alive(key)
        self.sets.setdefault(key, set()).update(str(m) for m in members)
        return len(members)

    async def srem(self, key, *members):
        bucket = self.sets.get(key, set())
        for member in members:
            bucket.discard(str(member))
        return len(members)

    async def smembers(self, key):
        return set(self.sets.get(key, set())) if self._alive(key) else set()

    async def zrevrange(self, key, start, end):
        return []

    # -- pipeline --
    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.queue = []

    def incr(self, key):
        self.queue.append(("incr", (key,), {}))
        return self

    def expire(self, key, seconds, **kwargs):
        self.queue.append(("expire", (key, seconds), kwargs))
        return self

    def hgetall(self, key):
        self.queue.append(("hgetall", (key,), {}))
        return self

    def get(self, key):
        self.queue.append(("get", (key,), {}))
        return self

    async def execute(self):
        results = []
        for name, args, kwargs in self.queue:
            results.append(await getattr(self.redis, name)(*args, **kwargs))
        self.queue = []
        return results


class FakeOtpProvider(OtpProvider):
    """Approves one fixed code and records what was sent where."""

    CODE = "123456"

    def __init__(self):
        self.sent = []
        self.email_options = []

    async def send(self, destination, channel, locale="es", email_template="", substitutions=None):
        self.sent.append((destination, channel))
        self.email_options.append((email_template, substitutions))
        return SendResult(sid=f"VE{len(self.sent):032d}", channel=channel, status="pending")

    async def check(self, destination, code):
        approved = code == self.CODE
        return CheckResult(approved=approved, status="approved" if approved else "pending")


class FakeReniec:
    """
    Stands in for json.pe. Counts calls, because each real one spends a credit
    and not spending them is part of the design (ADR-014).
    """

    def __init__(self):
        self.result = ReniecResult(
            available=True, found=True,
            full_name="JOSE PEDRO CASTILLO TERRONES",
            nombres="JOSE PEDRO", apellidos="CASTILLO TERRONES",
        )
        self.calls = []

    async def lookup(self, dni, state_redis=None):
        self.calls.append(dni)
        return self.result


class FakeOdoo:
    """Stands in for the internal identity endpoints of holi_ecommerce_sync."""

    def __init__(self):
        self.available = True
        self.created = []
        self.passwords_set = []
        self.reset_account = None

    TIPOS = [
        {"id": 5, "codigo": "1", "nombre": "DNI", "longitud": 8, "solo_digitos": True,
         "ayuda": "8 dígitos", "es_ruc": False},
        {"id": 7, "codigo": "4", "nombre": "Carnet de Extranjería", "longitud": 0, "solo_digitos": False,
         "ayuda": "", "es_ruc": False},
    ]

    async def fetch_document_types(self, request_id):
        return {"tipos_documento": self.TIPOS}

    async def auth_register_precheck(self, document_type_id, document_number, email, phone, request_id):
        self.prechecked = (document_type_id, document_number)
        if not self.available:
            return {"available": False, "reason": "Ya existe una cuenta registrada con este documento."}
        return {"available": True}

    async def auth_partner_create(self, partner_data, idempotency_key, request_id):
        self.created.append((partner_data, idempotency_key))
        return {
            "partner_id": 501,
            "validation_state": partner_data.get("validation_state"),
            "fiscal_data_complete": True,
        }

    async def auth_verify_credentials(self, email, password, request_id):
        if password != "Segura123":
            return {"authenticated": False}
        return {
            "authenticated": True,
            "partner_id": 501,
            "phone_verified": True,
            "validation_state": "validado",
            "fiscal_data_complete": True,
        }

    async def auth_reset_lookup(self, email, request_id):
        if self.reset_account is None:
            return {"found": False}
        return self.reset_account

    async def auth_set_password(self, partner_id, password, request_id):
        self.passwords_set.append((partner_id, password))
        return {"updated": True, "partner_id": partner_id}


REGISTRO_VALIDO = {
    "tipo_documento_id": 5,
    "numero_documento": "45678912",
    "correo": "brandon@example.com",
    "telefono": "987654321",
    "acepta_terminos": True,
    "acepta_comerciales": False,
}


@pytest.fixture
def entorno(monkeypatch):
    """Wires the doubles in and hands back (client, redis, provider, odoo, reniec)."""
    redis = FakeRedis()
    provider = FakeOtpProvider()
    odoo = FakeOdoo()
    reniec = FakeReniec()

    monkeypatch.setattr(auth_router, "get_state_redis", lambda: redis)
    monkeypatch.setattr(auth_router, "odoo_client", odoo)
    # The document types are cached in holi-cache; keep that out of the real Redis.
    cache = FakeRedis()
    monkeypatch.setattr(md_router, "get_cache_redis", lambda: cache)
    set_otp_provider(provider)
    set_reniec_client(reniec)

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")

    yield client, redis, provider, odoo, reniec

    set_otp_provider(None)
    set_reniec_client(None)


async def _abrir_registro(client, **overrides):
    payload = dict(REGISTRO_VALIDO)
    payload.update(overrides)
    res = await client.post("/v1/auth/register", json=payload)
    assert res.status_code == 200, res.text
    return res.json()["registro_id"]


async def _verificar(client, registro_id, codigo=FakeOtpProvider.CODE):
    await client.post("/v1/auth/otp/send", json={"registro_id": registro_id})
    return await client.post("/v1/auth/otp/verify", json={"registro_id": registro_id, "codigo": codigo})


# --------------------------------------------------------------------------- #
# Sign-up handshake
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_registro_no_escribe_en_odoo_hasta_el_paso_final(entorno):
    """Steps 1 to 3 must leave the ERP untouched."""
    client, _redis, _provider, odoo, _reniec = entorno
    registro_id = await _abrir_registro(client)
    await _verificar(client, registro_id)
    assert odoo.created == []


@pytest.mark.asyncio
async def test_flujo_completo_emite_sesion(entorno):
    client, _redis, provider, odoo, _reniec = entorno

    registro_id = await _abrir_registro(client)
    verificacion = await _verificar(client, registro_id)
    assert verificacion.status_code == 200, verificacion.text
    token = verificacion.json()["token_verificacion"]

    res = await client.post("/v1/auth/register/complete",
                            json={"token_verificacion": token, "contrasena": "Segura123"})
    assert res.status_code == 200, res.text
    sesion = res.json()
    assert sesion["cliente_id"] == 501
    assert sesion["token_acceso"] and sesion["token_renovacion"]

    # The code went out over the configured channel, to the E.164 number.
    assert provider.sent == [("+51987654321", "sms")]

    # Odoo was told the phone is already proven, and got the RENIEC name.
    partner_data, idempotency_key = odoo.created[0]
    assert partner_data["phone_verified"] is True
    assert partner_data["name"] == "JOSE PEDRO CASTILLO TERRONES"
    assert partner_data["validation_state"] == "validado"
    assert idempotency_key
    assert partner_data["document_type_id"] == 5
    assert partner_data["document_number"] == "45678912"
    assert "dni" not in partner_data


@pytest.mark.asyncio
async def test_registro_exige_aceptar_terminos(entorno):
    client, _redis, _provider, _odoo, _reniec = entorno
    res = await client.post("/v1/auth/register", json={**REGISTRO_VALIDO, "acepta_terminos": False})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_dni_ya_registrado_ofrece_recuperar(entorno):
    """RF-001 escenario 2."""
    client, _redis, _provider, odoo, _reniec = entorno
    odoo.available = False
    res = await client.post("/v1/auth/register", json=REGISTRO_VALIDO)
    assert res.status_code == 409


@pytest.mark.asyncio
async def test_dni_duplicado_no_gasta_credito_de_reniec(entorno):
    """
    The uniqueness check runs first precisely so a duplicate sign-up — or a
    script replaying one — cannot drain the json.pe balance (ADR-014).
    """
    client, _redis, _provider, odoo, reniec = entorno
    odoo.available = False

    await client.post("/v1/auth/register", json=REGISTRO_VALIDO)
    assert reniec.calls == []


@pytest.mark.asyncio
async def test_nombres_de_reniec_llegan_separados_y_bloqueados(entorno):
    client, _redis, _provider, _odoo, reniec = entorno
    res = await client.post("/v1/auth/register", json=REGISTRO_VALIDO)
    cuerpo = res.json()

    assert reniec.calls == ["45678912"]
    assert cuerpo["estado_validacion"] == "validado"
    assert cuerpo["motivo_validacion"] == ""
    assert cuerpo["datos_desde_reniec"] is True
    assert cuerpo["nombres_precargados"] == "JOSE PEDRO"
    assert cuerpo["apellidos_precargados"] == "CASTILLO TERRONES"


@pytest.mark.asyncio
async def test_reniec_caido_deja_la_cuenta_pendiente(entorno):
    """RF-001 escenario 3: sign-up proceeds, flagged pendiente."""
    client, _redis, _provider, _odoo, reniec = entorno
    reniec.result = ReniecResult(available=False, reason="proveedor_no_disponible")

    res = await client.post("/v1/auth/register",
                            json={**REGISTRO_VALIDO, "nombre": "Brandon", "apellido": "Cruzado"})
    assert res.status_code == 200
    cuerpo = res.json()
    assert cuerpo["estado_validacion"] == "pendiente"
    assert cuerpo["motivo_validacion"] == "servicio_no_disponible"
    assert cuerpo["datos_desde_reniec"] is False
    assert cuerpo["nombre_precargado"] == "Brandon Cruzado"


@pytest.mark.asyncio
async def test_documento_inexistente_se_distingue_del_servicio_caido(entorno):
    """The app needs to tell 'we could not ask' from 'that DNI does not exist'."""
    client, _redis, _provider, _odoo, reniec = entorno
    reniec.result = ReniecResult(available=True, found=False, reason="documento_no_encontrado")

    res = await client.post("/v1/auth/register",
                            json={**REGISTRO_VALIDO, "nombre": "Brandon", "apellido": "Cruzado"})
    assert res.status_code == 200
    cuerpo = res.json()
    assert cuerpo["estado_validacion"] == "pendiente"
    assert cuerpo["motivo_validacion"] == "documento_no_encontrado"


@pytest.mark.asyncio
async def test_sin_reniec_y_sin_nombre_no_se_puede_continuar(entorno):
    client, _redis, _provider, _odoo, reniec = entorno
    reniec.result = ReniecResult(available=False, reason="proveedor_no_disponible")
    res = await client.post("/v1/auth/register", json=REGISTRO_VALIDO)
    assert res.status_code == 400


# --------------------------------------------------------------------------- #
# Code handling
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_codigo_incorrecto_es_rechazado(entorno):
    client, _redis, _provider, _odoo, _reniec = entorno
    registro_id = await _abrir_registro(client)
    res = await _verificar(client, registro_id, codigo="000000")
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_intentos_agotados_descartan_el_borrador(entorno):
    """Past the ceiling the handshake dies rather than allowing more guesses."""
    client, redis, _provider, _odoo, _reniec = entorno
    registro_id = await _abrir_registro(client)
    await client.post("/v1/auth/otp/send", json={"registro_id": registro_id})

    for _ in range(settings.OTP_MAX_CHECK_ATTEMPTS):
        res = await client.post("/v1/auth/otp/verify",
                                json={"registro_id": registro_id, "codigo": "000000"})
        assert res.status_code == 400

    res = await client.post("/v1/auth/otp/verify",
                            json={"registro_id": registro_id, "codigo": FakeOtpProvider.CODE})
    assert res.status_code == 429
    assert await redis.hgetall(f"reg:{registro_id}") == {}


@pytest.mark.asyncio
async def test_reenvio_respeta_el_enfriamiento(entorno):
    client, _redis, provider, _odoo, _reniec = entorno
    registro_id = await _abrir_registro(client)

    primero = await client.post("/v1/auth/otp/send", json={"registro_id": registro_id})
    assert primero.status_code == 200
    assert primero.json()["reintento_en"] == settings.OTP_RESEND_COOLDOWN_SECONDS

    segundo = await client.post("/v1/auth/otp/send", json={"registro_id": registro_id})
    assert segundo.status_code == 429
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_destino_se_devuelve_enmascarado(entorno):
    client, _redis, _provider, _odoo, _reniec = entorno
    registro_id = await _abrir_registro(client)
    res = await client.post("/v1/auth/otp/send", json={"registro_id": registro_id})
    enmascarado = res.json()["destino_enmascarado"]
    assert enmascarado.endswith("321")
    assert "987654" not in enmascarado


@pytest.mark.asyncio
async def test_canal_no_habilitado_es_rechazado(entorno):
    client, _redis, _provider, _odoo, _reniec = entorno
    registro_id = await _abrir_registro(client)
    res = await client.post("/v1/auth/otp/send", json={"registro_id": registro_id, "canal": "whatsapp"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_registro_inexistente_o_expirado(entorno):
    client, _redis, _provider, _odoo, _reniec = entorno
    res = await client.post("/v1/auth/otp/send", json={"registro_id": "no-existe"})
    assert res.status_code == 410


# --------------------------------------------------------------------------- #
# Verification token
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_token_de_verificacion_no_se_puede_reutilizar(entorno):
    """Rule 3: the burned token is what stops a retry creating a second partner."""
    client, _redis, _provider, odoo, _reniec = entorno
    registro_id = await _abrir_registro(client)
    token = (await _verificar(client, registro_id)).json()["token_verificacion"]

    cuerpo = {"token_verificacion": token, "contrasena": "Segura123"}
    assert (await client.post("/v1/auth/register/complete", json=cuerpo)).status_code == 200
    assert (await client.post("/v1/auth/register/complete", json=cuerpo)).status_code == 409
    assert len(odoo.created) == 1


@pytest.mark.asyncio
async def test_no_se_puede_completar_sin_verificar_el_telefono(entorno):
    client, _redis, _provider, odoo, _reniec = entorno
    registro_id = await _abrir_registro(client)

    from app.core.security import create_verification_token
    token = create_verification_token(registro_id, auth_router.PROPOSITO_REGISTRO)

    res = await client.post("/v1/auth/register/complete",
                            json={"token_verificacion": token, "contrasena": "Segura123"})
    assert res.status_code == 403
    assert odoo.created == []


@pytest.mark.asyncio
async def test_token_de_reset_no_sirve_para_registrarse(entorno):
    """The purpose claim keeps one approved code from being spent on the other flow."""
    client, _redis, _provider, _odoo, _reniec = entorno
    registro_id = await _abrir_registro(client)

    from app.core.security import create_verification_token
    token = create_verification_token(registro_id, auth_router.PROPOSITO_RESET)

    res = await client.post("/v1/auth/register/complete",
                            json={"token_verificacion": token, "contrasena": "Segura123"})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_contrasena_debil_es_rechazada(entorno):
    client, _redis, _provider, odoo, _reniec = entorno
    registro_id = await _abrir_registro(client)
    token = (await _verificar(client, registro_id)).json()["token_verificacion"]

    res = await client.post("/v1/auth/register/complete",
                            json={"token_verificacion": token, "contrasena": "12345678"})
    assert res.status_code == 400
    assert odoo.created == []


# --------------------------------------------------------------------------- #
# Session (RF-051)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_login_correcto_e_incorrecto(entorno):
    client, _redis, _provider, _odoo, _reniec = entorno

    malo = await client.post("/v1/auth/login",
                             json={"correo": "brandon@example.com", "contrasena": "incorrecta"})
    assert malo.status_code == 401

    bueno = await client.post("/v1/auth/login",
                              json={"correo": "brandon@example.com", "contrasena": "Segura123"})
    assert bueno.status_code == 200
    assert bueno.json()["cliente_id"] == 501


@pytest.mark.asyncio
async def test_renovacion_rota_el_token(entorno):
    client, _redis, _provider, _odoo, _reniec = entorno
    sesion = (await client.post("/v1/auth/login",
                                json={"correo": "brandon@example.com", "contrasena": "Segura123"})).json()

    res = await client.post("/v1/auth/refresh", json={"token_renovacion": sesion["token_renovacion"]})
    assert res.status_code == 200
    assert res.json()["token_renovacion"] != sesion["token_renovacion"]


@pytest.mark.asyncio
async def test_reutilizar_token_de_renovacion_mata_la_cadena(entorno):
    """RF-051: a refresh token presented twice is treated as theft."""
    client, _redis, _provider, _odoo, _reniec = entorno
    sesion = (await client.post("/v1/auth/login",
                                json={"correo": "brandon@example.com", "contrasena": "Segura123"})).json()
    viejo = sesion["token_renovacion"]

    nuevo = (await client.post("/v1/auth/refresh", json={"token_renovacion": viejo})).json()["token_renovacion"]

    replay = await client.post("/v1/auth/refresh", json={"token_renovacion": viejo})
    assert replay.status_code == 401

    # The chain is gone, so the legitimately rotated token dies with it.
    despues = await client.post("/v1/auth/refresh", json={"token_renovacion": nuevo})
    assert despues.status_code == 401


@pytest.mark.asyncio
async def test_logout_revoca_de_inmediato(entorno):
    client, _redis, _provider, _odoo, _reniec = entorno
    sesion = (await client.post("/v1/auth/login",
                                json={"correo": "brandon@example.com", "contrasena": "Segura123"})).json()

    assert (await client.post("/v1/auth/logout",
                              json={"token_renovacion": sesion["token_renovacion"]})).status_code == 200
    assert (await client.post("/v1/auth/refresh",
                              json={"token_renovacion": sesion["token_renovacion"]})).status_code == 401


# --------------------------------------------------------------------------- #
# Password recovery
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_reset_completo_cambia_la_contrasena(entorno):
    client, _redis, provider, odoo, _reniec = entorno
    odoo.reset_account = {
        "found": True,
        "partner_id": 501,
        "email": "brandon@example.com",
        "phone": "987654321",
        "verification_channel": "sms",
    }

    inicio = await client.post("/v1/auth/password/reset", json={"correo": "brandon@example.com"})
    assert inicio.status_code == 200
    registro_id = inicio.json()["registro_id"]

    token = (await _verificar(client, registro_id)).json()["token_verificacion"]
    res = await client.post("/v1/auth/password/reset/complete",
                            json={"token_verificacion": token, "contrasena": "OtraClave9"})
    assert res.status_code == 200
    assert odoo.passwords_set == [(501, "OtraClave9")]
    assert len(provider.sent) == 1


@pytest.mark.asyncio
async def test_reset_de_correo_desconocido_no_revela_nada(entorno):
    """Same shape, same path, and no message actually spent."""
    client, _redis, provider, odoo, _reniec = entorno
    odoo.reset_account = None

    inicio = await client.post("/v1/auth/password/reset", json={"correo": "nadie@example.com"})
    assert inicio.status_code == 200
    registro_id = inicio.json()["registro_id"]

    envio = await client.post("/v1/auth/otp/send", json={"registro_id": registro_id})
    assert envio.status_code == 200
    assert provider.sent == []

    res = await client.post("/v1/auth/otp/verify",
                            json={"registro_id": registro_id, "codigo": FakeOtpProvider.CODE})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_el_proveedor_por_defecto_es_twilio_verify():
    """Guards the wiring: the default provider is the Twilio Verify client."""
    set_otp_provider(None)
    assert isinstance(otp_provider_module.get_otp_provider(), otp_provider_module.TwilioVerifyProvider)
    set_otp_provider(None)


# --------------------------------------------------------------------------- #
# Identity document of any enabled type (ADR-018)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_numero_que_no_cumple_el_tipo_es_rechazado(entorno):
    client, *_ = entorno
    res = await client.post("/v1/auth/register", json={**REGISTRO_VALIDO, "numero_documento": "1234"})
    assert res.status_code == 422
    assert res.json()["errores"][0]["campo"] == "numero_documento"


@pytest.mark.asyncio
async def test_tipo_no_habilitado_es_rechazado(entorno):
    client, *_ = entorno
    res = await client.post("/v1/auth/register", json={**REGISTRO_VALIDO, "tipo_documento_id": 99})
    assert res.status_code == 422
    assert res.json()["errores"][0]["campo"] == "tipo_documento_id"


@pytest.mark.asyncio
async def test_documento_que_no_es_dni_no_consulta_reniec(entorno):
    client, _redis, _provider, odoo, reniec = entorno
    carnet = {**REGISTRO_VALIDO, "tipo_documento_id": 7, "numero_documento": "ce-001234 5"}

    sin_nombre = await client.post("/v1/auth/register", json=carnet)
    assert sin_nombre.status_code == 400
    assert sin_nombre.json()["type"].endswith("/nombres-requeridos")

    res = await client.post("/v1/auth/register", json={**carnet, "nombre": "Ana", "apellido": "Pérez"})
    assert res.status_code == 200, res.text
    cuerpo = res.json()
    assert cuerpo["estado_validacion"] == "pendiente"
    assert cuerpo["motivo_validacion"] == "tipo_sin_validacion"
    assert cuerpo["datos_desde_reniec"] is False
    assert reniec.calls == []
    assert odoo.prechecked == (7, "CE0012345")
