import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from redis.asyncio import Redis

from ..clients.odoo_client import OdooClient, OdooClientError
from ..clients.otp_provider import get_otp_provider
from ..clients.redis_client import get_state_redis
from ..clients.reniec_client import get_reniec_client
from ..core.breaker import CircuitBreakerOpenError
from ..core.config import settings
from ..core.errors import ApiError, desde_odoo
from ..core.limits import check_rate_limit
from ..core.otp_guard import (
    burn_verification_token,
    check_send_allowed,
    register_check_attempt,
    send_refusal,
    start_cooldown,
)
from ..core.passwords import PasswordPolicyError, mask_destination, validate_password_policy
from ..core.security import (
    SecurityError,
    create_access_token,
    create_verification_token,
    verify_verification_token,
)
from ..schemas.auth import (
    LoginIn,
    OtpEnviarIn,
    OtpEnviarOut,
    OtpVerificarIn,
    OtpVerificarOut,
    PasswordResetCompletarIn,
    PasswordResetIn,
    PasswordResetOut,
    RegistroCompletarIn,
    RegistroIn,
    RegistroOut,
    RenovarIn,
    SesionOut,
)
from ..schemas.common import MensajeOut
from .master_data import tipos_documento

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/auth", tags=["Auth"])
odoo_client = OdooClient()

PROPOSITO_REGISTRO = "registro"
CODIGO_SUNAT_DNI = "1"  # SUNAT catalogue 06: the only type RENIEC can validate
PROPOSITO_RESET = "reset"

REFRESH_TTL_SECONDS = settings.JWT_REFRESH_EXPIRE_DAYS * 86400


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _client_ip(request: Request) -> str:
    """Behind Cloud Run the caller's address is the first hop of X-Forwarded-For."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _request_id(request: Request) -> str:
    return request.state.request_id


def _e164_peru(telefono: str) -> str:
    return f"+51{telefono}"


def _destino(borrador: Dict[str, str], canal: str) -> str:
    return borrador.get("correo", "") if canal == "correo" else _e164_peru(borrador.get("telefono", ""))


def primer_nombre(nombre: str) -> str:
    """'JOSE PEDRO CASTILLO TERRONES' -> 'Jose', for the greeting of the e-mail."""
    partes = (nombre or "").split()
    return partes[0].capitalize() if partes else ""


def _canales_para(proposito: str) -> List[str]:
    """
    Sign-up proves the phone, so only phone channels count there; the e-mail is
    verified on its own from the profile (ADR-017). A reset may use either.
    """
    return settings.phone_otp_channels if proposito == PROPOSITO_REGISTRO else settings.otp_channels


def _canal_sugerido(proposito: str) -> str:
    disponibles = _canales_para(proposito)
    return settings.OTP_CHANNEL_DEFAULT if settings.OTP_CHANNEL_DEFAULT in disponibles else (disponibles or [""])[0]


def _canal_para(borrador: Dict[str, str], solicitado: Optional[str]) -> str:
    """
    Resolves the verification channel (RF-062): what the client asked for, else
    what the account prefers, else the default. Rejects anything not switched on
    in this environment even if the contract declares it.
    """
    proposito = borrador.get("proposito", PROPOSITO_REGISTRO)
    disponibles = _canales_para(proposito)
    canal = (solicitado or borrador.get("canal_preferido") or _canal_sugerido(proposito)).strip().lower()
    if canal not in disponibles:
        opciones = ", ".join(disponibles)
        raise ApiError("canal-no-disponible", f"Canal de verificación no disponible. Opciones: {opciones}.")
    return canal


async def _tipo_documento(tipo_id: int, numero: str, request_id: str) -> Dict[str, Any]:
    """
    The document type the customer chose, from the same list the selector shows,
    with the number checked against its rules. Odoo checks it again.
    """
    tipo = next((t for t in await tipos_documento(odoo_client, request_id) if t.get("id") == tipo_id), None)
    if not tipo:
        raise ApiError("parametros-invalidos", "El tipo de documento no está habilitado.",
                       errores=[{"campo": "tipo_documento_id", "mensaje": "Tipo de documento no habilitado"}])

    error = ""
    if tipo.get("solo_digitos") and not numero.isdigit():
        error = f"El {tipo.get('nombre', 'documento')} solo admite dígitos."
    elif tipo.get("longitud") and len(numero) != tipo["longitud"]:
        error = f"El {tipo.get('nombre', 'documento')} debe tener {tipo['longitud']} caracteres."
    if error:
        raise ApiError("parametros-invalidos", tipo.get("ayuda") or error,
                       errores=[{"campo": "numero_documento", "mensaje": error}])
    return tipo


async def _cargar_borrador(state_redis: Redis, registro_id: str) -> Dict[str, str]:
    try:
        borrador = await state_redis.hgetall(f"reg:{registro_id}")
    except Exception as e:
        _logger.error("holi-estado unreachable while loading draft %s: %s", registro_id, e)
        raise ApiError("servicio-no-disponible", "El servicio de registro no está disponible en este momento.")

    if not borrador:
        raise ApiError("registro-expirado", "El registro expiró o no existe. Vuelve a iniciar el proceso.")
    return borrador


async def _descartar_borrador(state_redis: Redis, registro_id: str) -> None:
    try:
        await state_redis.delete(f"reg:{registro_id}", f"otp:{registro_id}", f"otp:try:{registro_id}")
    except Exception as e:
        _logger.warning("Could not discard draft %s: %s", registro_id, e)


async def _guardar_sesion_otp(state_redis: Redis, registro_id: str, sid: str,
                              canal: str, destino: str) -> None:
    try:
        await state_redis.hset(f"otp:{registro_id}", mapping={
            "sid": sid,
            "canal": canal,
            "destino": destino,
        })
        await state_redis.expire(f"otp:{registro_id}", settings.OTP_SESSION_TTL_SECONDS)
        await state_redis.hset(f"reg:{registro_id}", "canal_preferido", canal)
    except Exception as e:
        _logger.warning("Code was sent but the OTP session could not be stored: %s", e)


async def _emitir_sesion(state_redis: Redis, partner_id: int,
                         extra: Optional[Dict[str, Any]] = None) -> SesionOut:
    """
    Issues the RF-051 pair and files the refresh token under a session family so
    the whole chain can be revoked at once.
    """
    familia = str(uuid.uuid4())
    token_renovacion = str(uuid.uuid4())

    await state_redis.set(
        f"rt:{token_renovacion}",
        json.dumps({"partner_id": partner_id, "familia": familia}),
        ex=REFRESH_TTL_SECONDS,
    )
    await state_redis.sadd(f"sess:fam:{familia}", token_renovacion)
    await state_redis.expire(f"sess:fam:{familia}", REFRESH_TTL_SECONDS)
    await state_redis.sadd(f"sess:partner:{partner_id}", familia)
    await state_redis.expire(f"sess:partner:{partner_id}", REFRESH_TTL_SECONDS)

    extra = extra or {}
    return SesionOut(
        token_acceso=create_access_token(partner_id),
        expira_en=settings.JWT_ACCESS_EXPIRE_MINUTES * 60,
        token_renovacion=token_renovacion,
        cliente_id=partner_id,
        estado_validacion=extra.get("estado_validacion"),
        datos_fiscales_completos=extra.get("datos_fiscales_completos"),
        correo_verificado=extra.get("correo_verificado"),
    )


async def _revocar_familia(state_redis: Redis, familia: str) -> int:
    """Kills every refresh token of a session chain."""
    try:
        tokens = await state_redis.smembers(f"sess:fam:{familia}")
        if tokens:
            await state_redis.delete(*[f"rt:{t}" for t in tokens])
        await state_redis.delete(f"sess:fam:{familia}")
        return len(tokens or [])
    except Exception as e:
        _logger.error("Could not revoke session family %s: %s", familia, e)
        return 0


# --------------------------------------------------------------------------- #
# Sign-up handshake (RF-001)
# --------------------------------------------------------------------------- #

@router.post("/register", response_model=RegistroOut)
async def registrar(payload: RegistroIn, request: Request):
    """
    Step 1 — opens the sign-up handshake. Nothing is written to Odoo here.

    The draft lives in holi-estado with a TTL: an abandoned sign-up has to
    disappear on its own, and a script hammering this endpoint must not be able
    to fill the ERP with half-made partners.
    """
    if not payload.acepta_terminos:
        raise ApiError("terminos-no-aceptados", "Debes aceptar los términos y condiciones para registrarte.")

    state_redis = get_state_redis()
    request_id = _request_id(request)
    ip = _client_ip(request)

    if not await check_rate_limit(state_redis, "register", ip or "sin-ip", max_requests=10, window_seconds=3600):
        raise ApiError("limite-excedido", "Demasiados intentos de registro. Inténtalo más tarde.")

    tipo = await _tipo_documento(payload.tipo_documento_id, payload.numero_documento, request_id)
    es_dni = tipo.get("codigo") == CODIGO_SUNAT_DNI

    try:
        precheck = await odoo_client.auth_register_precheck(
            payload.tipo_documento_id, payload.numero_documento, payload.correo, payload.telefono, request_id
        )
    except CircuitBreakerOpenError:
        raise ApiError("servicio-no-disponible", "El servicio de registro no está disponible en este momento.")

    if not precheck.get("available", False):
        # RF-001 escenario 2: an existing account is offered recovery, never a second one.
        raise ApiError("cuenta-ya-existe", precheck.get("reason"))

    # The lookup happens only once the data is known to be free. Each one spends
    # a credit with the provider, and a duplicate sign-up must not pay for one
    # — which is also what stops replayed sign-ups from draining the balance.
    # RENIEC only knows DNIs; any other document is typed by the customer and
    # stays pendiente until someone validates it (ADR-018).
    reniec = await get_reniec_client().lookup(payload.numero_documento, state_redis) if es_dni else None

    # RF-001 escenario 3: with the lookup unavailable the account still proceeds,
    # flagged pendiente, which can browse but cannot confirm a purchase.
    if reniec is None:
        estado_validacion, motivo_validacion = "pendiente", "tipo_sin_validacion"
    else:
        estado_validacion = reniec.validation_state
        motivo_validacion = "" if estado_validacion == "validado" else (
            "documento_no_encontrado" if reniec.available else "servicio_no_disponible"
        )

    nombre_reniec = reniec.full_name if reniec else ""
    nombres = (reniec.nombres if reniec else "") or (payload.nombre or "").strip()
    apellidos = (reniec.apellidos if reniec else "") or (payload.apellido or "").strip()
    nombre_final = nombre_reniec or " ".join(p for p in [nombres, apellidos] if p).strip()

    if not nombre_final:
        raise ApiError("nombres-requeridos")

    registro_id = str(uuid.uuid4())
    borrador = {
        "proposito": PROPOSITO_REGISTRO,
        "tipo_documento_id": str(payload.tipo_documento_id),
        "numero_documento": payload.numero_documento,
        "correo": payload.correo,
        "telefono": payload.telefono,
        "nombre": nombre_final,
        "nombres": nombres,
        "apellidos": apellidos,
        "nombre_desde_reniec": "1" if nombre_reniec else "0",
        "fecha_nacimiento": payload.fecha_nacimiento or "",
        "acepta_terminos": "1",
        "acepta_comerciales": "1" if payload.acepta_comerciales else "0",
        "estado_validacion": estado_validacion,
        "telefono_verificado": "0",
        "canal_preferido": _canal_sugerido(PROPOSITO_REGISTRO),
        "simulado": "0",
    }

    try:
        await state_redis.hset(f"reg:{registro_id}", mapping=borrador)
        await state_redis.expire(f"reg:{registro_id}", settings.REGISTRATION_DRAFT_TTL_SECONDS)
    except Exception as e:
        _logger.error("Could not persist registration draft: %s", e)
        raise ApiError("servicio-no-disponible", "El servicio de registro no está disponible en este momento.")

    return RegistroOut(
        registro_id=registro_id,
        nombre_precargado=nombre_final,
        nombres_precargados=nombres,
        apellidos_precargados=apellidos,
        datos_desde_reniec=bool(nombre_reniec),
        estado_validacion=estado_validacion,
        motivo_validacion=motivo_validacion,
        canales_disponibles=_canales_para(PROPOSITO_REGISTRO),
        canal_sugerido=_canal_sugerido(PROPOSITO_REGISTRO),
        expira_en=settings.REGISTRATION_DRAFT_TTL_SECONDS,
    )


@router.post("/otp/send", response_model=OtpEnviarOut)
async def enviar_codigo(payload: OtpEnviarIn, request: Request):
    """
    Step 2 — asks the provider for a one-time code.

    The code is neither generated nor stored here: we hand over a destination
    and later ask for a verdict.
    """
    state_redis = get_state_redis()
    borrador = await _cargar_borrador(state_redis, payload.registro_id)
    canal = _canal_para(borrador, payload.canal)
    destino = _destino(borrador, canal)

    if not destino:
        raise ApiError("destino-invalido", "El registro no tiene un destino válido para el código.")

    veredicto = await check_send_allowed(state_redis, destino, _client_ip(request), payload.registro_id)
    if not veredicto.allowed:
        raise send_refusal(veredicto)

    reintento = await start_cooldown(state_redis, payload.registro_id)
    respuesta = OtpEnviarOut(
        enviado=True,
        canal=canal,
        destino_enmascarado=mask_destination(destino, canal),
        reintento_en=reintento,
        expira_en=settings.OTP_SESSION_TTL_SECONDS,
    )

    # A reset for an address that is not a customer walks the same path and gets
    # the same answer, so this endpoint cannot be used to find out which e-mails
    # belong to a customer. The OTP session is recorded even though nothing was
    # sent, otherwise the next step would answer 410 here and 400 for a real
    # account, which is the same disclosure by another door.
    if borrador.get("simulado") == "1":
        await _guardar_sesion_otp(state_redis, payload.registro_id, "simulado", canal, destino)
        return respuesta

    try:
        # By e-mail this can only be a password reset: sign-up is phone-only.
        resultado = await get_otp_provider().send(
            destino, canal, settings.TWILIO_LOCALE,
            email_template=settings.TWILIO_EMAIL_TEMPLATE_PASSWORD_RESET,
            substitutions={"first_name": primer_nombre(borrador.get("nombre", ""))},
        )
    except CircuitBreakerOpenError:
        # Fail closed: no account is ever created without a verified phone.
        raise ApiError("servicio-no-disponible",
                       "El servicio de verificación no está disponible. Inténtalo en unos minutos.")

    await _guardar_sesion_otp(state_redis, payload.registro_id, resultado.sid, canal, destino)
    return respuesta


@router.post("/otp/verify", response_model=OtpVerificarOut)
async def verificar_codigo(payload: OtpVerificarIn, request: Request):
    """
    Step 3 — spends one attempt against the provider's verdict.

    Past the attempt ceiling the whole handshake is dropped: six digits are
    guessable if you are allowed to keep guessing.
    """
    state_redis = get_state_redis()
    borrador = await _cargar_borrador(state_redis, payload.registro_id)

    intento = await register_check_attempt(state_redis, payload.registro_id)
    if not intento.allowed:
        if intento.reason == "estado_no_disponible":
            raise ApiError("servicio-no-disponible", "No podemos verificar el código en este momento.")
        await _descartar_borrador(state_redis, payload.registro_id)
        raise ApiError("intentos-agotados")

    sesion_otp = await state_redis.hgetall(f"otp:{payload.registro_id}")
    if not sesion_otp:
        raise ApiError("registro-expirado", "El código expiró. Solicita uno nuevo.")

    canal = sesion_otp.get("canal", settings.OTP_CHANNEL_DEFAULT)
    destino = sesion_otp.get("destino") or _destino(borrador, canal)

    if borrador.get("simulado") == "1":
        raise ApiError("codigo-incorrecto", "El código ingresado no es correcto.")

    try:
        resultado = await get_otp_provider().check(destino, payload.codigo)
    except CircuitBreakerOpenError:
        raise ApiError("servicio-no-disponible",
                       "El servicio de verificación no está disponible. Inténtalo en unos minutos.")

    if not resultado.approved:
        if resultado.status == "expired":
            raise ApiError("registro-expirado", "El código expiró. Solicita uno nuevo.")
        raise ApiError("codigo-incorrecto", "El código ingresado no es correcto.")

    proposito = borrador.get("proposito", PROPOSITO_REGISTRO)
    try:
        await state_redis.hset(f"reg:{payload.registro_id}", mapping={
            "telefono_verificado": "1",
            "canal_verificado": canal,
        })
        await state_redis.delete(f"otp:{payload.registro_id}")
    except Exception as e:
        _logger.warning("Code approved but draft could not be marked: %s", e)

    _logger.info("OTP approved for registration %s over %s (%s)",
                 payload.registro_id, canal, _request_id(request))

    return OtpVerificarOut(
        verificado=True,
        token_verificacion=create_verification_token(payload.registro_id, proposito),
        expira_en=settings.VERIFICATION_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/register/complete", response_model=SesionOut)
async def completar_registro(payload: RegistroCompletarIn, request: Request):
    """
    Step 4 — the first and only write into Odoo.

    Rule 3: the idempotency key is reserved (the token is burned) before Odoo is
    called, so a retried request cannot produce a second partner.
    """
    state_redis = get_state_redis()
    request_id = _request_id(request)

    try:
        claims = verify_verification_token(payload.token_verificacion, PROPOSITO_REGISTRO)
    except SecurityError:
        raise ApiError("token-invalido")

    try:
        validate_password_policy(payload.contrasena)
    except PasswordPolicyError as e:
        raise ApiError("contrasena-debil", e.detail)

    # The burn is the idempotency reservation, so it has to happen before Odoo is
    # touched — but after the password is checked, so a rejected password does
    # not cost the customer the code they already received.
    if not await burn_verification_token(state_redis, claims["jti"]):
        raise ApiError("registro-ya-completado", "Este registro ya fue completado.")

    registro_id = claims["sub"]
    borrador = await _cargar_borrador(state_redis, registro_id)

    if borrador.get("telefono_verificado") != "1":
        raise ApiError("telefono-no-verificado", "Debes verificar tu teléfono antes de completar el registro.")

    partner_data = {
        "document_type_id": int(borrador["tipo_documento_id"]),
        "document_number": borrador["numero_documento"],
        "name": borrador["nombre"],
        "email": borrador["correo"],
        "phone": borrador["telefono"],
        "birthdate": borrador.get("fecha_nacimiento") or None,
        "phone_verified": True,
        "verified_channel": borrador.get("canal_verificado", settings.OTP_CHANNEL_DEFAULT),
        "validation_state": borrador.get("estado_validacion", "pendiente"),
        "accepts_marketing": borrador.get("acepta_comerciales") == "1",
        "account_origin": "registro_dni",
        "password": payload.contrasena,
    }

    try:
        resultado = await odoo_client.auth_partner_create(partner_data, claims["jti"], request_id)
    except CircuitBreakerOpenError:
        raise ApiError("servicio-no-disponible", "No pudimos crear tu cuenta en este momento. Inténtalo de nuevo.")

    await _descartar_borrador(state_redis, registro_id)

    return await _emitir_sesion(state_redis, int(resultado["partner_id"]), {
        "estado_validacion": resultado.get("validation_state"),
        "datos_fiscales_completos": resultado.get("fiscal_data_complete", True),
        "correo_verificado": bool(resultado.get("email_verified", False)),
    })


# --------------------------------------------------------------------------- #
# Session (RF-051)
# --------------------------------------------------------------------------- #

@router.post("/login", response_model=SesionOut)
async def login(payload: LoginIn, request: Request):
    """Odoo holds the hash and returns the verdict (RF-001); the Edge owns the session."""
    state_redis = get_state_redis()
    request_id = _request_id(request)
    ip = _client_ip(request)

    if not await check_rate_limit(state_redis, "login", f"{payload.correo}|{ip}", max_requests=10, window_seconds=300):
        raise ApiError("limite-excedido", "Demasiados intentos de inicio de sesión. Espera unos minutos.")

    try:
        resultado = await odoo_client.auth_verify_credentials(payload.correo, payload.contrasena, request_id)
    except CircuitBreakerOpenError:
        raise ApiError("servicio-no-disponible", "El servicio de autenticación no está disponible.")

    if not resultado.get("authenticated"):
        raise ApiError("credenciales-invalidas", "Correo o contraseña incorrectos.")

    if not resultado.get("phone_verified", False):
        raise ApiError("telefono-no-verificado", "Debes verificar tu número de teléfono antes de iniciar sesión.")

    return await _emitir_sesion(state_redis, int(resultado["partner_id"]), {
        "estado_validacion": resultado.get("validation_state"),
        "datos_fiscales_completos": resultado.get("fiscal_data_complete", True),
        "correo_verificado": bool(resultado.get("email_verified", False)),
    })


@router.post("/refresh", response_model=SesionOut)
async def renovar_sesion(payload: RenovarIn):
    """
    Rotates the refresh token.

    RF-051: a refresh token presented twice is treated as theft. The second use
    revokes the whole chain rather than just failing, because by then we cannot
    tell which of the two holders is the customer.
    """
    state_redis = get_state_redis()
    clave = f"rt:{payload.token_renovacion}"

    almacenado = await state_redis.get(clave)

    if not almacenado:
        usado = await state_redis.get(f"rt:used:{payload.token_renovacion}")
        if usado:
            try:
                familia = json.loads(usado).get("familia", "")
            except Exception:
                familia = ""
            revocados = await _revocar_familia(state_redis, familia) if familia else 0
            _logger.error(
                "Refresh token reuse detected (family %s): revoked %s tokens of the session chain.",
                familia, revocados,
            )
            raise ApiError("sesion-revocada", "Sesión invalidada por motivos de seguridad. Vuelve a iniciar sesión.")
        raise ApiError("sesion-expirada", "El token de renovación no es válido o expiró.")

    try:
        datos = json.loads(almacenado)
        partner_id = int(datos["partner_id"])
        familia = datos["familia"]
    except Exception:
        # Tokens issued before session families existed.
        partner_id = int(almacenado)
        familia = str(uuid.uuid4())

    await state_redis.delete(clave)
    await state_redis.srem(f"sess:fam:{familia}", payload.token_renovacion)
    await state_redis.set(
        f"rt:used:{payload.token_renovacion}",
        json.dumps({"partner_id": partner_id, "familia": familia}),
        ex=REFRESH_TTL_SECONDS,
    )

    nuevo = str(uuid.uuid4())
    await state_redis.set(
        f"rt:{nuevo}", json.dumps({"partner_id": partner_id, "familia": familia}), ex=REFRESH_TTL_SECONDS
    )
    await state_redis.sadd(f"sess:fam:{familia}", nuevo)
    await state_redis.expire(f"sess:fam:{familia}", REFRESH_TTL_SECONDS)

    return SesionOut(
        token_acceso=create_access_token(partner_id),
        expira_en=settings.JWT_ACCESS_EXPIRE_MINUTES * 60,
        token_renovacion=nuevo,
        cliente_id=partner_id,
    )


@router.post("/logout", response_model=MensajeOut)
async def logout(payload: RenovarIn):
    """Revokes the presented refresh token immediately."""
    state_redis = get_state_redis()
    almacenado = await state_redis.get(f"rt:{payload.token_renovacion}")
    if almacenado:
        try:
            familia = json.loads(almacenado).get("familia", "")
            if familia:
                await state_redis.srem(f"sess:fam:{familia}", payload.token_renovacion)
        except Exception:
            pass
    await state_redis.delete(f"rt:{payload.token_renovacion}")
    return MensajeOut(mensaje="Sesión cerrada correctamente.")


# --------------------------------------------------------------------------- #
# Password recovery (RF-062: same channel choice as sign-up)
# --------------------------------------------------------------------------- #

@router.post("/password/reset", response_model=PasswordResetOut)
async def iniciar_reset(payload: PasswordResetIn, request: Request):
    """
    Opens a recovery handshake. It answers the same way whether or not the
    address belongs to a customer, so it cannot be used to find out who is one.
    """
    state_redis = get_state_redis()
    request_id = _request_id(request)
    ip = _client_ip(request)

    if not await check_rate_limit(state_redis, "reset", ip or "sin-ip", max_requests=10, window_seconds=3600):
        raise ApiError("limite-excedido", "Demasiadas solicitudes de recuperación. Inténtalo más tarde.")

    cuenta: Dict[str, Any] = {}
    try:
        cuenta = await odoo_client.auth_reset_lookup(payload.correo, request_id)
    except CircuitBreakerOpenError:
        raise ApiError("servicio-no-disponible", "El servicio de recuperación no está disponible.")
    except OdooClientError as e:
        if e.status_code != 404:
            raise desde_odoo(e)

    registro_id = str(uuid.uuid4())
    encontrada = bool(cuenta.get("found"))
    borrador = {
        "proposito": PROPOSITO_RESET,
        "correo": cuenta.get("email") or payload.correo,
        "telefono": cuenta.get("phone") or "",
        "nombre": cuenta.get("name") or "",
        "partner_id": str(cuenta.get("partner_id") or ""),
        "telefono_verificado": "0",
        "canal_preferido": cuenta.get("verification_channel") or settings.OTP_CHANNEL_DEFAULT,
        "simulado": "0" if encontrada else "1",
    }

    await state_redis.hset(f"reg:{registro_id}", mapping=borrador)
    await state_redis.expire(f"reg:{registro_id}", settings.REGISTRATION_DRAFT_TTL_SECONDS)

    return PasswordResetOut(
        registro_id=registro_id,
        canales_disponibles=_canales_para(PROPOSITO_RESET),
        canal_sugerido=borrador["canal_preferido"],
        expira_en=settings.REGISTRATION_DRAFT_TTL_SECONDS,
    )


@router.post("/password/reset/complete", response_model=MensajeOut)
async def completar_reset(payload: PasswordResetCompletarIn, request: Request):
    """Sets the new password and drops every open session of that customer."""
    state_redis = get_state_redis()
    request_id = _request_id(request)

    try:
        claims = verify_verification_token(payload.token_verificacion, PROPOSITO_RESET)
    except SecurityError:
        raise ApiError("token-invalido")

    try:
        validate_password_policy(payload.contrasena)
    except PasswordPolicyError as e:
        raise ApiError("contrasena-debil", e.detail)

    if not await burn_verification_token(state_redis, claims["jti"]):
        raise ApiError("registro-ya-completado", "Esta solicitud de recuperación ya fue utilizada.")

    registro_id = claims["sub"]
    borrador = await _cargar_borrador(state_redis, registro_id)
    partner_id = borrador.get("partner_id")

    if borrador.get("telefono_verificado") != "1" or not partner_id:
        raise ApiError("telefono-no-verificado", "Debes verificar tu identidad antes de cambiar la contraseña.")

    try:
        await odoo_client.auth_set_password(int(partner_id), payload.contrasena, request_id)
    except CircuitBreakerOpenError:
        raise ApiError("servicio-no-disponible", "No pudimos actualizar tu contraseña en este momento.")

    # A password change is also an eviction: whoever was in with the old one goes out.
    try:
        familias = await state_redis.smembers(f"sess:partner:{partner_id}")
        for familia in familias or []:
            await _revocar_familia(state_redis, familia)
        await state_redis.delete(f"sess:partner:{partner_id}")
    except Exception as e:
        _logger.warning("Could not revoke sessions after password reset for partner %s: %s", partner_id, e)

    await _descartar_borrador(state_redis, registro_id)
    return MensajeOut(mensaje="Tu contraseña fue actualizada. Inicia sesión con tu nueva contraseña.")
