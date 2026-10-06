"""
E-mail verification from the profile, and the split it implies (ADR-017):
sign-up proves the phone only, the e-mail is proven on its own.
"""
import httpx
import pytest

from app.clients.odoo_client import OdooClientError
from app.clients.otp_provider import set_otp_provider
from app.clients.reniec_client import set_reniec_client
from app.core.config import settings
from app.core.security import create_access_token
from app.main import app
from app.routers import auth as auth_router
from app.routers import perfil as perfil_router
from app.routers import master_data as md_router
from tests.test_auth_otp import REGISTRO_VALIDO, FakeOdoo, FakeOtpProvider, FakeRedis, FakeReniec

PLANTILLA_VERIFICACION = "d-verificacion"
PLANTILLA_RECUPERACION = "d-recuperacion"


class FakeOdooCorreo(FakeOdoo):
    def __init__(self):
        super().__init__()
        self.email = "brandon@example.com"
        self.name = "JOSE PEDRO CASTILLO TERRONES"
        self.email_verified = False
        self.marked = []

    async def auth_email_status(self, partner_id, request_id):
        return {"partner_id": partner_id, "email": self.email, "name": self.name,
                "email_verified": self.email_verified}

    async def auth_mark_email_verified(self, partner_id, email, request_id):
        if email != self.email:
            raise OdooClientError(409, "Email Changed", "The e-mail changed.")
        self.email_verified = True
        self.marked.append((partner_id, email))
        return {"email_verified": True}

    async def auth_verify_credentials(self, email, password, request_id):
        resultado = await super().auth_verify_credentials(email, password, request_id)
        if resultado.get("authenticated"):
            resultado["email_verified"] = self.email_verified
        return resultado


@pytest.fixture
def entorno(monkeypatch):
    redis = FakeRedis()
    provider = FakeOtpProvider()
    odoo = FakeOdooCorreo()

    for modulo in (auth_router, perfil_router):
        monkeypatch.setattr(modulo, "get_state_redis", lambda: redis)
        monkeypatch.setattr(modulo, "odoo_client", odoo)
    monkeypatch.setattr(settings, "TWILIO_EMAIL_TEMPLATE_EMAIL_VERIFICATION", PLANTILLA_VERIFICACION)
    monkeypatch.setattr(settings, "TWILIO_EMAIL_TEMPLATE_PASSWORD_RESET", PLANTILLA_RECUPERACION)
    cache = FakeRedis()
    monkeypatch.setattr(md_router, "get_cache_redis", lambda: cache)
    set_otp_provider(provider)
    set_reniec_client(FakeReniec())

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    yield client, redis, provider, odoo

    set_otp_provider(None)
    set_reniec_client(None)


def _sesion(partner_id: int = 501) -> dict:
    return {"Authorization": f"Bearer {create_access_token(partner_id)}"}


# --------------------------------------------------------------------------- #
# Session required
# --------------------------------------------------------------------------- #

async def test_sin_sesion_se_rechaza(entorno):
    client, *_ = entorno
    res = await client.post("/v1/me/email/otp/send")
    assert res.status_code == 401
    assert res.json()["type"].endswith("/sesion-requerida")


async def test_token_invalido_se_rechaza(entorno):
    client, *_ = entorno
    res = await client.post("/v1/me/email/otp/send", headers={"Authorization": "Bearer basura"})
    assert res.status_code == 401
    assert res.json()["type"].endswith("/sesion-expirada")


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

async def test_envio_usa_la_plantilla_de_verificacion_y_el_nombre(entorno):
    client, _, provider, _ = entorno
    res = await client.post("/v1/me/email/otp/send", headers=_sesion())
    assert res.status_code == 200, res.text
    cuerpo = res.json()
    assert cuerpo["canal"] == "correo"
    assert cuerpo["destino_enmascarado"].endswith("@example.com")
    assert provider.sent[-1] == ("brandon@example.com", "correo")
    assert provider.email_options[-1] == (PLANTILLA_VERIFICACION, {"first_name": "Jose"})


async def test_flujo_completo_marca_el_correo_en_odoo(entorno):
    client, _, _, odoo = entorno
    await client.post("/v1/me/email/otp/send", headers=_sesion())
    res = await client.post("/v1/me/email/otp/verify", json={"codigo": FakeOtpProvider.CODE}, headers=_sesion())
    assert res.status_code == 200, res.text
    assert res.json() == {"correo_verificado": True}
    assert odoo.marked == [(501, "brandon@example.com")]


async def test_correo_ya_verificado_no_gasta_un_envio(entorno):
    client, _, provider, odoo = entorno
    odoo.email_verified = True
    res = await client.post("/v1/me/email/otp/send", headers=_sesion())
    assert res.status_code == 409
    assert res.json()["type"].endswith("/correo-ya-verificado")
    assert provider.sent == []


async def test_verificar_sin_codigo_enviado_es_codigo_expirado(entorno):
    client, *_ = entorno
    res = await client.post("/v1/me/email/otp/verify", json={"codigo": "123456"}, headers=_sesion())
    assert res.status_code == 410
    assert res.json()["type"].endswith("/codigo-expirado")


async def test_codigo_incorrecto_y_luego_intentos_agotados(entorno):
    client, _, _, odoo = entorno
    await client.post("/v1/me/email/otp/send", headers=_sesion())
    for _ in range(settings.OTP_MAX_CHECK_ATTEMPTS):
        res = await client.post("/v1/me/email/otp/verify", json={"codigo": "000000"}, headers=_sesion())
        assert res.status_code == 400
    res = await client.post("/v1/me/email/otp/verify", json={"codigo": FakeOtpProvider.CODE}, headers=_sesion())
    assert res.status_code == 429
    assert odoo.marked == []


async def test_correo_cambiado_durante_la_verificacion(entorno):
    client, _, _, odoo = entorno
    await client.post("/v1/me/email/otp/send", headers=_sesion())
    odoo.email = "otro@example.com"
    res = await client.post("/v1/me/email/otp/verify", json={"codigo": FakeOtpProvider.CODE}, headers=_sesion())
    assert res.status_code == 409
    assert odoo.marked == []


async def test_reenvio_respeta_el_enfriamiento(entorno):
    client, *_ = entorno
    assert (await client.post("/v1/me/email/otp/send", headers=_sesion())).status_code == 200
    segundo = await client.post("/v1/me/email/otp/send", headers=_sesion())
    assert segundo.status_code == 429
    assert "Retry-After" in segundo.headers


# --------------------------------------------------------------------------- #
# The split with sign-up and recovery
# --------------------------------------------------------------------------- #

async def test_el_registro_solo_ofrece_canales_de_telefono(entorno):
    client, *_ = entorno
    res = await client.post("/v1/auth/register", json=REGISTRO_VALIDO)
    assert res.status_code == 200, res.text
    assert "correo" not in res.json()["canales_disponibles"]

    envio = await client.post("/v1/auth/otp/send", json={"registro_id": res.json()["registro_id"], "canal": "correo"})
    assert envio.status_code == 400
    assert envio.json()["type"].endswith("/canal-no-disponible")


async def test_recuperacion_por_correo_usa_su_propia_plantilla(entorno):
    client, _, provider, odoo = entorno
    odoo.reset_account = {"found": True, "partner_id": 501, "email": "brandon@example.com",
                          "phone": "987654321", "name": "JOSE PEDRO CASTILLO TERRONES"}
    inicio = await client.post("/v1/auth/password/reset", json={"correo": "brandon@example.com"})
    assert "correo" in inicio.json()["canales_disponibles"]

    res = await client.post("/v1/auth/otp/send", json={"registro_id": inicio.json()["registro_id"], "canal": "correo"})
    assert res.status_code == 200, res.text
    assert provider.email_options[-1] == (PLANTILLA_RECUPERACION, {"first_name": "Jose"})


async def test_la_sesion_informa_si_el_correo_esta_verificado(entorno):
    client, _, _, odoo = entorno
    credenciales = {"correo": "brandon@example.com", "contrasena": "Segura123"}
    assert (await client.post("/v1/auth/login", json=credenciales)).json()["correo_verificado"] is False
    odoo.email_verified = True
    assert (await client.post("/v1/auth/login", json=credenciales)).json()["correo_verificado"] is True
